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
    ("/data/downloads", "<DATA_DOWNLOADS>"),
    ("/data/media", "<DATA_MEDIA>"),
    (f"{HOST_DATA_ROOT}/downloads", "<DATA_DOWNLOADS>"),
    (f"{HOST_DATA_ROOT}/media", "<DATA_MEDIA>"),
    (f"{HOST_ARR_ROOT}/diagnosticos_codex", "<CODEX_DIAGS>"),
    (f"{HOST_ARR_ROOT}/diagnostics", "<DIAGNOSTICS>"),
    (f"{HOST_ARR_ROOT}/config", "<CONFIG>"),
    ("/diagnosticos_codex", "<CODEX_DIAGS>"),
    ("/diagnostics", "<DIAGNOSTICS>"),
    ("/config", "<CONFIG>"),
    ("/app/data", "<APP_DATA>"),
    ("/app/logs", "<APP_LOGS>"),
    (HOST_ARR_ROOT, "<ARR_ROOT>"),
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
    "log_file",
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
    for prefix, alias in PATH_ALIASES:
        result = result.replace(prefix, alias)
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
