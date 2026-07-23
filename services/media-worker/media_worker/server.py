import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .core import normalize_bluray, process_movie, process_trailer


REPORT_ROOT_ENV = "MEDIA_WORKER_REPORT_ROOT"
DEFAULT_REPORT_ROOT = "/config/media-worker"
MOVIES_ROOT_ENV = "MEDIA_WORKER_MOVIES_ROOT"
DEFAULT_MOVIES_ROOT = "/data/media/movies"
REVIEW_ROOT_ENV = "MEDIA_WORKER_REVIEW_ROOT"
DEFAULT_REVIEW_ROOT = "/data/media/repetidas_vs_error"
CALLBACK_ORIGIN_ENV = "MEDIA_WORKER_CALLBACK_ORIGIN"
DEFAULT_CALLBACK_ORIGIN = "http://arr-orchestrator:8787"
DEFAULT_ALLOWED_SOURCE_ROOT = "/data/downloads/torrents/complete/taller"
TERMINAL_FILENAMES = {
    "movie": "media_result.json",
    "trailer": "trailer_result.json",
    "bluray": "bluray_result.json",
}
TERMINAL_STATUSES = {"done", "review", "error"}
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_REQUEST_BYTES = 256 * 1024
MAX_TERMINAL_BYTES = 16 * 1024 * 1024
MAX_ERROR_LENGTH = 500


class RequestValidationError(ValueError):
    pass


class TerminalStateError(RuntimeError):
    pass


def _allowed_roots() -> list[Path]:
    configured = os.environ.get(
        "MEDIA_WORKER_ALLOWED_ROOTS",
        DEFAULT_ALLOWED_SOURCE_ROOT,
    )
    return [
        Path(item).resolve()
        for item in configured.split(os.pathsep)
        if item.strip()
    ]


def _canonical_report_root() -> Path:
    configured = os.environ.get(REPORT_ROOT_ENV, DEFAULT_REPORT_ROOT).strip()
    return Path(configured or DEFAULT_REPORT_ROOT).resolve()


def _configured_path(env_name: str, default: str) -> Path:
    configured = os.environ.get(env_name, default).strip()
    return Path(configured or default).resolve()


def _path_inside_allowed(path: Path) -> bool:
    return any(path == root or path.is_relative_to(root) for root in _allowed_roots())


def _validated_callback(value: object, job_id: str) -> str:
    callback = str(value or "").strip()
    if not callback:
        return ""
    parsed = urlsplit(callback)
    allowed = urlsplit(
        os.environ.get(CALLBACK_ORIGIN_ENV, DEFAULT_CALLBACK_ORIGIN).strip()
        or DEFAULT_CALLBACK_ORIGIN
    )
    if (
        parsed.scheme != allowed.scheme
        or parsed.hostname != allowed.hostname
        or parsed.port != allowed.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/jobs/{job_id}/events"
    ):
        raise RequestValidationError("callback_url no pertenece al orquestador permitido")
    return callback


def _validated_job_id(value: object) -> str:
    job_id = str(value or "").strip()
    if not JOB_ID_RE.fullmatch(job_id):
        raise RequestValidationError("job_id no es valido")
    return job_id


def _validated_job_payload(payload: object, kind: str) -> Dict[str, object]:
    if not isinstance(payload, dict):
        raise RequestValidationError("El payload debe ser un objeto JSON")
    job_id = _validated_job_id(payload.get("job_id"))
    reports_text = str(payload.get("reports_root") or "").strip()
    if not reports_text:
        raise RequestValidationError("reports_root es obligatorio")
    reports_root = Path(reports_text).resolve()
    canonical_root = _canonical_report_root()
    if reports_root != canonical_root:
        raise RequestValidationError(
            f"reports_root debe ser la raiz canonica {canonical_root}"
        )
    if kind not in TERMINAL_FILENAMES:
        raise RequestValidationError("kind no es valido")
    source_text = str(payload.get("source_path") or "").strip()
    if not source_text:
        raise RequestValidationError("source_path es obligatorio")
    source = Path(source_text).resolve(strict=False)
    if not _path_inside_allowed(source):
        raise RequestValidationError("source_path queda fuera de los volumenes permitidos")

    callback = _validated_callback(payload.get("callback_url"), job_id)
    common_payload = {
        **payload,
        "job_id": job_id,
        "source_path": str(source),
        "reports_root": str(canonical_root),
        "callback_url": callback,
    }
    if kind == "bluray":
        return _validated_normalize_payload(common_payload)

    movies_field = "movies_root" if kind == "trailer" else "final_root"
    movies_text = str(payload.get(movies_field) or "").strip()
    review_text = str(payload.get("review_root") or "").strip()
    if not movies_text or Path(movies_text).resolve() != _configured_path(
        MOVIES_ROOT_ENV, DEFAULT_MOVIES_ROOT
    ):
        raise RequestValidationError(f"{movies_field} no es la raiz canonica")
    if not review_text or Path(review_text).resolve() != _configured_path(
        REVIEW_ROOT_ENV, DEFAULT_REVIEW_ROOT
    ):
        raise RequestValidationError("review_root no es la raiz canonica")
    return {
        **common_payload,
        movies_field: str(_configured_path(MOVIES_ROOT_ENV, DEFAULT_MOVIES_ROOT)),
        "review_root": str(_configured_path(REVIEW_ROOT_ENV, DEFAULT_REVIEW_ROOT)),
    }


def _validated_normalize_payload(payload: dict) -> dict:
    source_text = str(payload.get("source_path") or "").strip()
    if not source_text:
        raise RequestValidationError("source_path es obligatorio")
    try:
        source = Path(source_text).resolve(strict=True)
    except OSError as error:
        raise RequestValidationError("source_path no existe") from error
    if not source.is_dir():
        raise RequestValidationError("source_path debe ser una carpeta")
    if not any(source == root or source.is_relative_to(root) for root in _allowed_roots()):
        raise RequestValidationError("source_path queda fuera de los volumenes permitidos")
    return {**payload, "source_path": str(source)}


def _terminal_path(kind: str, job_id: str) -> Path:
    return _canonical_report_root() / job_id / TERMINAL_FILENAMES[kind]


def _safe_error_text(error: BaseException) -> str:
    text = str(error).strip() or error.__class__.__name__
    replacements = [(_canonical_report_root(), "<REPORT_ROOT>")]
    replacements.extend((root, "<DATA>") for root in _allowed_roots())
    for root, alias in replacements:
        root_text = str(root)
        if root_text:
            text = text.replace(root_text, alias)
    text = re.sub(
        r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
        "Authorization: <REDACTED>",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer <REDACTED>", text)
    text = re.sub(
        r"(?i)(token|password|passwd|secret|auth)\s*[:=]\s*([^\s&,;]+)",
        r"\1=<REDACTED>",
        text,
    )
    text = re.sub(
        r"(?i)\bdownload_url\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        "download_url=<REDACTED>",
        text,
    )
    text = re.sub(r"(?i)magnet:\?[^\s]+", "<MAGNET_REDACTED>", text)
    text = re.sub(r"(?i)https?://[^\s]+", "<URL_REDACTED>", text)
    text = re.sub(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s,;]+", "<PATH_REDACTED>", text)
    text = re.sub(r"(?<![>\w])/(?:[^/\s]+/)*[^\s,;]*", "<PATH_REDACTED>", text)
    text = re.sub(r"[\x00-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_ERROR_LENGTH]


def _typed_error(
    error_code: str,
    message: str,
    *,
    kind: Optional[str] = None,
    job_id: Optional[str] = None,
    retryable: bool = False,
) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "status": "error",
        "error_code": error_code,
        "error": message[:MAX_ERROR_LENGTH],
        "retryable": retryable,
    }
    if kind:
        payload["kind"] = kind
    if job_id:
        payload["job_id"] = job_id
    return payload


def _load_terminal(kind: str, job_id: str) -> Optional[Dict[str, object]]:
    path = _terminal_path(kind, job_id)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > MAX_TERMINAL_BYTES:
            raise TerminalStateError("El resultado terminal supera el limite permitido")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise TerminalStateError("El resultado terminal no se puede leer") from error
    stored_status = payload.get("status") if isinstance(payload, dict) else None
    status = stored_status.strip() if isinstance(stored_status, str) else ""
    valid_status = bool(status) and (
        kind == "bluray" or status in TERMINAL_STATUSES
    )
    if not isinstance(payload, dict) or not valid_status:
        raise TerminalStateError("El resultado terminal no tiene un estado valido")
    stored_job_id = str(payload.get("job_id") or "")
    if stored_job_id and stored_job_id != job_id:
        raise TerminalStateError("El resultado terminal pertenece a otro trabajo")
    stored_kind = str(payload.get("kind") or "")
    if stored_kind and stored_kind != kind:
        raise TerminalStateError("El resultado terminal pertenece a otro tipo de trabajo")
    return payload


def _write_terminal_atomic(kind: str, job_id: str, payload: Dict[str, object]) -> Path:
    path = _terminal_path(kind, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return path


class MediaJobRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: Dict[Tuple[str, str], Dict[str, object]] = {}

    def claim(
        self, kind: str, job_id: str
    ) -> Tuple[str, Optional[Dict[str, object]]]:
        key = (kind, job_id)
        with self._lock:
            active = self._active.get(key)
            if active is not None:
                return "active", dict(active)
            terminal = _load_terminal(kind, job_id)
            if terminal is not None:
                return "terminal", terminal
            self._active[key] = {
                "kind": kind,
                "job_id": job_id,
                "started_at": time.time(),
            }
            return "claimed", None

    def release(self, kind: str, job_id: str) -> None:
        with self._lock:
            self._active.pop((kind, job_id), None)

    def status(
        self, kind: str, job_id: str
    ) -> Tuple[str, Optional[Dict[str, object]]]:
        key = (kind, job_id)
        with self._lock:
            active = self._active.get(key)
            if active is not None:
                return "active", dict(active)
            terminal = _load_terminal(kind, job_id)
            if terminal is not None:
                return "terminal", terminal
            return "not_found", None


JOB_REGISTRY = MediaJobRegistry()


def _processor(kind: str):
    if kind == "movie":
        return process_movie
    if kind == "trailer":
        return process_trailer
    return normalize_bluray


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        if urlsplit(self.path).path == "/health":
            return
        print(fmt % args, flush=True)

    def _json(self, status: int, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _read_payload(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as error:
            raise RequestValidationError("Content-Length no es valido") from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise RequestValidationError("El payload supera el limite permitido")
        data = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestValidationError("El JSON no es valido") from error
        if not isinstance(payload, dict):
            raise RequestValidationError("El payload debe ser un objeto JSON")
        return payload

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._json(200, {"status": "ok"})
            return
        match = re.fullmatch(r"/jobs/([^/]+)/status", parsed.path)
        if not match:
            self._json(404, {"error": "not_found"})
            return
        try:
            job_id = _validated_job_id(unquote(match.group(1)))
            query = parse_qs(parsed.query, keep_blank_values=True)
            kinds = query.get("kind", [])
            if len(kinds) != 1 or kinds[0] not in TERMINAL_FILENAMES:
                raise RequestValidationError("kind debe ser movie, trailer o bluray")
            kind = kinds[0]
            state, payload = JOB_REGISTRY.status(kind, job_id)
        except RequestValidationError as error:
            self._json(
                400,
                _typed_error("media_invalid_request", _safe_error_text(error)),
            )
            return
        except TerminalStateError as error:
            self._json(
                500,
                _typed_error(
                    "media_terminal_invalid",
                    _safe_error_text(error),
                    kind=kind,
                    job_id=job_id,
                ),
            )
            return
        if state == "active":
            self._json(
                200,
                {
                    "ok": True,
                    "status": "active",
                    "kind": kind,
                    "job_id": job_id,
                    "started_at": payload.get("started_at") if payload else None,
                },
            )
            return
        if state == "terminal":
            self._json(
                200,
                {
                    "ok": True,
                    "status": "terminal",
                    "kind": kind,
                    "job_id": job_id,
                    "result": payload,
                },
            )
            return
        self._json(
            404,
            {
                "ok": False,
                "status": "not_found",
                "error_code": "media_job_not_found",
                "error": "No existe estado para este trabajo.",
                "retryable": False,
                "kind": kind,
                "job_id": job_id,
            },
        )

    def _run_idempotent(self, kind: str, raw_payload: object) -> None:
        try:
            payload = _validated_job_payload(raw_payload, kind)
            job_id = str(payload["job_id"])
            state, existing = JOB_REGISTRY.claim(kind, job_id)
        except RequestValidationError as error:
            self._json(
                400,
                _typed_error("media_invalid_request", _safe_error_text(error)),
            )
            return
        except TerminalStateError as error:
            self._json(
                500,
                _typed_error(
                    "media_terminal_invalid",
                    _safe_error_text(error),
                    kind=kind,
                    job_id=str(raw_payload.get("job_id") or "")
                    if isinstance(raw_payload, dict)
                    else None,
                ),
            )
            return

        if state == "terminal":
            self._json(500 if existing and existing.get("status") == "error" else 200, existing)
            return
        if state == "active":
            self._json(
                409,
                {
                    "status": "active",
                    "error_code": "media_job_active",
                    "error": "El trabajo ya esta activo.",
                    "retryable": True,
                    "kind": kind,
                    "job_id": job_id,
                    "started_at": existing.get("started_at") if existing else None,
                },
            )
            return

        response_status = 200
        try:
            raw_result = _processor(kind)(payload)
            raw_status_value = raw_result.get("status") if isinstance(raw_result, dict) else None
            raw_status = raw_status_value.strip() if isinstance(raw_status_value, str) else ""
            valid_result = bool(raw_status) and (
                kind == "bluray" or raw_status in {"done", "review"}
            )
            if not isinstance(raw_result, dict) or not valid_result:
                raise RuntimeError("Media Worker devolvio un resultado terminal no valido")
            result = dict(raw_result)
            result["job_id"] = job_id
            result["kind"] = kind
            _write_terminal_atomic(kind, job_id, result)
            if raw_status == "error":
                response_status = 500
        except Exception as error:
            response_status = 500
            result = _typed_error(
                "media_worker_failed",
                _safe_error_text(error),
                kind=kind,
                job_id=job_id,
                retryable=False,
            )
            try:
                _write_terminal_atomic(kind, job_id, result)
            except Exception as persist_error:
                result = _typed_error(
                    "media_terminal_persist_failed",
                    _safe_error_text(persist_error),
                    kind=kind,
                    job_id=job_id,
                    retryable=False,
                )
        finally:
            JOB_REGISTRY.release(kind, job_id)
        self._json(response_status, result)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            payload = self._read_payload()
        except RequestValidationError as error:
            self._json(
                400,
                _typed_error("media_invalid_request", _safe_error_text(error)),
            )
            return
        if parsed.path == "/process-movie":
            self._run_idempotent("movie", payload)
            return
        if parsed.path == "/process-trailer":
            self._run_idempotent("trailer", payload)
            return
        if parsed.path == "/normalize-bluray":
            self._run_idempotent("bluray", payload)
            return
        self._json(404, {"error": "not_found"})


def main() -> int:
    port = int(os.environ.get("MEDIA_WORKER_PORT", "8790") or "8790")
    http_server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"media-worker iniciado en puerto {port}", flush=True)
    http_server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
