import json
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .identity_proxy import IdentityProxy
from .source_context_proxy import (
    MAX_REQUEST_BODY_BYTES as SOURCE_CONTEXT_MAX_BODY_BYTES,
    SourceContextProxy,
    read_source_context_token,
)


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
STATIC_DIR = WEB_DIR / "static"

RULES_PATH = Path(os.environ.get("MEDIA_RULES_PATH", "/config/media-rules/reglas_motor.json"))
DEFAULT_RULES_PATH = Path(os.environ.get("MEDIA_DEFAULT_RULES_PATH", "/defaults/reglas_motor_default.json"))
REPORT_ROOT = Path(os.environ.get("MEDIA_REPORT_ROOT", "/config/media-worker"))
REVIEW_DIR = Path(os.environ.get("MEDIA_REVIEW_DIR", "/data/media/repetidas_vs_error"))
COMPLETE_ROOT = Path(os.environ.get("ARR_COMPLETE_ROOT", "/data/downloads/torrents/complete"))
MOVIES_ROOT = Path(os.environ.get("ARR_MOVIES_ROOT", "/data/media/movies"))
TV_ROOT = Path(os.environ.get("ARR_TV_ROOT", "/data/media/tv"))
ORCH_URL = os.environ.get("ARR_ORCHESTRATOR_URL", "http://arr-orchestrator:8787").rstrip("/")
WORKER_URL = os.environ.get("MEDIA_WORKER_URL", "http://media-worker:8790").rstrip("/")
CODEX_DIAG_ROOT = Path(os.environ.get("CODEX_DIAG_ROOT", "/diagnosticos_codex"))
IDENTITY_PROXY = IdentityProxy(ORCH_URL)
SOURCE_CONTEXT_PROXY = SourceContextProxy(ORCH_URL, read_source_context_token())
MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024


class PayloadTooLargeError(ValueError):
    pass


class InvalidContentLengthError(ValueError):
    pass


class InvalidJsonPayloadError(ValueError):
    pass


def _is_application_json(headers: object) -> bool:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return False
    content_type = str(getter("Content-Type", "") or "")
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def _same_origin_request(headers: object) -> bool:
    """Bloquea navegadores cross-site sin exigir cabeceras a clientes de API."""

    getter = getattr(headers, "get", None)
    if not callable(getter):
        return True
    fetch_site = str(getter("Sec-Fetch-Site", "") or "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    host = str(getter("Host", "") or "").strip().lower()
    origin = str(getter("Origin", "") or "").strip()
    referer = str(getter("Referer", "") or "").strip()
    source = origin or referer
    if not source:
        return True
    try:
        parsed = urllib.parse.urlsplit(source)
    except ValueError:
        return False
    return bool(
        host
        and parsed.scheme.lower() in {"http", "https"}
        and parsed.netloc.lower() == host
    )

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".zip": "application/zip",
}

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _safe_child(root: Path, value: str) -> Optional[Path]:
    try:
        root = root.resolve()
        target = (root / value).resolve()
        target.relative_to(root)
        return target
    except (OSError, ValueError):
        return None


def _upstream_json(url: str, timeout: int = 8) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return {"ok": False, "error": str(error)}


def _upstream_post_json(url: str, payload: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return {"ok": False, "error": str(error)}


def _proxy_upstream_json(
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 20,
) -> Tuple[int, Dict[str, Any]]:
    """Proxy JSON conservando el estado y el cuerpo util del upstream."""
    request: Any = url
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = int(error.code or 502)
        try:
            raw = error.read()
        finally:
            error.close()
    except Exception as error:
        return 502, {"ok": False, "error": str(error), "upstream_status": 502}

    try:
        decoded = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        text = raw.decode("utf-8", errors="replace").strip()
        return status, {
            "ok": False,
            "error": text or "Respuesta no valida del orquestador.",
            "upstream_status": status,
        }
    if isinstance(decoded, dict):
        return status, decoded
    return status, {
        "ok": False,
        "error": "Respuesta no valida del orquestador.",
        "upstream_status": status,
        "upstream_body": decoded,
    }


def _count_children(path: Path) -> int:
    try:
        return sum(1 for _ in path.iterdir()) if path.is_dir() else 0
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _short_text(path: Path, limit: int = 5000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:limit]
    except OSError:
        return ""


def _codex_bucket_label(bucket: str) -> str:
    return {
        "movies": "Peliculas",
        "tv": "Series",
        "trailers": "Trailers",
        "repetidas_vs_error": "Repetidas / Error",
    }.get(bucket or "", "Sin clasificar")


def _codex_zip_metadata(path: Path) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open("job.json") as handle:
                metadata = json.loads(handle.read().decode("utf-8"))
    except Exception:
        metadata = {}
    return metadata


def _codex_diagnostics_payload(limit: int = 80) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    if CODEX_DIAG_ROOT.is_dir():
        candidates = [
            path
            for path in CODEX_DIAG_ROOT.rglob("*.zip")
            if path.is_file() and not path.name.startswith(".")
        ]
        for path in sorted(candidates, key=_mtime, reverse=True)[:limit]:
            rel = str(path.relative_to(CODEX_DIAG_ROOT)).replace("\\", "/")
            folder = path.parent.name if path.parent != CODEX_DIAG_ROOT else ""
            job = _codex_zip_metadata(path)
            display_name = str(job.get("name") or path.stem.replace("_informe_codex", ""))
            category = str(job.get("category") or "")
            state = str(job.get("state") or "")
            files.append(
                {
                    "name": path.name,
                    "relative": rel,
                    "folder": folder,
                    "folder_label": _codex_bucket_label(folder) if folder else "Antiguos",
                    "display_name": display_name,
                    "category": category,
                    "state": state,
                    "updated_at": job.get("updated_at"),
                    "size": path.stat().st_size,
                    "mtime": _mtime(path),
                    "download_url": f"/api/codex-diagnostic?file={urllib.parse.quote(rel)}",
                }
            )
    return {"ok": True, "root": str(CODEX_DIAG_ROOT), "files": files}


def _create_codex_diagnostic(job_id: str) -> Dict[str, Any]:
    if not job_id:
        return {"ok": False, "error": "job_id_vacio"}
    result = _upstream_post_json(
        f"{ORCH_URL}/jobs/{urllib.parse.quote(job_id)}/diagnostic",
        {"force": False},
        timeout=60,
    )
    if result.get("ok") and result.get("relative") and not result.get("download_url"):
        result["download_url"] = (
            f"/api/codex-diagnostic?file={urllib.parse.quote(str(result.get('relative')))}"
        )
    return result


def _media_rules_payload() -> Tuple[int, Dict[str, Any]]:
    return _proxy_upstream_json(f"{WORKER_URL}/settings/rules", timeout=8)


def _save_media_rules(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    return _proxy_upstream_json(
        f"{WORKER_URL}/settings/rules",
        payload,
        timeout=20,
    )


def _watcher_rules_payload() -> Dict[str, Any]:
    return _upstream_json(f"{ORCH_URL}/settings/watcher")


def _save_watcher_rules(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _upstream_post_json(f"{ORCH_URL}/settings/watcher", payload)


def _status_payload() -> Dict[str, Any]:
    orch = _upstream_json(f"{ORCH_URL}/health")
    worker = _upstream_json(f"{WORKER_URL}/health")
    return {
        "ok": True,
        "orchestrator": orch,
        "media_worker": worker,
        "paths": {
            "rules": {"path": str(RULES_PATH), "exists": RULES_PATH.exists()},
            "defaults": {"path": str(DEFAULT_RULES_PATH), "exists": DEFAULT_RULES_PATH.exists()},
            "reports": {"path": str(REPORT_ROOT), "exists": REPORT_ROOT.exists()},
            "review": {"path": str(REVIEW_DIR), "exists": REVIEW_DIR.exists(), "items": _count_children(REVIEW_DIR)},
            "movies_final": {"path": str(MOVIES_ROOT), "exists": MOVIES_ROOT.exists(), "items": _count_children(MOVIES_ROOT)},
            "tv_final": {"path": str(TV_ROOT), "exists": TV_ROOT.exists(), "items": _count_children(TV_ROOT)},
            "movies_automatizacion": {
                "path": str(COMPLETE_ROOT / "movies_automatizacion"),
                "exists": (COMPLETE_ROOT / "movies_automatizacion").exists(),
                "items": _count_children(COMPLETE_ROOT / "movies_automatizacion"),
            },
            "trailers_automatizacion": {
                "path": str(COMPLETE_ROOT / "trailers_automatizacion"),
                "exists": (COMPLETE_ROOT / "trailers_automatizacion").exists(),
                "items": _count_children(COMPLETE_ROOT / "trailers_automatizacion"),
            },
        },
    }


def _jobs_payload() -> Dict[str, Any]:
    jobs = _upstream_json(f"{ORCH_URL}/jobs", timeout=12)
    if isinstance(jobs, list):
        return {"ok": True, "jobs": jobs}
    return {"ok": False, "jobs": [], "error": jobs.get("error", "No se pudo leer jobs.")}


def _job_detail_payload(job_id: str) -> Dict[str, Any]:
    if not job_id:
        return {"ok": False, "error": "job_id_vacio"}
    detail = _upstream_json(f"{ORCH_URL}/jobs/{urllib.parse.quote(job_id)}", timeout=12)
    if isinstance(detail, dict):
        return detail
    return {"ok": False, "error": "No se pudo leer detalle del job."}


def _job_follow_payload(job_id: str) -> Dict[str, Any]:
    if not job_id:
        return {"ok": False, "error": "job_id_vacio"}
    detail = _upstream_json(
        f"{ORCH_URL}/jobs/{urllib.parse.quote(job_id)}/follow",
        timeout=12,
    )
    if isinstance(detail, dict):
        return detail
    return {"ok": False, "error": "No se pudo leer seguimiento del job."}


def _review_payload(limit: int = 80) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    if REVIEW_DIR.is_dir():
        for folder in sorted(REVIEW_DIR.iterdir(), key=_mtime, reverse=True)[:limit]:
            if not folder.is_dir():
                continue
            txts = sorted(folder.glob("*.txt"))
            reason_json = folder / "reason.json"
            payload = _read_json(reason_json)
            items.append(
                {
                    "name": folder.name,
                    "path": str(folder),
                    "mtime": _mtime(folder),
                    "reason_file": txts[0].name if txts else "",
                    "reason_text": _short_text(txts[0], 2000) if txts else "",
                    "phase": payload.get("phase", ""),
                    "job_id": payload.get("job_id", ""),
                    "file_count": _count_children(folder),
                }
            )
    return {"ok": True, "review_dir": str(REVIEW_DIR), "items": items}


def _reports_payload(limit: int = 120) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    roots = [REPORT_ROOT / "runtime", REPORT_ROOT / "logs", REPORT_ROOT]
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=_mtime, reverse=True):
            if len(files) >= limit:
                break
            if not path.is_file():
                continue
            if any(part in {"temp", "backups"} for part in path.relative_to(REPORT_ROOT).parts):
                continue
            rel = str(path.relative_to(REPORT_ROOT))
            if rel in seen:
                continue
            seen.add(rel)
            files.append(
                {
                    "name": path.name,
                    "relative": rel,
                    "size": path.stat().st_size,
                    "mtime": _mtime(path),
                    "kind": path.suffix.lower().lstrip(".") or "file",
                }
            )
    return {"ok": True, "report_root": str(REPORT_ROOT), "files": files}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _download(self, path: Path) -> None:
        content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _read_payload(self, *, strict: bool = False) -> Dict[str, Any]:
        length = self._content_length()
        if length > MAX_REQUEST_BODY_BYTES:
            raise PayloadTooLargeError
        if strict and length == 0:
            raise InvalidJsonPayloadError
        data = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(data.decode("utf-8"))
            if isinstance(payload, dict):
                return payload
            if strict:
                raise InvalidJsonPayloadError
            return {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            if strict:
                raise InvalidJsonPayloadError from error
            return {}

    def _content_length(self) -> int:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError) as error:
            raise InvalidContentLengthError from error
        if length < 0:
            raise InvalidContentLengthError
        return length

    def _static(self, path: str) -> None:
        rel = path.removeprefix("/static/").strip("/")
        target = _safe_child(STATIC_DIR, rel)
        if not target or not target.is_file():
            self._send(404, b"No encontrado.", "text/plain; charset=utf-8")
            return
        content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), content_type)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            self._json(200, {"status": "ok"})
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/api/status":
            self._json(200, _status_payload())
            return
        if path == "/api/jobs":
            self._json(200, _jobs_payload())
            return
        if path.startswith("/api/jobs/"):
            suffix = urllib.parse.unquote(path.removeprefix("/api/jobs/")).strip("/")
            if suffix.endswith("/follow"):
                job_id = suffix.removesuffix("/follow").strip("/")
                detail = _job_follow_payload(job_id)
            else:
                detail = _job_detail_payload(suffix)
            self._json(200 if detail.get("ok") else 404, detail)
            return
        if path == "/api/rules":
            status, result = _media_rules_payload()
            self._json(status, result)
            return
        if path == "/api/watcher-rules":
            result = _watcher_rules_payload()
            self._json(200 if result.get("ok") else 502, result)
            return
        if path == "/api/identity-rules":
            status, result = IDENTITY_PROXY.get_rules()
            self._json(status, result)
            return
        if path == "/api/review":
            self._json(200, _review_payload())
            return
        if path == "/api/reports":
            self._json(200, _reports_payload())
            return
        if path == "/api/codex-diagnostics":
            self._json(200, _codex_diagnostics_payload())
            return
        if path == "/api/codex-diagnostic":
            rel = query.get("file", [""])[0]
            target = _safe_child(CODEX_DIAG_ROOT, rel)
            if not target or not target.is_file() or target.suffix.lower() != ".zip":
                self._send(404, b"No hay informe Codex.", "text/plain; charset=utf-8")
                return
            self._download(target)
            return
        if path == "/api/report":
            rel = query.get("file", [""])[0]
            target = _safe_child(REPORT_ROOT, rel)
            if not target or not target.is_file():
                self._send(404, b"No hay informe.", "text/plain; charset=utf-8")
                return
            self._send(200, _short_text(target, 512000).encode("utf-8"), "text/plain; charset=utf-8")
            return
        if path.startswith("/static/"):
            self._static(path)
            return
        if path == "/" or path == "/index.html":
            self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        self._send(404, b"No encontrado.", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = self._content_length()
        except InvalidContentLengthError:
            self._json(
                400,
                {
                    "ok": False,
                    "error": "invalid_request",
                    "message": "Content-Length no es valido.",
                },
            )
            return
        if length > MAX_REQUEST_BODY_BYTES:
            self._json(
                413,
                {
                    "ok": False,
                    "error": "payload_too_large",
                    "message": "El JSON supera el limite de 4 MB.",
                },
            )
            return
        if parsed.path == "/api/source-context/events":
            if not SOURCE_CONTEXT_PROXY.enabled:
                self._json(
                    503,
                    {
                        "ok": False,
                        "error": "source_context_disabled",
                        "message": "La recepcion de contexto de origen no esta configurada.",
                    },
                )
                return
            if not SOURCE_CONTEXT_PROXY.authorized(self.headers.get("Authorization")):
                self._json(
                    401,
                    {
                        "ok": False,
                        "error": "unauthorized",
                        "message": "Bearer no valido.",
                    },
                )
                return
            if not _is_application_json(self.headers):
                self._json(
                    415,
                    {
                        "ok": False,
                        "error": "unsupported_media_type",
                        "message": "Content-Type debe ser application/json.",
                    },
                )
                return
            if length > SOURCE_CONTEXT_MAX_BODY_BYTES:
                self._json(
                    413,
                    {
                        "ok": False,
                        "error": "payload_too_large",
                        "message": "El JSON supera el limite de 16 KB.",
                    },
                )
                return
            try:
                status, result = SOURCE_CONTEXT_PROXY.post_event(
                    self._read_payload(strict=True)
                )
                self._json(status, result)
            except InvalidJsonPayloadError:
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_json",
                        "message": "El cuerpo debe ser un objeto JSON valido.",
                    },
                )
            except Exception:
                self._json(
                    500,
                    {
                        "ok": False,
                        "error": "source_context_proxy_failed",
                        "message": "No se pudo registrar el contexto de origen.",
                    },
                )
            return
        if parsed.path == "/api/rules":
            try:
                status, result = _save_media_rules(self._read_payload())
                self._json(status, result)
            except Exception as error:
                self._json(500, {"ok": False, "error": str(error)})
            return
        if parsed.path == "/api/watcher-rules":
            try:
                result = _save_watcher_rules(self._read_payload())
                self._json(200 if result.get("ok") else 400, result)
            except Exception as error:
                self._json(500, {"ok": False, "error": str(error)})
            return
        identity_actions = {
            "/api/identity-rules": IDENTITY_PROXY.save_rules,
            "/api/identity-rules/reset": IDENTITY_PROXY.reset_rules,
            "/api/identity-rules/cache/clear": IDENTITY_PROXY.clear_cache,
            "/api/identity-rules/test-parser": IDENTITY_PROXY.test_parser,
            "/api/identity-rules/test-resolver": IDENTITY_PROXY.test_resolver,
        }
        identity_action = identity_actions.get(parsed.path)
        if identity_action:
            if not _same_origin_request(self.headers):
                self._json(
                    403,
                    {
                        "ok": False,
                        "error": "cross_origin_request",
                        "message": "La petición debe proceder del panel ARR.",
                    },
                )
                return
            if not _is_application_json(self.headers):
                self._json(
                    415,
                    {
                        "ok": False,
                        "error": "unsupported_media_type",
                        "message": "Content-Type debe ser application/json.",
                    },
                )
                return
            try:
                status, result = identity_action(self._read_payload(strict=True))
                self._json(status, result)
            except InvalidJsonPayloadError:
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_json",
                        "message": "El cuerpo debe ser un objeto JSON válido.",
                    },
                )
            except Exception as error:
                self._json(500, {"ok": False, "error": type(error).__name__})
            return
        if parsed.path == "/api/codex-diagnostic":
            try:
                payload = self._read_payload()
                result = _create_codex_diagnostic(str(payload.get("job_id") or "").strip())
                self._json(200 if result.get("ok") else 404, result)
            except Exception as error:
                self._json(500, {"ok": False, "error": str(error)})
            return
        self._json(404, {"ok": False, "error": "Ruta no reconocida."})


def main() -> int:
    port = int(os.environ.get("MEDIA_PANEL_PORT", "8080") or "8080")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"media-panel iniciado en puerto {port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
