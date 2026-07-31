import copy
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .identity_proxy import IdentityProxy


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
STATIC_DIR = WEB_DIR / "static"

RULES_PATH = Path(os.environ.get("MEDIA_RULES_PATH", "/config/media-rules/reglas_motor.json"))
DEFAULT_RULES_PATH = Path(os.environ.get("MEDIA_DEFAULT_RULES_PATH", "/defaults/reglas_motor_default.json"))
REPORT_ROOT = Path(os.environ.get("MEDIA_REPORT_ROOT", "/config/media-worker"))
REVIEW_DIR = Path(os.environ.get("MEDIA_REVIEW_DIR", "/data/media/repetidas_vs_error"))
SERIES_REPORT_ROOT = Path("/config/series-worker")
SERIES_REVIEW_DIR = Path("/data/media/repetidas_vs_error_series")
SERIES_RULES_PATH = Path(
    os.environ.get("SERIES_RULES_PATH", "/config/series-rules/reglas_series.json")
)
SERIES_REPORT_ALIAS = "<CONFIG>/series-worker"
SERIES_REVIEW_ALIAS = "<DATA_MEDIA>/repetidas_vs_error_series"
SERIES_RULES_ALIAS = "<CONFIG>/series-rules/reglas_series.json"
COMPLETE_ROOT = Path(os.environ.get("ARR_COMPLETE_ROOT", "/data/downloads/torrents/complete"))
MOVIES_ROOT = Path(os.environ.get("ARR_MOVIES_ROOT", "/data/media/movies"))
TV_ROOT = Path(os.environ.get("ARR_TV_ROOT", "/data/media/tv"))
ORCH_URL = os.environ.get("ARR_ORCHESTRATOR_URL", "http://arr-orchestrator:8787").rstrip("/")
WORKER_URL = os.environ.get("MEDIA_WORKER_URL", "http://media-worker:8790").rstrip("/")
SERIES_WORKER_URL = os.environ.get(
    "SERIES_WORKER_URL", "http://series-worker:8791"
).rstrip("/")
CODEX_DIAG_ROOT = Path(os.environ.get("CODEX_DIAG_ROOT", "/diagnosticos_codex"))
IDENTITY_PROXY = IdentityProxy(ORCH_URL)
MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
IDENTITY_PROFILES = frozenset({"common", "movies", "tv"})
MOVIE_RULE_BLOCKS = ("entrada", "video", "audio", "subtitulos", "limpieza")
TRAILER_RULE_BLOCKS = ("trailers",)
SERIES_NOT_CONNECTED_MESSAGE = "Motor de series no conectado"
JOB_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
SERIES_EVIDENCE_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:s\d{1,2}e\d{1,3}|season\s*\d+|temporada\s*\d+|series?)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
MOVIE_EVIDENCE_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:pel[ií]cula|movies?|trailers?|(?:19|20)\d{2})(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
MAX_PROFILE_EVIDENCE_PATHS = 200
MAX_REPORT_METADATA_BYTES = 512 * 1024
SERIES_JOB_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SERIES_TECHNICAL_REPORT_FILES = frozenset(
    {
        "journal.json",
        "journal.jsonl",
        "manifest.json",
        "request.json",
        "rules_snapshot.json",
        "series_result.json",
    }
)
SERIES_RESERVED_REPORT_DIRS = frozenset({"backups", "logs", "runtime", "temp"})


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


def _read_protected_json_object(handler: object) -> Optional[Dict[str, Any]]:
    """Aplica a los POST nuevos el mismo contrato seguro de Identidad."""

    headers = getattr(handler, "headers", {})
    send_json = getattr(handler, "_json")
    if not _same_origin_request(headers):
        send_json(
            403,
            {
                "ok": False,
                "error": "cross_origin_request",
                "message": "La petición debe proceder del panel ARR.",
            },
        )
        return None
    if not _is_application_json(headers):
        send_json(
            415,
            {
                "ok": False,
                "error": "unsupported_media_type",
                "message": "Content-Type debe ser application/json.",
            },
        )
        return None
    try:
        return getattr(handler, "_read_payload")(strict=True)
    except InvalidJsonPayloadError:
        send_json(
            400,
            {
                "ok": False,
                "error": "invalid_json",
                "message": "El cuerpo debe ser un objeto JSON válido.",
            },
        )
        return None

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


def _safe_regular_child(root: Path, value: str) -> Optional[Path]:
    normalized = str(value or "").replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    try:
        if root.is_symlink():
            return None
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            return None
        candidate = root
        for part in parts:
            candidate /= part
            if candidate.is_symlink():
                return None
        target = candidate.resolve(strict=True)
        target.relative_to(resolved_root)
        return target if target.is_file() else None
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


def _codex_diagnostics_payload(
    limit: int = 80,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    if CODEX_DIAG_ROOT.is_dir():
        candidates = [
            path
            for path in CODEX_DIAG_ROOT.rglob("*.zip")
            if path.is_file() and not path.name.startswith(".")
        ]
        for path in sorted(candidates, key=_mtime, reverse=True):
            if len(files) >= limit:
                break
            rel = str(path.relative_to(CODEX_DIAG_ROOT)).replace("\\", "/")
            folder = path.parent.name if path.parent != CODEX_DIAG_ROOT else ""
            job = _codex_zip_metadata(path)
            display_name = str(job.get("name") or path.stem.replace("_informe_codex", ""))
            category = str(job.get("category") or "")
            state = str(job.get("state") or "")
            item_profile = _profile_from_metadata(job)
            if item_profile is None:
                item_profile = "series" if folder == "tv" else "movies"
            if profile is not None and item_profile != profile:
                continue
            files.append(
                {
                    "name": path.name,
                    "relative": rel,
                    "folder": folder,
                    "folder_label": _codex_bucket_label(folder) if folder else "Antiguos",
                    "display_name": display_name,
                    "category": category,
                    "profile": item_profile,
                    "state": state,
                    "updated_at": job.get("updated_at"),
                    "size": path.stat().st_size,
                    "mtime": _mtime(path),
                    "download_url": f"/api/codex-diagnostic?file={urllib.parse.quote(rel)}",
                }
            )
    result: Dict[str, Any] = {
        "ok": True,
        "root": str(CODEX_DIAG_ROOT),
        "files": files,
    }
    if profile is not None:
        result["profile"] = profile
    return result


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


def _rules_block_view(
    rules: object,
    blocks: Tuple[str, ...],
    *,
    fill_missing: bool = False,
) -> Dict[str, Any]:
    source = rules if isinstance(rules, dict) else {}
    return {
        block: copy.deepcopy(source.get(block, {}))
        for block in blocks
        if fill_missing or block in source
    }


def _scoped_media_rules_payload(
    payload: Dict[str, Any],
    profile: str,
    blocks: Tuple[str, ...],
    *,
    connected: bool = True,
    editable: bool = True,
) -> Dict[str, Any]:
    result = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    for field in ("rules", "active", "defaults"):
        if field in result:
            result[field] = _rules_block_view(result.get(field), blocks)
    result.update(
        {
            "profile": profile,
            "connected": connected,
            "editable": editable,
        }
    )
    return result


def _media_rules_profile_payload(
    profile: str,
    blocks: Tuple[str, ...],
) -> Tuple[int, Dict[str, Any]]:
    status, payload = _media_rules_payload()
    return status, _scoped_media_rules_payload(
        payload,
        profile,
        blocks,
        connected=status < 500,
        editable=status < 400,
    )


def _invalid_scoped_rules(profile: str, message: str) -> Tuple[int, Dict[str, Any]]:
    return 400, {
        "ok": False,
        "error": "invalid_rules",
        "message": message,
        "profile": profile,
    }


def _save_media_rules_profile(
    payload: Dict[str, Any],
    profile: str,
    blocks: Tuple[str, ...],
) -> Tuple[int, Dict[str, Any]]:
    unexpected_payload = (
        sorted(set(payload) - {"rules", "expected_fingerprint"})
        if isinstance(payload, dict)
        else []
    )
    if unexpected_payload:
        return _invalid_scoped_rules(
            profile,
            "Campos fuera del contrato: " + ", ".join(unexpected_payload),
        )
    requested_rules = payload.get("rules") if isinstance(payload, dict) else None
    expected = payload.get("expected_fingerprint") if isinstance(payload, dict) else None
    if not isinstance(requested_rules, dict):
        return _invalid_scoped_rules(profile, "rules debe ser un objeto.")
    unexpected = sorted(set(requested_rules) - set(blocks))
    if unexpected:
        return _invalid_scoped_rules(
            profile,
            "Bloques fuera del perfil: " + ", ".join(unexpected),
        )
    if not isinstance(expected, str) or not expected:
        return _invalid_scoped_rules(
            profile,
            "expected_fingerprint es obligatorio.",
        )

    current_status, current = _media_rules_payload()
    if current_status >= 400:
        return current_status, _scoped_media_rules_payload(
            current,
            profile,
            blocks,
            connected=current_status < 500,
            editable=False,
        )
    current_rules = current.get("rules") if isinstance(current, dict) else None
    current_fingerprint = current.get("fingerprint") if isinstance(current, dict) else None
    if not isinstance(current_rules, dict) or not isinstance(current_fingerprint, str):
        return 502, {
            "ok": False,
            "error": "invalid_upstream_response",
            "message": "Media Worker no devolvio reglas completas con huella.",
            "profile": profile,
            "connected": False,
            "editable": False,
        }
    if expected != current_fingerprint:
        conflict = _scoped_media_rules_payload(
            current,
            profile,
            blocks,
            connected=True,
            editable=True,
        )
        conflict.update(
            {
                "ok": False,
                "error": "fingerprint_conflict",
                "message": "Las reglas cambiaron; recarga antes de guardar.",
                "expected_fingerprint": expected,
                "current_fingerprint": current_fingerprint,
            }
        )
        return 409, conflict

    merged_rules = copy.deepcopy(current_rules)
    for block, value in requested_rules.items():
        merged_rules[block] = copy.deepcopy(value)
    saved_status, saved = _save_media_rules(
        {
            "rules": merged_rules,
            "expected_fingerprint": expected,
        }
    )
    return saved_status, _scoped_media_rules_payload(
        saved,
        profile,
        blocks,
        connected=saved_status < 500,
        editable=saved_status < 500,
    )


def _series_rules_fallback() -> Dict[str, Any]:
    stored = (
        _read_json(SERIES_RULES_PATH)
        if SERIES_RULES_PATH.is_file() and not SERIES_RULES_PATH.is_symlink()
        else {}
    )
    defaults_source = (
        _read_json(DEFAULT_RULES_PATH)
        if DEFAULT_RULES_PATH.is_file() and not DEFAULT_RULES_PATH.is_symlink()
        else {}
    )
    defaults = _rules_block_view(
        defaults_source,
        MOVIE_RULE_BLOCKS,
        fill_missing=True,
    )
    rules = _rules_block_view(stored, MOVIE_RULE_BLOCKS, fill_missing=True)
    if not any(rules.values()):
        rules = copy.deepcopy(defaults)
    canonical = json.dumps(
        rules,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "rules": rules,
        "active": copy.deepcopy(rules),
        "defaults": defaults,
        "rules_path": SERIES_RULES_ALIAS,
        "fingerprint": hashlib.sha256(canonical).hexdigest() if rules else None,
    }


def _series_rules_disconnected(upstream_status: int) -> Dict[str, Any]:
    fallback = _series_rules_fallback()
    return {
        "ok": True,
        "profile": "series",
        "connected": False,
        "editable": False,
        "message": SERIES_NOT_CONNECTED_MESSAGE,
        **fallback,
        "saved_at": None,
        "applied": False,
        "applies_to": "none",
        "upstream_status": upstream_status,
    }


def _series_rules_payload() -> Tuple[int, Dict[str, Any]]:
    status, payload = _proxy_upstream_json(
        f"{SERIES_WORKER_URL}/settings/rules",
        timeout=8,
    )
    valid_document = bool(
        isinstance(payload.get("rules"), dict)
        and isinstance(payload.get("fingerprint"), str)
        and payload.get("fingerprint")
    )
    if status >= 400 or payload.get("ok") is False or not valid_document:
        return 200, _series_rules_disconnected(status)
    result = copy.deepcopy(payload)
    result.update(
        {
            "profile": "series",
            "connected": True,
            "editable": True,
        }
    )
    return status, result


def _save_series_rules(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    status, result = _proxy_upstream_json(
        f"{SERIES_WORKER_URL}/settings/rules",
        payload,
        timeout=20,
    )
    if status >= 500:
        unavailable = _series_rules_disconnected(status)
        unavailable.update(
            {
                "ok": False,
                "error": "series_worker_unavailable",
            }
        )
        return 503, unavailable
    enriched = copy.deepcopy(result)
    enriched.update(
        {
            "profile": "series",
            "connected": True,
            "editable": True,
        }
    )
    return status, enriched


def _watcher_rules_payload() -> Tuple[int, Dict[str, Any]]:
    return _proxy_upstream_json(f"{ORCH_URL}/settings/watcher", timeout=8)


def _save_watcher_rules(
    payload: Dict[str, Any],
) -> Tuple[int, Dict[str, Any]]:
    return _proxy_upstream_json(
        f"{ORCH_URL}/settings/watcher",
        payload,
        timeout=20,
    )


def _watcher_rules_profile_payload(profile: str) -> Tuple[int, Dict[str, Any]]:
    if profile not in {"movies", "tv"}:
        return 400, {
            "ok": False,
            "error": "invalid_profile",
            "message": "profile debe ser movies o tv.",
        }
    status, upstream = _proxy_upstream_json(
        f"{ORCH_URL}/settings/watcher/{profile}",
        timeout=8,
    )
    available = status < 500 and status not in {404, 405}
    result = copy.deepcopy(upstream)
    result.update(
        {
            "profile": profile,
            "connected": available,
            "editable": available,
        }
    )
    return status, result


def _save_watcher_rules_profile(
    profile: str,
    payload: Dict[str, Any],
) -> Tuple[int, Dict[str, Any]]:
    if profile not in {"movies", "tv"}:
        return 400, {
            "ok": False,
            "error": "invalid_profile",
            "message": "profile debe ser movies o tv.",
        }
    status, upstream = _proxy_upstream_json(
        f"{ORCH_URL}/settings/watcher/{profile}",
        payload,
        timeout=20,
    )
    available = status < 500 and status not in {404, 405}
    result = copy.deepcopy(upstream)
    result.update(
        {
            "profile": profile,
            "connected": available,
            "editable": available,
        }
    )
    return status, result


def _status_payload() -> Dict[str, Any]:
    orch = _upstream_json(f"{ORCH_URL}/health")
    worker = _upstream_json(f"{WORKER_URL}/health")
    series_worker = _upstream_json(f"{SERIES_WORKER_URL}/health")
    orchestrator_connected = bool(
        isinstance(orch, dict)
        and not orch.get("error")
        and (orch.get("ok") is True or orch.get("status") == "ok")
    )
    worker_connected = bool(
        isinstance(worker, dict)
        and not worker.get("error")
        and (worker.get("ok") is True or worker.get("status") == "ok")
    )
    series_worker_connected = bool(
        isinstance(series_worker, dict)
        and not series_worker.get("error")
        and (
            series_worker.get("ok") is True
            or series_worker.get("status") == "ok"
        )
    )
    orchestrator_mode = str(orch.get("mode") or "unknown")
    series_mode = str(orch.get("series_mode") or "unknown")
    if series_mode not in {"legacy", "canary", "active"}:
        series_mode = "unknown"
    orchestrator_service = copy.deepcopy(orch)
    orchestrator_service.update(
        {"connected": orchestrator_connected, "editable": False}
    )
    movies_service = copy.deepcopy(worker)
    movies_service.update(
        {"connected": worker_connected, "editable": worker_connected}
    )
    trailers_service = copy.deepcopy(worker)
    trailers_service.update(
        {"connected": worker_connected, "editable": worker_connected}
    )
    series_service = copy.deepcopy(series_worker)
    series_service.update(
        {
            "connected": series_worker_connected,
            "editable": series_worker_connected,
            "healthy": series_worker_connected,
            "mode": series_mode,
            "routing_active": series_mode in {"canary", "active"},
        }
    )
    if series_worker_connected:
        series_service["message"] = f"Motor sano · modo {series_mode}"
    else:
        series_service.setdefault("status", "not_connected")
        series_service.setdefault("message", SERIES_NOT_CONNECTED_MESSAGE)
    return {
        "ok": True,
        "mode": orchestrator_mode,
        "series_mode": series_mode,
        "health": {
            "orchestrator": orchestrator_connected,
            "movies": worker_connected,
            "series": series_worker_connected,
            "trailers": worker_connected,
        },
        "orchestrator": orch,
        "media_worker": worker,
        "series_worker": series_worker,
        "services": {
            "orchestrator": orchestrator_service,
            "movies": movies_service,
            "series": series_service,
            "trailers": trailers_service,
        },
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


def _jobs_payload(profile: Optional[str] = None) -> Dict[str, Any]:
    jobs = _upstream_json(f"{ORCH_URL}/jobs", timeout=12)
    if isinstance(jobs, list):
        filtered = [
            job
            for job in jobs
            if profile is None
            or (
                isinstance(job, dict)
                and _profile_from_metadata(job) == profile
            )
        ]
        result: Dict[str, Any] = {"ok": True, "jobs": filtered}
        if profile is not None:
            result["profile"] = profile
        return result
    result = {
        "ok": False,
        "jobs": [],
        "error": jobs.get("error", "No se pudo leer jobs."),
    }
    if profile is not None:
        result["profile"] = profile
    return result


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


def _normalized_profile(value: object) -> Optional[str]:
    text = str(value or "").strip().lower()
    if text in {"tv", "series", "serie", "show", "shows"}:
        return "series"
    if text in {"movie", "movies", "pelicula", "película"} or text.startswith(
        ("movie", "trailer")
    ):
        return "movies"
    return None


def _profile_from_metadata(value: object) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    queue: List[Tuple[Dict[str, Any], int]] = [(value, 0)]
    seen = set()
    while queue:
        current, depth = queue.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        for key in ("profile", "category", "media_type"):
            profile = _normalized_profile(current.get(key))
            if profile:
                return profile
        if depth >= 3:
            continue
        for key in (
            "source_meta",
            "source_meta_json",
            "media_decision",
            "resolved_identity",
            "identity",
            "details",
            "job",
        ):
            nested = current.get(key)
            if isinstance(nested, str) and nested.lstrip().startswith("{"):
                try:
                    nested = json.loads(nested)
                except (TypeError, ValueError, json.JSONDecodeError):
                    nested = None
            if isinstance(nested, dict):
                queue.append((nested, depth + 1))
    return None


def _profile_from_text_evidence(text: str) -> Optional[str]:
    if SERIES_EVIDENCE_RE.search(text):
        return "series"
    if MOVIE_EVIDENCE_RE.search(text):
        return "movies"
    return None


def _review_structure_profile(folder: Path, reason_file: str) -> Optional[str]:
    evidence = [folder.name, reason_file]
    try:
        for index, child in enumerate(folder.rglob("*")):
            if index >= MAX_PROFILE_EVIDENCE_PATHS:
                break
            evidence.append(str(child.relative_to(folder)))
    except OSError:
        pass
    return _profile_from_text_evidence("\n".join(evidence))


def _review_item_profile(
    payload: Dict[str, Any],
    reason_file: str,
    *,
    folder: Optional[Path] = None,
    job: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    return (
        _profile_from_metadata(payload)
        or _profile_from_metadata(job)
        or (_review_structure_profile(folder, reason_file) if folder else None)
    )


def _job_contexts(job_ids: set[str]) -> Dict[str, Dict[str, Any]]:
    if not job_ids:
        return {}
    jobs = _jobs_payload().get("jobs", [])
    if not isinstance(jobs, list):
        return {}
    return {
        str(job.get("job_id")): job
        for job in jobs
        if isinstance(job, dict) and str(job.get("job_id") or "") in job_ids
    }


def _job_id_from_path(path: Path) -> str:
    for part in path.parts:
        if JOB_ID_RE.fullmatch(part):
            return part
    return ""


def _series_alias(alias_root: str, relative: Path | str = "") -> str:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return alias_root
    value = relative_path.as_posix()
    return f"{alias_root}/{value}" if value not in {"", "."} else alias_root


def _series_report_relative_allowed(value: Path | str) -> bool:
    normalized = str(value or "").replace("\\", "/")
    parts = normalized.split("/")
    return bool(
        len(parts) == 2
        and SERIES_JOB_DIR_RE.fullmatch(parts[0])
        and parts[0].lower() not in SERIES_RESERVED_REPORT_DIRS
        and parts[1] in SERIES_TECHNICAL_REPORT_FILES
    )


def _sanitize_series_text(value: str) -> str:
    text = str(value or "")
    replacements = (
        (str(SERIES_REVIEW_DIR), SERIES_REVIEW_ALIAS),
        (SERIES_REVIEW_DIR.as_posix(), SERIES_REVIEW_ALIAS),
        (str(SERIES_REPORT_ROOT), SERIES_REPORT_ALIAS),
        (SERIES_REPORT_ROOT.as_posix(), SERIES_REPORT_ALIAS),
    )
    for source, alias in replacements:
        if source:
            text = text.replace(source, alias)
    return text


def _sanitize_series_report_text(value: str) -> str:
    text = _sanitize_series_text(value).replace("\\/", "/")
    for source, alias in (
        ("/data/downloads", "<DATA_DOWNLOADS>"),
        ("/data/media", "<DATA_MEDIA>"),
        ("/config", "<CONFIG>"),
    ):
        text = text.replace(source, alias)
    text = re.sub(
        r"(?i)(?<![A-Za-z0-9_])/(?:data|config)(?=$|[/\\\s\"'])",
        "<REDACTED_PATH>",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:[a-z][a-z0-9+.-]*://|magnet:)[^\s\"'<>]+",
        "<REDACTED_URL>",
        text,
    )
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer <REDACTED>",
        text,
    )
    text = re.sub(
        r'(?i)(?P<prefix>"[^"\r\n]*(?:token|secret|password|api[_-]?key|apikey|authorization|auth)[^"\r\n]*"\s*:\s*")(?P<value>[^"\r\n]*)',
        lambda match: f'{match.group("prefix")}<REDACTED>',
        text,
    )
    return re.sub(
        r"(?i)(?P<prefix>\b(?:[A-Za-z0-9_-]*_)?(?:token|secret|password|api[_-]?key|apikey|authorization|auth)\s*[=:]\s*)(?P<value>[^&,\s]+)",
        lambda match: f'{match.group("prefix")}<REDACTED>',
        text,
    )


def _review_payload(
    limit: int = 80,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    series_owned = profile == "series"
    review_root = SERIES_REVIEW_DIR if series_owned else REVIEW_DIR
    root_connected = review_root.is_dir() and (
        not series_owned or not review_root.is_symlink()
    )
    records: List[Dict[str, Any]] = []
    if root_connected:
        for folder in sorted(review_root.iterdir(), key=_mtime, reverse=True):
            if series_owned:
                folder_name = folder.name.lower()
                if (
                    folder.name.startswith(".")
                    or folder_name.endswith(".tmp")
                    or ".tmp." in folder_name
                    or folder.is_symlink()
                    or not folder.is_dir()
                ):
                    continue
            elif not folder.is_dir():
                continue
            txts = sorted(
                path
                for path in folder.glob("*.txt")
                if not series_owned or not path.is_symlink()
            )
            reason_json = folder / "reason.json"
            payload = (
                {}
                if series_owned and reason_json.is_symlink()
                else _read_json(reason_json)
            )
            reason_file = txts[0].name if txts else ""
            item_profile = (
                "series"
                if series_owned
                else _review_item_profile(
                    payload,
                    reason_file,
                    folder=folder,
                )
            )
            records.append(
                {
                    "folder": folder,
                    "payload": payload,
                    "reason_file": reason_file,
                    "reason_path": txts[0] if txts else None,
                    "profile": item_profile,
                }
            )
    unresolved_job_ids = {
        str(record["payload"].get("job_id") or "")
        for record in records
        if record["profile"] is None
        and JOB_ID_RE.fullmatch(str(record["payload"].get("job_id") or ""))
    }
    contexts = (
        _job_contexts(unresolved_job_ids)
        if profile is not None and not series_owned
        else {}
    )
    items: List[Dict[str, Any]] = []
    for record in records:
        if len(items) >= limit:
            break
        folder = record["folder"]
        payload = record["payload"]
        job_id = str(payload.get("job_id") or "")
        item_profile = (
            "series"
            if series_owned
            else record["profile"]
            or _profile_from_metadata(contexts.get(job_id))
        )
        if not series_owned and profile is not None and item_profile not in {None, profile}:
            continue
        reason_path = record["reason_path"]
        reason_text = _short_text(reason_path, 2000) if reason_path else ""
        if series_owned:
            reason_text = _sanitize_series_text(reason_text)
        items.append(
            {
                "name": folder.name,
                "path": (
                    _series_alias(
                        SERIES_REVIEW_ALIAS,
                        folder.relative_to(review_root),
                    )
                    if series_owned
                    else str(folder)
                ),
                "mtime": _mtime(folder),
                "reason_file": record["reason_file"],
                "reason_text": reason_text,
                "phase": payload.get("phase", ""),
                "job_id": job_id,
                "file_count": _count_children(folder),
                "profile": item_profile,
                "classification": item_profile or "unclassified",
            }
        )
    result: Dict[str, Any] = {
        "ok": True,
        "review_dir": SERIES_REVIEW_ALIAS if series_owned else str(REVIEW_DIR),
        "items": items,
    }
    if profile is not None:
        result["profile"] = profile
    if series_owned:
        result["connected"] = root_connected
        if not root_connected:
            result["message"] = SERIES_NOT_CONNECTED_MESSAGE
    return result


def _reports_payload(
    limit: int = 120,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    series_owned = profile == "series"
    report_root = SERIES_REPORT_ROOT if series_owned else REPORT_ROOT
    root_connected = report_root.is_dir() and (
        not series_owned or not report_root.is_symlink()
    )
    records: List[Dict[str, Any]] = []
    roots = (
        [report_root / "runtime", report_root / "logs", report_root]
        if not series_owned or root_connected
        else []
    )
    seen = set()
    scan_limit = max(limit, limit * 10 if profile is not None else limit)
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=_mtime, reverse=True):
            if len(records) >= scan_limit:
                break
            if not path.is_file() or (series_owned and path.is_symlink()):
                continue
            relative_path = path.relative_to(report_root)
            if any(part in {"temp", "backups"} for part in relative_path.parts):
                continue
            if series_owned and (
                not _series_report_relative_allowed(relative_path)
                or _safe_regular_child(report_root, relative_path.as_posix()) is None
            ):
                continue
            rel = relative_path.as_posix() if series_owned else str(relative_path)
            if rel in seen:
                continue
            seen.add(rel)
            metadata: Dict[str, Any] = {}
            size = path.stat().st_size
            if path.suffix.lower() == ".json" and size <= MAX_REPORT_METADATA_BYTES:
                metadata = _read_json(path)
            item_profile = "series" if series_owned else _profile_from_metadata(metadata)
            if item_profile is None and not series_owned:
                item_profile = _profile_from_text_evidence(rel)
            job_id = str(metadata.get("job_id") or "") or _job_id_from_path(
                relative_path
            )
            records.append(
                {
                    "name": path.name,
                    "relative": rel,
                    "size": size,
                    "mtime": _mtime(path),
                    "kind": path.suffix.lower().lstrip(".") or "file",
                    "profile": item_profile,
                    "job_id": job_id,
                }
            )
    unresolved_job_ids = {
        str(record.get("job_id") or "")
        for record in records
        if record.get("profile") is None
        and JOB_ID_RE.fullmatch(str(record.get("job_id") or ""))
    }
    contexts = (
        _job_contexts(unresolved_job_ids)
        if profile is not None and not series_owned
        else {}
    )
    files: List[Dict[str, Any]] = []
    for record in records:
        if len(files) >= limit:
            break
        item_profile = (
            "series"
            if series_owned
            else record.get("profile")
            or _profile_from_metadata(contexts.get(str(record.get("job_id") or "")))
        )
        if item_profile is None:
            # Este root pertenece al Media Worker de peliculas/trailers.
            item_profile = "movies"
        if profile is not None and item_profile != profile:
            continue
        item = dict(record)
        item["profile"] = item_profile
        item.pop("job_id", None)
        if series_owned:
            item["path"] = _series_alias(SERIES_REPORT_ALIAS, item["relative"])
        files.append(item)
    result: Dict[str, Any] = {
        "ok": True,
        "report_root": SERIES_REPORT_ALIAS if series_owned else str(REPORT_ROOT),
        "files": files,
    }
    if profile is not None:
        result.update(
            {
                "profile": profile,
                "connected": root_connected,
            }
        )
        if series_owned and not root_connected:
            result["message"] = SERIES_NOT_CONNECTED_MESSAGE
    return result


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
            profile = query.get("profile", [None])[0]
            if profile not in {None, "movies", "series"}:
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_profile",
                        "message": "profile debe ser movies o series.",
                    },
                )
                return
            self._json(200, _jobs_payload(profile=profile))
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
        if path == "/api/movie-rules":
            status, result = _media_rules_profile_payload(
                "movies",
                MOVIE_RULE_BLOCKS,
            )
            self._json(status, result)
            return
        if path == "/api/trailer-rules":
            status, result = _media_rules_profile_payload(
                "trailers",
                TRAILER_RULE_BLOCKS,
            )
            self._json(status, result)
            return
        if path == "/api/series-rules":
            status, result = _series_rules_payload()
            self._json(status, result)
            return
        if path == "/api/watcher-rules":
            status, result = _watcher_rules_payload()
            self._json(status, result)
            return
        if path in {"/api/watcher-rules/movies", "/api/watcher-rules/tv"}:
            profile = path.rsplit("/", 1)[-1]
            status, result = _watcher_rules_profile_payload(profile)
            self._json(status, result)
            return
        if path == "/api/identity-rules":
            status, result = IDENTITY_PROXY.get_rules()
            self._json(status, result)
            return
        if path.startswith("/api/identity-rules/"):
            profile = path.removeprefix("/api/identity-rules/").strip("/")
            if profile in IDENTITY_PROFILES:
                status, result = IDENTITY_PROXY.get_rules(profile)
                self._json(status, result)
                return
        if path == "/api/review":
            profile = query.get("profile", [None])[0]
            if profile not in {None, "movies", "series"}:
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_profile",
                        "message": "profile debe ser movies o series.",
                    },
                )
                return
            self._json(200, _review_payload(profile=profile))
            return
        if path == "/api/reports":
            profile = query.get("profile", [None])[0]
            if profile not in {None, "movies", "series"}:
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_profile",
                        "message": "profile debe ser movies o series.",
                    },
                )
                return
            self._json(200, _reports_payload(profile=profile))
            return
        if path == "/api/codex-diagnostics":
            profile = query.get("profile", [None])[0]
            if profile not in {None, "movies", "series"}:
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_profile",
                        "message": "profile debe ser movies o series.",
                    },
                )
                return
            self._json(200, _codex_diagnostics_payload(profile=profile))
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
            profile = query.get("profile", [None])[0]
            if profile not in {None, "movies", "series"}:
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_profile",
                        "message": "profile debe ser movies o series.",
                    },
                )
                return
            series_owned = profile == "series"
            report_root = SERIES_REPORT_ROOT if series_owned else REPORT_ROOT
            target = (
                _safe_regular_child(report_root, rel)
                if not series_owned or _series_report_relative_allowed(rel)
                else None
            )
            if not target or not target.is_file():
                self._send(404, b"No hay informe.", "text/plain; charset=utf-8")
                return
            text = _short_text(target, 512000)
            if series_owned:
                text = _sanitize_series_report_text(text)
            self._send(
                200,
                text.encode("utf-8"),
                "text/plain; charset=utf-8",
            )
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
        if parsed.path == "/api/rules":
            try:
                status, result = _save_media_rules(self._read_payload())
                self._json(status, result)
            except Exception as error:
                self._json(500, {"ok": False, "error": str(error)})
            return
        media_profile_actions = {
            "/api/movie-rules": ("movies", MOVIE_RULE_BLOCKS),
            "/api/trailer-rules": ("trailers", TRAILER_RULE_BLOCKS),
        }
        media_profile_action = media_profile_actions.get(parsed.path)
        if media_profile_action:
            profile, blocks = media_profile_action
            payload = _read_protected_json_object(self)
            if payload is None:
                return
            try:
                status, result = _save_media_rules_profile(
                    payload,
                    profile,
                    blocks,
                )
                self._json(status, result)
            except Exception as error:
                self._json(500, {"ok": False, "error": type(error).__name__})
            return
        if parsed.path == "/api/series-rules":
            payload = _read_protected_json_object(self)
            if payload is None:
                return
            try:
                status, result = _save_series_rules(payload)
                self._json(status, result)
            except Exception as error:
                self._json(500, {"ok": False, "error": type(error).__name__})
            return
        if parsed.path == "/api/watcher-rules":
            try:
                status, result = _save_watcher_rules(self._read_payload())
                self._json(status, result)
            except Exception as error:
                self._json(500, {"ok": False, "error": str(error)})
            return
        if parsed.path in {"/api/watcher-rules/movies", "/api/watcher-rules/tv"}:
            profile = parsed.path.rsplit("/", 1)[-1]
            payload = _read_protected_json_object(self)
            if payload is None:
                return
            try:
                status, result = _save_watcher_rules_profile(
                    profile,
                    payload,
                )
                self._json(status, result)
            except Exception as error:
                self._json(500, {"ok": False, "error": type(error).__name__})
            return
        identity_actions = {
            "/api/identity-rules": IDENTITY_PROXY.save_rules,
            "/api/identity-rules/reset": IDENTITY_PROXY.reset_rules,
            "/api/identity-rules/cache/clear": IDENTITY_PROXY.clear_cache,
            "/api/identity-rules/test-parser": IDENTITY_PROXY.test_parser,
            "/api/identity-rules/test-resolver": IDENTITY_PROXY.test_resolver,
        }
        identity_action = identity_actions.get(parsed.path)
        if identity_action is None and parsed.path.startswith("/api/identity-rules/"):
            suffix = parsed.path.removeprefix("/api/identity-rules/").strip("/")
            profile, separator, action_name = suffix.partition("/")
            profile_actions = {
                "": IDENTITY_PROXY.save_rules,
                "reset": IDENTITY_PROXY.reset_rules,
                "cache/clear": IDENTITY_PROXY.clear_cache,
                "test-parser": IDENTITY_PROXY.test_parser,
                "test-resolver": IDENTITY_PROXY.test_resolver,
            }
            profile_action = profile_actions.get(action_name if separator else "")
            if profile in IDENTITY_PROFILES and profile_action is not None:
                identity_action = lambda payload, method=profile_action, selected=profile: method(
                    payload,
                    selected,
                )
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
