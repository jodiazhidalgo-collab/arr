import json
import os
import re
from typing import Any, Iterable, List


MAX_EXPORT_STRING_CHARS = 2000
EXPORT_TEXT_EDGE_CHARS = 800
MAX_EXPORT_LIST_ITEMS = 40
MAX_RELATED_FILES = 20
REDACTED = "<REDACTED>"
HOST_DATA_ROOT = os.environ.get("ARR_HOST_DATA_ROOT", "/host/data").rstrip("/")
HOST_ARR_ROOT = os.environ.get("ARR_HOST_ROOT", "/host/arr").rstrip("/")
WIN_ARR_ROOT = os.environ.get("ARR_ROOT_WIN", r"C:\arr").rstrip("\\")
UNC_ARR_ROOT = (os.environ.get("ARR_ROOT_UNC") or r"\\nas\docker\arr").rstrip("\\")
NAS_DATA_ROOT = "/volume1/UGREEN/data"
FIXED_WIN_ARR_ROOT = r"Z:\arr"
FIXED_WIN_ARR_ROOT_SLASH = "Z:/arr"

SENSITIVE_KEY_PARTS = (
    "token",
    "pass",
    "password",
    "authorization",
    "auth",
    "apikey",
    "api_key",
    "cookie",
    "session",
    "magnet",
    "download_url",
    "download_ref",
    "torrent_url",
    "url",
    "link",
)

PATH_ALIASES = (
    (f"{NAS_DATA_ROOT}/downloads", "<DATA_DOWNLOADS>"),
    (f"{NAS_DATA_ROOT}/media", "<DATA_MEDIA>"),
    (NAS_DATA_ROOT, "<DATA_ROOT>"),
    ("/data/downloads", "<DATA_DOWNLOADS>"),
    ("/data/media", "<DATA_MEDIA>"),
    (f"{HOST_DATA_ROOT}/downloads", "<DATA_DOWNLOADS>"),
    (f"{HOST_DATA_ROOT}/media", "<DATA_MEDIA>"),
    (HOST_DATA_ROOT, "<DATA_ROOT>"),
    (f"{HOST_ARR_ROOT}/diagnosticos_codex", "<CODEX_DIAGS>"),
    (f"{HOST_ARR_ROOT}/diagnostics", "<DIAGNOSTICS>"),
    (f"{HOST_ARR_ROOT}/config", "<CONFIG>"),
    ("/diagnosticos_codex", "<CODEX_DIAGS>"),
    ("/diagnostics", "<DIAGNOSTICS>"),
    ("/config", "<CONFIG>"),
    ("/app/data", "<APP_DATA>"),
    ("/app/logs", "<APP_LOGS>"),
    ("/data", "<DATA_ROOT>"),
    (HOST_ARR_ROOT, "<ARR_ROOT>"),
    (f"{FIXED_WIN_ARR_ROOT}\\diagnosticos_codex", "<CODEX_DIAGS>"),
    (f"{FIXED_WIN_ARR_ROOT}\\diagnostics", "<DIAGNOSTICS>"),
    (f"{FIXED_WIN_ARR_ROOT}\\config", "<CONFIG>"),
    (FIXED_WIN_ARR_ROOT, "<ARR_ROOT_WIN>"),
    (f"{FIXED_WIN_ARR_ROOT_SLASH}/diagnosticos_codex", "<CODEX_DIAGS>"),
    (f"{FIXED_WIN_ARR_ROOT_SLASH}/diagnostics", "<DIAGNOSTICS>"),
    (f"{FIXED_WIN_ARR_ROOT_SLASH}/config", "<CONFIG>"),
    (FIXED_WIN_ARR_ROOT_SLASH, "<ARR_ROOT_WIN>"),
    (f"{WIN_ARR_ROOT}\\diagnosticos_codex", "<CODEX_DIAGS>"),
    (f"{WIN_ARR_ROOT}\\diagnostics", "<DIAGNOSTICS>"),
    (f"{WIN_ARR_ROOT}\\config", "<CONFIG>"),
    (WIN_ARR_ROOT, "<ARR_ROOT_WIN>"),
    (f"{UNC_ARR_ROOT}\\diagnosticos_codex", "<CODEX_DIAGS>"),
    (f"{UNC_ARR_ROOT}\\diagnostics", "<DIAGNOSTICS>"),
    (f"{UNC_ARR_ROOT}\\config", "<CONFIG>"),
    (UNC_ARR_ROOT, "<ARR_ROOT_UNC>"),
)

USEFUL_RELATED_KEYS = {
    "filebot_output",
    "final_series_dir",
    "journal_path",
    "log_file",
    "manifest_path",
    "preserved_path",
    "reports_dir",
    "report_path",
    "review_path",
    "reason_file",
    "final_dir",
    "final_video",
    "final_srt",
    "source_path",
    "stage_path",
    "output_root",
    "torrent_path",
    "work_dir",
}


def sanitize_for_export(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<MAX_DEPTH>"
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        items = [sanitize_for_export(item, depth + 1) for item in value[:MAX_EXPORT_LIST_ITEMS]]
        if len(value) > MAX_EXPORT_LIST_ITEMS:
            items.append(f"<TRUNCATED_LIST {len(value) - MAX_EXPORT_LIST_ITEMS} items>")
        return items
    if isinstance(value, tuple):
        return sanitize_for_export(list(value), depth)
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            text_key = str(key)
            if is_sensitive_key(text_key):
                sanitized[text_key] = REDACTED
            else:
                sanitized[text_key] = sanitize_for_export(item, depth + 1)
        return sanitized
    return value


def sanitize_text(value: Any) -> str:
    text = str(value or "")
    text = redact_sensitive_fragments(text)
    text = alias_paths(text)
    return trim_export_string(text)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact_sensitive_fragments(text: str) -> str:
    text = re.sub(r"magnet:\?[^\s\"']+", "<MAGNET_REDACTED>", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;}\]]+",
        r"\1<REDACTED>",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:token|api_key|apikey|auth|password|pass|download_url|download_ref|torrent_url|url|magnet)=)[^&\s\"']+",
        r"\1<REDACTED>",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|api_key|apikey|auth|password|pass|download_url|download_ref|torrent_url|url)\s*=\s*[^\s,;}\]]+",
        r"\1=<REDACTED>",
        text,
    )
    text = re.sub(r"(?i)https?://[^\s\"']+\.torrent[^\s\"']*", "<URL_REDACTED>", text)
    return text


def alias_paths(text: str) -> str:
    result = text
    variants = list(PATH_ALIASES)
    variants.extend(
        (prefix.replace("\\", "\\\\"), alias)
        for prefix, alias in PATH_ALIASES
        if "\\" in prefix
    )
    for prefix, alias in sorted(variants, key=lambda item: len(item[0]), reverse=True):
        if prefix:
            result = re.sub(re.escape(prefix), lambda _match: alias, result, flags=re.IGNORECASE)
    return result


def trim_export_string(text: str) -> str:
    if len(text) <= MAX_EXPORT_STRING_CHARS:
        return text
    omitted = len(text) - (EXPORT_TEXT_EDGE_CHARS * 2)
    return (
        text[:EXPORT_TEXT_EDGE_CHARS]
        + f"\n\n[RECORTADO: {omitted} caracteres omitidos]\n\n"
        + text[-EXPORT_TEXT_EDGE_CHARS:]
    )


def phase_label(phase: Any) -> str:
    key = str(phase or "").strip().lower()
    return {
        "qbt": "qBittorrent",
        "qbit": "qBittorrent",
        "qbittorrent": "qBittorrent",
        "stable_wait": "Estabilidad",
        "stability": "Estabilidad",
        "staging": "Taller",
        "stage": "Taller",
        "identity": "Identidad",
        "resolver": "Identidad",
        "media_analysis": "Analisis",
        "media_ffmpeg": "FFmpeg",
        "media_verify": "Verificacion media",
        "media_finalize": "Finalizacion",
        "manual_review": "Revision manual",
        "series": "Series",
        "series_worker": "Series Worker",
        "series_postprocess": "Postproceso de Series",
        "series_verify": "Verificacion de Series",
        "series_publish": "Publicacion de Series",
        "series_review": "Revision de Series",
        "received": "Entrada",
        "extract": "Extraccion",
        "extraction": "Extraccion",
        "filebot": "FileBot",
        "verify": "Verificacion",
        "verification": "Verificacion",
        "media": "Media",
        "trailer": "Trailer",
        "cleanup": "Limpieza",
        "diagnostic": "Diagnostico",
        "finish": "Final",
    }.get(key, str(phase or "Sin fase"))


def series_worker_status(phase: Any, structured: Any, previous: Any = "") -> str:
    """Resume el estado del worker usando solo datos ya presentes en job_events."""

    phase_key = str(phase or "").strip().lower()
    payload = structured if isinstance(structured, dict) else {}
    previous_status = str(previous or "").strip().lower()
    state = str(payload.get("state") or "").strip().lower()
    result = payload.get("result") if isinstance(payload.get("result"), dict) else None
    if result is None and isinstance(payload.get("result_summary"), dict):
        result = payload["result_summary"]
    if result is None and isinstance(payload.get("result_json"), str):
        try:
            loaded = json.loads(payload["result_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            result = loaded

    result_status = str((result or {}).get("status") or "").strip().lower()
    explicit_status = result_status or str(payload.get("status") or "").strip().lower()
    series_phases = {
        "series",
        "series_worker",
        "series_postprocess",
        "series_verify",
        "series_publish",
        "series_review",
    }
    series_evidence = bool(
        phase_key in series_phases
        or state.startswith("series_")
        or (result or {}).get("kind") == "series"
        or payload.get("kind") == "series"
        or previous_status
    )
    if not series_evidence:
        return previous_status

    if explicit_status == "terminal" and result_status:
        explicit_status = result_status
    if explicit_status in {"accepted", "active", "processing", "recoverable", "running"}:
        return "running"
    if explicit_status in {"not_found", "queued", "ready"}:
        return "ready"
    if explicit_status in {
        "review_cleanup_pending",
        "status_unavailable",
        "workshop_cleanup_pending",
    }:
        return "running"
    if explicit_status in {"done", "review", "failed"}:
        return explicit_status

    if state == "series_postprocess_ready":
        return "ready"
    if state in {"series_postprocess_running", "series_review_cleanup"}:
        return "running"
    if state == "ready_cleanup" and (phase_key in series_phases or previous_status):
        return "done"
    if state == "manual_review" and (phase_key == "series_review" or previous_status):
        return "review"
    if state == "done" and previous_status:
        return "done"
    return previous_status


def collect_related_paths(value: Any) -> List[str]:
    found: List[str] = []
    _collect_related_paths(value, found)
    return limit_related_files(found)


def limit_related_files(paths: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in paths:
        text = sanitize_text(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= MAX_RELATED_FILES:
            break
    return result


def _collect_related_paths(value: Any, found: List[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in USEFUL_RELATED_KEYS and isinstance(item, str) and item.strip():
                found.append(item.strip())
            _collect_related_paths(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_related_paths(item, found)
    elif isinstance(value, str):
        text = value.strip()
        if (
            text.startswith(("/config/", "/data/", "/diagnostics/", "/diagnosticos_codex/"))
            or text.endswith((".json", ".log", ".txt"))
        ):
            found.append(text)


def json_dumps_sanitized(value: Any) -> str:
    return json.dumps(sanitize_for_export(value), ensure_ascii=False, indent=2, default=str) + "\n"
