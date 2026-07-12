import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin

import requests
from flask import Flask, Response, jsonify, redirect, render_template, request
from modulos.arr_trace import ArrTrace
from modulos.persistent_jobs import PersistentJobStore
from modulos.search_history import SearchHistoryStore
from modulos.submission_store import SubmissionStore, submission_key


PORT = int(os.getenv("PORT", "9003"))
JACKETT_BASE = os.getenv("JACKETT_BASE", "http://jackett:9117").rstrip("/")
JACKETT_API_KEY = os.getenv("JACKETT_API_KEY", "")
REAL_DEBRID_TOKEN = os.getenv("REAL_DEBRID_TOKEN", "").strip()
REAL_DEBRID_API = os.getenv("REAL_DEBRID_API", "https://api.real-debrid.com/rest/1.0").rstrip("/")
RDT_BASE = os.getenv("RDT_BASE", "http://rdtclient:6500").rstrip("/")
RDT_USER = os.getenv("RDT_USER", "admin")
RDT_PASS = os.getenv("RDT_PASS", "")
RDT_SAVE_ROOT = os.getenv("RDT_SAVE_ROOT", "/data/downloads").rstrip("/")
QBIT_BASE = os.getenv("QBIT_BASE", "http://gluetun:8080").rstrip("/")
QBIT_USER = os.getenv("QBIT_USER", "admin")
QBIT_PASS = os.getenv("QBIT_PASS", "")
QBIT_SAVE_ROOT = os.getenv("QBIT_SAVE_ROOT", "/data/downloads/torrents/complete").rstrip("/")
LOG_DIR = Path(os.getenv("LOG_DIR", "/app/logs"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
ARR_DIAGNOSTICS_ROOT = Path(os.getenv("ARR_DIAGNOSTICS_ROOT", "/diagnostics/arr"))
SETTINGS_PATH = DATA_DIR / "settings.json"
MONITOR_DIR = DATA_DIR / "monitor_torrents"
MONITOR_STATE_PATH = DATA_DIR / "monitor_state.json"
RESULT_CACHE_PATH = DATA_DIR / "result_cache.json"
UI_JOBS_DIR = DATA_DIR / "ui_jobs"
SUBMISSIONS_PATH = DATA_DIR / "submissions.sqlite3"
SEARCH_HISTORY_PATH = DATA_DIR / "search_history.sqlite3"
VIDEO_EXT_RE = re.compile(r"\.(mkv|mp4|avi|mov|m4v|wmv|ts|m2ts)$", re.I)
ENGINE_LOCK = threading.Lock()
SETTINGS_LOCK = threading.Lock()
MONITOR_LOCK = threading.Lock()
RESULT_CACHE_LOCK = threading.Lock()
MONITOR_START_LOCK = threading.Lock()
MONITOR_STARTED = False
RETRYABLE_HTTP = {408, 409, 423, 425, 429, 500, 502, 503, 504}
RESULT_CACHE_TTL_SEC = 6 * 60 * 60
RESULT_CACHE_LIMIT = 3000
RDT_FINISHED_CLEANUP_DELAY_SEC = int(os.getenv("RDT_FINISHED_CLEANUP_DELAY_SEC", "30"))
SUBMISSION_REUSE_SEC = int(os.getenv("SUBMISSION_REUSE_SEC", str(6 * 60 * 60)))
MONITOR_ORPHAN_CLEANUP_SEC = int(os.getenv("MONITOR_ORPHAN_CLEANUP_SEC", str(6 * 60 * 60)))

LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
MONITOR_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("buscador-puente-arr")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_DIR / "buscador-puente-arr.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)

app = Flask(__name__)
ui_jobs = PersistentJobStore(UI_JOBS_DIR, logger)
submissions = SubmissionStore(SUBMISSIONS_PATH, logger)
search_history = SearchHistoryStore(
    SEARCH_HISTORY_PATH,
    logger,
    retention_days=int(os.getenv("SEARCH_HISTORY_RETENTION_DAYS", "30")),
    max_searches=int(os.getenv("SEARCH_HISTORY_MAX_SEARCHES", "300")),
    page_size=int(os.getenv("SEARCH_HISTORY_PAGE_SIZE", "25")),
)
arr_trace = ArrTrace(ARR_DIAGNOSTICS_ROOT, logger)

DEFAULT_SETTINGS = {
    "rdt": {
        "start_timeout_sec": 120,
        "ready_timeout_sec": 5,
        "rd_retry_attempts": 5,
        "fallback_enabled": True,
        "cleanup_on_fallback": True,
    },
    "qbit": {
        "fallback_enabled": True,
        "add_paused": False,
        "default_category": "manual",
        "auto_uncertain_category": "manual",
    },
    "auto": {
        "series_templates": [
            "SXXEXX",
            "SXEX",
            "SXX EXX",
            "XXxXX",
            "XxXX",
            "Temporada XX",
            "Temp XX",
            "Season XX",
            "Capitulo XX",
            "Capitulo X",
            "Episode XX",
            "Episodio XX",
        ],
        "series_words": [
            "capitulo",
            "capítulo",
            "episodio",
            "episode",
            "temporada",
            "temp",
            "season",
        ],
        "movie_words": [
            "bluray",
            "blu-ray",
            "bdrip",
            "webrip",
            "web-dl",
            "webdl",
            "hdrip",
            "dvdrip",
            "remux",
            "2160p",
            "1080p",
            "720p",
            "4k",
        ],
        "movie_years": True,
    },
}


def safe_name(value: str, suffix: str = ".torrent") -> str:
    base = re.sub(r"[^a-zA-Z0-9_. -]+", "_", value or "jackett").strip(" ._")
    base = re.sub(r"\s+", " ", base)[:150].strip() or "jackett"
    if not base.lower().endswith(suffix):
        base += suffix
    return base


def unique_path(folder: Path, filename: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    if not path.exists():
        return path
    return folder / f"{Path(filename).stem}__{int(time.time())}{Path(filename).suffix}"


def copy_defaults() -> dict:
    return json.loads(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False))


def as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "si", "sí", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def clean_list(value: Any, default: list[str], limit: int = 40) -> list[str]:
    if isinstance(value, str):
        rows = value.splitlines()
    elif isinstance(value, list):
        rows = value
    else:
        rows = default
    out = []
    for row in rows:
        text = str(row or "").strip()
        if text and text not in out:
            out.append(text[:120])
        if len(out) >= limit:
            break
    return out or default


def sanitize_settings(raw: dict | None) -> dict:
    base = copy_defaults()
    raw = raw if isinstance(raw, dict) else {}
    rdt = raw.get("rdt") if isinstance(raw.get("rdt"), dict) else {}
    qbit = raw.get("qbit") if isinstance(raw.get("qbit"), dict) else {}
    auto = raw.get("auto") if isinstance(raw.get("auto"), dict) else {}

    base["rdt"]["start_timeout_sec"] = bounded_int(rdt.get("start_timeout_sec"), 120, 30, 600)
    base["rdt"]["ready_timeout_sec"] = bounded_int(rdt.get("ready_timeout_sec"), 5, 1, 120)
    base["rdt"]["rd_retry_attempts"] = bounded_int(rdt.get("rd_retry_attempts"), 5, 1, 8)
    base["rdt"]["fallback_enabled"] = as_bool(rdt.get("fallback_enabled"), True)
    base["rdt"]["cleanup_on_fallback"] = as_bool(rdt.get("cleanup_on_fallback"), True)

    default_category = str(qbit.get("default_category") or base["qbit"]["default_category"]).strip().lower()
    uncertain_category = str(qbit.get("auto_uncertain_category") or base["qbit"]["auto_uncertain_category"]).strip().lower()
    base["qbit"]["fallback_enabled"] = as_bool(qbit.get("fallback_enabled"), True)
    base["qbit"]["add_paused"] = as_bool(qbit.get("add_paused"), False)
    base["qbit"]["default_category"] = default_category if default_category in {"movies", "tv", "manual"} else "manual"
    base["qbit"]["auto_uncertain_category"] = uncertain_category if uncertain_category in {"movies", "tv", "manual"} else "manual"

    base["auto"]["series_templates"] = clean_list(auto.get("series_templates"), base["auto"]["series_templates"])
    base["auto"]["series_words"] = clean_list(auto.get("series_words"), base["auto"]["series_words"])
    base["auto"]["movie_words"] = clean_list(auto.get("movie_words"), base["auto"]["movie_words"])
    base["auto"]["movie_years"] = as_bool(auto.get("movie_years"), True)
    return base


def load_settings() -> dict:
    with SETTINGS_LOCK:
        try:
            if SETTINGS_PATH.exists():
                return sanitize_settings(json.loads(SETTINGS_PATH.read_text(encoding="utf-8") or "{}"))
        except Exception as exc:
            logger.warning("settings load failed error=%s", str(exc)[:160])
        settings = copy_defaults()
        save_settings(settings)
        return settings


def save_settings(settings: dict) -> dict:
    clean = sanitize_settings(settings)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)
    return clean


def text_lines(value: list[str]) -> str:
    return "\n".join(value or [])


def absolute_jackett_url(value: str) -> str:
    if not value:
        return ""
    if value.startswith("magnet:"):
        return value
    return urljoin(JACKETT_BASE + "/", value)


def human_size(size: str) -> str:
    try:
        n = int(size or 0)
    except ValueError:
        return ""
    if n <= 0:
        return ""
    gb = n / 1024 / 1024 / 1024
    if gb >= 1:
        return f"{gb:.2f} GB"
    return f"{n / 1024 / 1024:.0f} MB"


def int_value(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value or "").replace(",", ".")))
    except (TypeError, ValueError):
        return default


def clean_text(value: str) -> str:
    text = value or ""
    if any(bad in text for bad in ("Ã", "Â", "�")):
        try:
            fixed = text.encode("latin1").decode("utf-8")
            if fixed:
                return fixed
        except Exception:
            pass
    return text


def template_to_regex(template: str) -> re.Pattern:
    parts = []
    i = 0
    while i < len(template):
        char = template[i]
        if char == "X":
            j = i
            while j < len(template) and template[j] == "X":
                j += 1
            parts.append(rf"\d{{1,{j - i}}}")
            i = j
            continue
        if char.isspace():
            parts.append(r"[\s._-]*")
        elif char in ".-_":
            parts.append(r"[\s._-]*")
        else:
            parts.append(re.escape(char))
        i += 1
    return re.compile(r"(?<![a-z0-9])" + "".join(parts) + r"(?![a-z0-9])", re.I)


def word_regex(word: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(word.strip()) + r"(?![a-z0-9])", re.I)


def classify_auto(title: str, settings: dict | None = None) -> str:
    settings = settings or load_settings()
    auto = settings.get("auto", {})
    qbit = settings.get("qbit", {})
    title = clean_text(title or "")

    for template in auto.get("series_templates") or []:
        try:
            if template_to_regex(template).search(title):
                return "tv"
        except re.error:
            continue

    for word in auto.get("series_words") or []:
        if word.strip() and word_regex(word).search(title):
            return "tv"

    if auto.get("movie_years", True) and re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", title):
        return "movies"

    lower = title.lower()
    for word in auto.get("movie_words") or []:
        if word.strip() and word.strip().lower() in lower:
            return "movies"

    return qbit.get("auto_uncertain_category") or "manual"


def resolved_category(category: str, title: str, settings: dict | None = None) -> str:
    category = (category or "").strip().lower()
    if category in {"movies", "tv", "manual"}:
        return category
    return classify_auto(title, settings)


def bdecode_item(raw: bytes, pos: int = 0) -> tuple[Any, int]:
    token = raw[pos : pos + 1]
    if token == b"i":
        end = raw.index(b"e", pos)
        return int(raw[pos + 1 : end]), end + 1
    if token == b"l":
        pos += 1
        items = []
        while raw[pos : pos + 1] != b"e":
            item, pos = bdecode_item(raw, pos)
            items.append(item)
        return items, pos + 1
    if token == b"d":
        pos += 1
        items = {}
        while raw[pos : pos + 1] != b"e":
            key, pos = bdecode_item(raw, pos)
            value, pos = bdecode_item(raw, pos)
            items[key] = value
        return items, pos + 1
    if token.isdigit():
        colon = raw.index(b":", pos)
        length = int(raw[pos:colon])
        start = colon + 1
        end = start + length
        return raw[start:end], end
    raise ValueError("torrent invalido")


def text_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def normalize_info_hash(value: Any) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"[a-fA-F0-9]{40}", raw):
        return raw.lower()
    if re.fullmatch(r"[a-zA-Z2-7]{32}", raw):
        try:
            return base64.b32decode(raw.upper()).hex()
        except Exception:
            return ""
    return ""


def magnet_hash(magnet: str) -> str:
    match = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", str(magnet or ""))
    return normalize_info_hash(match.group(1)) if match else ""


def torrent_info(raw: bytes) -> dict:
    root, _pos = bdecode_item(raw, 0)
    if not isinstance(root, dict):
        raise RuntimeError("torrent invalido")
    pos = 1
    info_raw = b""
    info_obj = None
    while raw[pos : pos + 1] != b"e":
        key, pos = bdecode_item(raw, pos)
        if key == b"info":
            start = pos
            info_obj, pos = bdecode_item(raw, pos)
            info_raw = raw[start:pos]
        else:
            _skip, pos = bdecode_item(raw, pos)
    if not info_raw or not isinstance(info_obj, dict):
        raise RuntimeError("torrent sin bloque info")
    files = []
    if isinstance(info_obj.get(b"files"), list):
        for file_info in info_obj.get(b"files") or []:
            parts = file_info.get(b"path") or []
            if not isinstance(parts, list):
                parts = [parts]
            path = "/".join(text_value(part) for part in parts if text_value(part))
            if path:
                files.append({"path": "/" + path, "size": int(file_info.get(b"length") or 0)})
    else:
        name = text_value(info_obj.get(b"name")) or "torrent"
        files.append({"path": "/" + name, "size": int(info_obj.get(b"length") or 0)})
    return {
        "hash": hashlib.sha1(info_raw).hexdigest().lower(),
        "name": text_value(info_obj.get(b"name")) or "torrent",
        "files": files,
        "private": int(info_obj.get(b"private") or 0) == 1,
    }


def torrent_to_magnet(raw: bytes) -> str:
    info = torrent_info(raw)
    if info.get("private"):
        raise ValueError("torrent privado")
    info_hash = normalize_info_hash(info.get("hash"))
    if not info_hash:
        raise ValueError("torrent sin hash")
    name = str(info.get("name") or "torrent").strip() or "torrent"
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(name, safe='')}"


def selected_manual_files(raw: bytes) -> str:
    files = torrent_info(raw)["files"]
    videos = [
        str(item["path"])
        for item in files
        if VIDEO_EXT_RE.search(str(item.get("path") or ""))
        and not re.search(r"(^|[\\/ ._-])sample([\\/ ._-]|$)", str(item.get("path") or ""), re.I)
    ]
    if videos:
        return ",".join(videos)
    return ""


def result_cache_id(row: dict) -> str:
    info_hash = normalize_info_hash(row.get("info_hash") or row.get("infohash"))
    payload = (
        {"info_hash": info_hash}
        if info_hash
        else {
            "title": clean_text(str(row.get("title") or "")),
            "tracker_id": str(row.get("tracker_id") or ""),
            "size": str(row.get("size") or ""),
            "download_url": str(row.get("download_url") or row.get("magnet") or ""),
        }
    )
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def normalized_source(row: dict) -> dict:
    link = str(row.get("download_url") or row.get("magnet") or "").strip()
    info_hash = normalize_info_hash(row.get("info_hash") or row.get("infohash")) or magnet_hash(link)
    return {
        "title": clean_text(str(row.get("title") or "Sin titulo").strip()),
        "size": str(row.get("size") or "").strip(),
        "size_text": str(row.get("size_text") or human_size(row.get("size"))),
        "seeders": int_value(row.get("seeders")),
        "peers": int_value(row.get("peers")),
        "leechers": int_value(row.get("leechers", row.get("peers"))),
        "tracker": clean_text(str(row.get("tracker") or "").strip()),
        "tracker_id": str(row.get("tracker_id") or "").strip(),
        "download_url": link,
        "info_hash": info_hash,
        "identity_checked": bool(row.get("identity_checked") or info_hash),
        "type": "magnet" if link.startswith("magnet:") else "torrent",
        "is_magnet": link.startswith("magnet:"),
    }


def normalize_result(row: dict) -> dict:
    out = normalized_source(row)
    link = out["download_url"]
    sources = [normalized_source(source) for source in row.get("sources", []) if isinstance(source, dict)]
    source_count = max(1, int_value(row.get("source_count")), len(sources))
    out["magnet"] = link if link.startswith("magnet:") else None
    out["source_count"] = source_count
    if sources:
        out["sources"] = sources
    out["id"] = str(row.get("id") or result_cache_id(out))
    return out


def search_history_source(indexers: list[str], metadata: dict | None = None) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    explicit = str(metadata.get("source") or metadata.get("origin") or "").strip().lower()
    if explicit in {"wolfmax", "wolfmax4k"}:
        return "wolfmax"
    tracker_ids = {str(value or "").strip().lower() for value in indexers or []}
    if "wolfmax4k" in tracker_ids and str(metadata.get("section") or "").strip():
        return "wolfmax"
    return "bridge"


def parse_results(xml_text: str) -> list[dict]:
    ns = "{http://torznab.com/schemas/2015/feed}"
    root = ET.fromstring(xml_text)
    rows = []
    for item in root.findall(".//item"):
        attrs = {attr.attrib.get("name", ""): attr.attrib.get("value", "") for attr in item.findall(f"{ns}attr")}
        title = clean_text((item.findtext("title") or "Sin titulo").strip())
        tracker_node = item.find("jackettindexer")
        tracker = clean_text(((tracker_node.text if tracker_node is not None else "") or attrs.get("tracker", "") or "").strip())
        tracker_id = (tracker_node.attrib.get("id", "") if tracker_node is not None else "").strip()
        link = absolute_jackett_url((item.findtext("link") or "").strip())
        guid = (item.findtext("guid") or "").strip()
        magnet_url = attrs.get("magneturl", "").strip()
        info_hash = normalize_info_hash(attrs.get("infohash")) or magnet_hash(magnet_url) or magnet_hash(link) or magnet_hash(guid)
        size = item.findtext("size") or ""
        seeders = attrs.get("seeders", "")
        peers = attrs.get("peers", "")
        leechers = attrs.get("leechers", "") or attrs.get("peers", "")
        rows.append(normalize_result({
            "title": title,
            "size": size,
            "size_text": human_size(size),
            "seeders": int_value(seeders),
            "peers": int_value(peers),
            "leechers": int_value(leechers),
            "tracker": tracker,
            "tracker_id": tracker_id,
            "download_url": link,
            "info_hash": info_hash,
        }))
    return rows


def parse_indexers(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    rows = []
    for item in root.findall(".//indexer"):
        if item.attrib.get("configured") != "true":
            continue
        indexer_id = item.attrib.get("id", "").strip()
        title = clean_text((item.findtext("title") or indexer_id).strip())
        if indexer_id:
            rows.append({"id": indexer_id, "title": title})
    return sorted(rows, key=lambda row: row["title"].lower())


def configured_indexers() -> list[dict]:
    if not JACKETT_API_KEY:
        raise RuntimeError("falta JACKETT_API_KEY")
    params = {
        "t": "indexers",
        "apikey": JACKETT_API_KEY,
    }
    url = f"{JACKETT_BASE}/api/v2.0/indexers/all/results/torznab/api"
    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()
    response.encoding = "utf-8"
    return parse_indexers(response.text)


def search_jackett(query: str, indexer: str = "all") -> list[dict]:
    if not JACKETT_API_KEY:
        raise RuntimeError("falta JACKETT_API_KEY")
    indexer = re.sub(r"[^a-zA-Z0-9_-]+", "", indexer or "all") or "all"
    params = {
        "t": "search",
        "q": query,
        "apikey": JACKETT_API_KEY,
    }
    url = f"{JACKETT_BASE}/api/v2.0/indexers/{indexer}/results/torznab/api"
    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()
    response.encoding = "utf-8"
    return parse_results(response.text)


def normalize_search_text(value: str) -> str:
    text = clean_text(value or "").lower()
    text = unicodedata.normalize("NFKD", text)
    return "".join(char for char in text if not unicodedata.combining(char))


def compact_search_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_search_text(value))


def search_terms(query: str) -> list[str]:
    terms = []
    for part in re.findall(r"[a-z0-9]+", normalize_search_text(query)):
        compact = compact_search_text(part)
        if compact and compact not in terms:
            terms.append(compact)
    return terms


QUALITY_QUERY_TERMS = {
    "4k", "uhd", "2160", "2160p", "1080", "1080p", "720", "720p",
    "4kuhdrip", "4kuhstrip", "webrip", "webdl", "web", "dl", "bluray",
    "blu", "ray", "brrip", "bdrip", "hdtv", "hdrip", "dvdrip", "rip",
    "ac3", "spanish", "esp", "spa", "castellano", "latino",
}
CAP_QUERY_TERMS = {"cap", "capitulo", "capitulos", "episodio", "episode"}


def unique_search_parts(value: str) -> list[str]:
    parts = []
    for part in re.findall(r"[a-z0-9]+", normalize_search_text(value)):
        compact = compact_search_text(part)
        if compact and compact not in parts:
            parts.append(compact)
    return parts


def extract_year(value: str) -> str:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", str(value or ""))
    return match.group(1) if match else ""


def format_episode(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def extract_episode(value: str) -> str:
    text = normalize_search_text(value)
    match = re.search(r"\bs\s*(\d{1,2})\s*e\s*(\d{1,3})\b", text)
    if not match:
        match = re.search(r"\b(\d{1,2})\s*x\s*(\d{1,3})\b", text)
    if not match:
        return ""
    return format_episode(int(match.group(1)), int(match.group(2)))


def extract_cap_info(value: str) -> dict[str, str]:
    text = normalize_search_text(value)
    match = re.search(r"\bcap(?:itulo)?\.?\s*\[?\s*(\d{1,4})\s*\]?", text)
    if not match:
        return {"raw": "", "episode": ""}
    raw = match.group(1)
    episode = ""
    if len(raw) >= 3:
        season = int(raw[:-2])
        ep = int(raw[-2:])
        if season > 0 and ep > 0:
            episode = format_episode(season, ep)
    return {"raw": raw, "episode": episode}


def section_resolution_bucket(section: str) -> str:
    section = (section or "").strip().lower()
    if section in {"peliculas4k", "series4k"}:
        return "2160"
    if section in {"peliculas1080", "series1080"}:
        return "1080"
    return ""


def resolution_bucket(value: str, section: str = "") -> str:
    section_bucket = section_resolution_bucket(section)
    if section_bucket:
        return section_bucket
    text = normalize_search_text(value)
    compact = compact_search_text(value)
    if "4kuhdrip" in compact or "4kuhstrip" in compact or re.search(r"\b4k\b", text) or "2160" in text or re.search(r"\buhd\b", text):
        return "2160"
    if "1080" in text:
        return "1080"
    if "720" in text:
        return "720"
    return ""


def strip_quality_text(value: str) -> str:
    text = clean_text(value or "")
    text = re.sub(r"\b(?:4k|2160p?|1080p?|720p?|uhd|4kuhdrip|4kuhstrip)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:webrip|web[- ]?dl|bluray|blu[- ]?ray|brrip|bdrip|hdtv|hdrip|dvdrip|ac3)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:esp|spa|spanish|castellano|latino)\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -_.:[]()")


def strip_cap_text(value: str) -> str:
    text = re.sub(r"\bcap(?:itulo)?\.?\s*\[?\s*\d{1,4}\s*\]?", " ", clean_text(value or ""), flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -_.:[]()")


def parse_query_intent(query: str, category: str = "auto", metadata: dict | None = None) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, dict) else {}
    section = str(metadata.get("section") or "").strip().lower()
    combined = " ".join(
        str(x or "")
        for x in (
            query,
            metadata.get("title"),
            metadata.get("title_clean"),
            metadata.get("quality"),
            section,
        )
    )
    cap = extract_cap_info(combined)
    episode = extract_episode(combined) or cap.get("episode", "")
    bucket = resolution_bucket(combined, section)
    year = extract_year(query) or extract_year(combined)
    skip = set(QUALITY_QUERY_TERMS) | set(CAP_QUERY_TERMS)
    if cap.get("raw"):
        skip.add(compact_search_text(cap["raw"]))
    terms = []
    for term in unique_search_parts(query):
        if term in skip:
            continue
        if re.fullmatch(r"(2160|1080|720)p?", term):
            continue
        terms.append(term)
    return {
        "category": category or "auto",
        "section": section,
        "year": year,
        "episode": episode,
        "cap_raw": cap.get("raw", ""),
        "resolution": bucket,
        "terms": terms,
    }


def add_unique_text(rows: list[str], value: str) -> None:
    text = clean_text(value or "").strip()
    if text and text not in rows:
        rows.append(text)


def jackett_candidate_queries(query: str, intent: dict[str, Any] | None = None) -> list[str]:
    candidates: list[str] = []
    add_unique_text(candidates, query)
    clean_query = strip_quality_text(query)
    add_unique_text(candidates, clean_query)
    if intent:
        base = strip_cap_text(clean_query)
        cap_raw = str(intent.get("cap_raw") or "")
        episode = str(intent.get("episode") or "")
        if cap_raw and base:
            add_unique_text(candidates, f"{base} Cap {cap_raw}")
        if episode and base and compact_search_text(episode) not in compact_search_text(base):
            add_unique_text(candidates, f"{base} {episode}")
        add_unique_text(candidates, base)
    raw_parts = re.findall(r"[0-9A-Za-zÀ-ÿ]+", clean_text(query or ""))
    if raw_parts:
        add_unique_text(candidates, " ".join(raw_parts))
    for end in range(min(len(raw_parts) - 1, 5), 1, -1):
        add_unique_text(candidates, " ".join(raw_parts[:end]))
    for part in raw_parts:
        compact = compact_search_text(part)
        if len(compact) >= 2:
            add_unique_text(candidates, part)
            add_unique_text(candidates, compact)
    return candidates[:16]


def cap_matches_title(title: str, intent: dict[str, Any]) -> bool:
    cap_raw = str(intent.get("cap_raw") or "")
    if not cap_raw:
        return True
    title_cap = extract_cap_info(title)
    if title_cap.get("raw") == cap_raw:
        return True
    wanted_episode = str(intent.get("episode") or "")
    return bool(wanted_episode and extract_episode(title).lower() == wanted_episode.lower())


def flexible_match(title: str, terms: list[str], intent: dict[str, Any] | None = None) -> bool:
    haystack = compact_search_text(title)
    if not haystack or not all(term in haystack for term in terms):
        return False
    if not intent:
        return True
    wanted_year = str(intent.get("year") or "")
    if wanted_year and wanted_year not in title:
        return False
    wanted_episode = str(intent.get("episode") or "")
    if wanted_episode and extract_episode(title).lower() != wanted_episode.lower() and not cap_matches_title(title, intent):
        return False
    if not wanted_episode and not cap_matches_title(title, intent):
        return False
    wanted_resolution = str(intent.get("resolution") or "")
    if wanted_resolution and resolution_bucket(title) != wanted_resolution:
        return False
    return True


def filter_search_results(rows: list[dict], query: str, intent: dict[str, Any] | None = None) -> list[dict]:
    intent = intent or parse_query_intent(query)
    terms = intent.get("terms") if isinstance(intent.get("terms"), list) else search_terms(query)
    if not terms:
        return [row for row in rows if flexible_match(str(row.get("title") or ""), [], intent)]
    return [row for row in rows if flexible_match(str(row.get("title") or ""), terms, intent)]


def selected_indexers(raw: str) -> list[str]:
    ids = []
    for part in (raw or "").split(","):
        item = re.sub(r"[^a-zA-Z0-9_-]+", "", part.strip())
        if item and item != "all" and item not in ids:
            ids.append(item)
    return ids


def selected_indexers_from_payload(payload: dict) -> list[str]:
    values: list[str] = []
    for key in ("indexers", "trackers"):
        raw = payload.get(key)
        if isinstance(raw, list):
            values.extend(str(item or "") for item in raw)
        elif isinstance(raw, str):
            values.extend(raw.split(","))
    for key in ("indexer", "tracker"):
        raw = payload.get(key)
        if isinstance(raw, str):
            values.extend(raw.split(","))
    if any(str(item or "").strip().lower() == "all" for item in values):
        return []
    return selected_indexers(",".join(values))


def result_identity_key(row: dict) -> str:
    return "::".join(
        (
            str(row.get("tracker_id") or "").strip().lower(),
            compact_search_text(str(row.get("title") or "")),
            str(row.get("size") or "").strip(),
        )
    )


def cached_info_hashes() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for entry in load_result_cache().values():
        if not isinstance(entry, dict):
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        candidates = [result]
        candidates.extend(source for source in result.get("sources", []) if isinstance(source, dict))
        for source in candidates:
            info_hash = normalize_info_hash(source.get("info_hash") or source.get("infohash"))
            key = result_identity_key(source)
            if info_hash and key:
                lookup[key] = info_hash
    return lookup


def cached_identity_checks() -> set[str]:
    checked: set[str] = set()
    for entry in load_result_cache().values():
        if not isinstance(entry, dict):
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        candidates = [result]
        candidates.extend(source for source in result.get("sources", []) if isinstance(source, dict))
        for source in candidates:
            if source.get("identity_checked"):
                checked.add(result_identity_key(source))
    return checked


def resolve_result_info_hash(row: dict) -> str:
    info_hash = normalize_info_hash(row.get("info_hash") or row.get("infohash"))
    if info_hash:
        return info_hash
    target = absolute_jackett_url(str(row.get("download_url") or row.get("magnet") or "").strip())
    direct_hash = magnet_hash(target)
    if direct_hash:
        return direct_hash
    if not target.startswith(("http://", "https://")):
        return ""
    try:
        for _redirect in range(2):
            response = requests.get(target, timeout=(1, 1), allow_redirects=False)
            if 300 <= response.status_code < 400:
                location = (response.headers.get("Location") or "").strip()
                redirect_hash = magnet_hash(location)
                if redirect_hash:
                    return redirect_hash
                if not location:
                    return ""
                target = urljoin(target, location)
                continue
            response.raise_for_status()
            content = response.content or b""
            text = content[:4096].decode("utf-8", errors="ignore").strip()
            text_hash = magnet_hash(text.splitlines()[0] if text else "")
            if text_hash:
                return text_hash
            if content.startswith(b"d"):
                return normalize_info_hash(torrent_info(content).get("hash"))
            return ""
    except Exception:
        return ""
    return ""


def sizes_may_match(left: Any, right: Any) -> bool:
    left_size = int_value(left)
    right_size = int_value(right)
    if left_size <= 0 or right_size <= 0:
        return False
    tolerance = max(16 * 1024 * 1024, int(max(left_size, right_size) * 0.001))
    return abs(left_size - right_size) <= tolerance


def enrich_candidate_info_hashes(rows: list[dict]) -> list[dict]:
    normalized = [normalize_result(row) for row in rows]
    cached = cached_info_hashes()
    checked = cached_identity_checks()
    for row in normalized:
        if not row.get("info_hash"):
            identity_key = result_identity_key(row)
            row["info_hash"] = cached.get(identity_key, "")
            row["identity_checked"] = identity_key in checked

    by_title: dict[str, list[dict]] = {}
    for row in normalized:
        title_key = compact_search_text(str(row.get("title") or ""))
        if title_key:
            by_title.setdefault(title_key, []).append(row)
    candidates: list[dict] = []
    for group in by_title.values():
        known = [row for row in group if row.get("info_hash")]
        if not known:
            continue
        candidates.extend(
            row
            for row in group
            if not row.get("info_hash")
            and not row.get("identity_checked")
            and any(sizes_may_match(row.get("size"), other.get("size")) for other in known)
        )
    if candidates:
        workers = min(24, len(candidates))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="torrent-identity") as executor:
            hashes = executor.map(resolve_result_info_hash, candidates)
            for row, info_hash in zip(candidates, hashes):
                row["info_hash"] = normalize_info_hash(info_hash)
                row["identity_checked"] = True
    return normalized


def source_rank(row: dict) -> tuple[int, int, int, str]:
    return (
        int_value(row.get("seeders")),
        int_value(row.get("peers")),
        1 if str(row.get("download_url") or "").startswith("magnet:") else 0,
        str(row.get("tracker") or "").lower(),
    )


def deduplicate_exact_results(rows: list[dict]) -> list[dict]:
    buckets: list[list[dict]] = []
    by_hash: dict[str, list[dict]] = {}
    for row in enrich_candidate_info_hashes(rows):
        info_hash = normalize_info_hash(row.get("info_hash"))
        if not info_hash:
            buckets.append([row])
            continue
        bucket = by_hash.get(info_hash)
        if bucket is None:
            bucket = []
            by_hash[info_hash] = bucket
            buckets.append(bucket)
        bucket.append(row)

    results: list[dict] = []
    for bucket in buckets:
        unique_sources: dict[str, dict] = {}
        for row in bucket:
            source_key = str(row.get("tracker_id") or row.get("tracker") or row.get("download_url") or row.get("id"))
            current = unique_sources.get(source_key)
            if current is None or source_rank(row) > source_rank(current):
                unique_sources[source_key] = row
        sources = sorted(unique_sources.values(), key=source_rank, reverse=True)
        primary = dict(sources[0])
        primary.pop("id", None)
        if len(sources) > 1:
            primary["source_count"] = len(sources)
            primary["sources"] = [normalized_source(source) for source in sources]
        results.append(normalize_result(primary))
    return results


def search_jackett_many(query: str, indexers: list[str], category: str = "auto", metadata: dict | None = None) -> list[dict]:
    intent = parse_query_intent(query, category, metadata)
    rows = []
    seen = set()
    targets = indexers or ["all"]
    for candidate in jackett_candidate_queries(query, intent):
        for indexer in targets:
            for row in search_jackett(candidate, indexer):
                title_key = compact_search_text(str(row.get("title") or ""))
                key = f"{row.get('tracker_id')}::{title_key}::{row.get('size')}" if title_key else row.get("download_url")
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
        filtered = filter_search_results(rows, query, intent)
        if filtered:
            return deduplicate_exact_results(filtered)
    return deduplicate_exact_results(filter_search_results(rows, query, intent))


def load_result_cache() -> dict:
    try:
        if RESULT_CACHE_PATH.exists():
            data = json.loads(RESULT_CACHE_PATH.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("result cache load failed error=%s", str(exc)[:160])
    return {}


def save_result_cache(cache: dict) -> None:
    tmp = RESULT_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(RESULT_CACHE_PATH)


def cache_results(rows: list[dict]) -> None:
    now = int(time.time())
    with RESULT_CACHE_LOCK:
        cache = load_result_cache()
        fresh = {
            key: value
            for key, value in cache.items()
            if isinstance(value, dict) and now - int(value.get("ts") or 0) < RESULT_CACHE_TTL_SEC
        }
        for row in rows:
            result = normalize_result(row)
            fresh[result["id"]] = {"ts": now, "result": result}
        if len(fresh) > RESULT_CACHE_LIMIT:
            ordered = sorted(fresh.items(), key=lambda item: int(item[1].get("ts") or 0), reverse=True)
            fresh = dict(ordered[:RESULT_CACHE_LIMIT])
        save_result_cache(fresh)


def cached_result(result_id: str) -> dict | None:
    result_id = str(result_id or "").strip()
    if not result_id:
        return None
    now = int(time.time())
    with RESULT_CACHE_LOCK:
        cache = load_result_cache()
        entry = cache.get(result_id)
        if not isinstance(entry, dict):
            return None
        if now - int(entry.get("ts") or 0) >= RESULT_CACHE_TTL_SEC:
            cache.pop(result_id, None)
            save_result_cache(cache)
            return None
        result = entry.get("result")
        return normalize_result(result) if isinstance(result, dict) else None


def search_response(query: str, indexers: list[str], category: str = "auto", metadata: dict | None = None):
    if not query:
        return jsonify({"ok": False, "error": "busqueda vacia"}), 400
    trace_id = arr_trace.trace_id(
        "search",
        {"query": query, "indexers": indexers, "category": category, "metadata": metadata or {}},
    )
    arr_trace.start(
        "search",
        trace_id,
        {"query": query, "indexers": indexers or ["all"], "category": category or "auto", "metadata": metadata or {}},
    )
    try:
        results = search_jackett_many(query, indexers, category, metadata)
        cache_results(results)
        try:
            search_history.record(
                query,
                category or "auto",
                results,
                "done",
                search_history_source(indexers, metadata),
            )
        except Exception as history_error:
            logger.warning("search history record failed error=%s", str(history_error)[:180])
        arr_trace.finish("search", trace_id, "done", {"count": len(results)})
        logger.info("search q=%s indexers=%s category=%s results=%s", query, ",".join(indexers) or "all", category or "auto", len(results))
        return jsonify({
            "ok": True,
            "trace_id": trace_id,
            "query": query,
            "category": category or "auto",
            "indexers": indexers or ["all"],
            "count": len(results),
            "results": results,
        })
    except Exception as exc:
        try:
            search_history.record(
                query,
                category or "auto",
                [],
                "error",
                search_history_source(indexers, metadata),
            )
        except Exception as history_error:
            logger.warning("search history error record failed error=%s", str(history_error)[:180])
        arr_trace.finish("search", trace_id, "error", error=str(exc)[:500])
        logger.exception("search failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


def job_fingerprint(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def emit_download_progress(progress: Callable[..., None] | None, phase: str, label: str, tone: str, **extra: Any) -> None:
    if not callable(progress):
        return
    payload = {
        "phase": phase,
        "label": label,
        "tone": tone,
    }
    payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    progress(payload)


def download_job_stale_after_sec(job: dict) -> int:
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    phase = str(progress.get("phase") or "")
    settings = load_settings()
    rdt = settings.get("rdt", {}) if isinstance(settings.get("rdt"), dict) else {}
    start_timeout = max(45, int(rdt.get("start_timeout_sec") or 120))
    ready_timeout = max(1, int(rdt.get("ready_timeout_sec") or 5))
    if phase == "qbit_sending":
        return 90
    if phase == "rdt_sending":
        return start_timeout + ready_timeout + 45
    return start_timeout + ready_timeout + 90


def enrich_download_job_public(payload: dict, job: dict) -> None:
    if job.get("kind") != "download":
        return
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    payload["progress"] = progress
    payload["stale_after_sec"] = download_job_stale_after_sec(job)
    updated_at = int(job.get("updated_at") or job.get("created_at") or 0)
    age = max(0, int(time.time()) - updated_at)
    payload["active_age_sec"] = age
    payload["recoverable"] = job.get("state") in {"error", "interrupted"} or (
        job.get("state") in {"queued", "running"} and age >= int(payload["stale_after_sec"])
    )


def public_job(job: dict) -> dict:
    payload = {
        key: job.get(key)
        for key in ("id", "kind", "state", "created_at", "updated_at", "request", "result", "error")
    }
    enrich_download_job_public(payload, job)
    return payload


def run_search_job(
    query: str,
    indexers: list[str],
    category: str,
    metadata: dict,
    trace_id: str = "",
) -> dict:
    trace_id = trace_id or arr_trace.trace_id(
        "search-job",
        {"query": query, "indexers": indexers, "category": category, "metadata": metadata or {}},
    )
    arr_trace.start(
        "search",
        trace_id,
        {"query": query, "indexers": indexers or ["all"], "category": category or "auto", "metadata": metadata or {}},
    )
    try:
        results = search_jackett_many(query, indexers, category, metadata)
        cache_results(results)
        try:
            search_history.record(
                query,
                category or "auto",
                results,
                "done",
                search_history_source(indexers, metadata),
            )
        except Exception as history_error:
            logger.warning("search job history record failed error=%s", str(history_error)[:180])
        arr_trace.finish("search", trace_id, "done", {"count": len(results)})
    except Exception as exc:
        try:
            search_history.record(
                query,
                category or "auto",
                [],
                "error",
                search_history_source(indexers, metadata),
            )
        except Exception as history_error:
            logger.warning("search job history error record failed error=%s", str(history_error)[:180])
        arr_trace.finish("search", trace_id, "error", error=str(exc)[:500])
        raise
    logger.info(
        "search job q=%s indexers=%s category=%s results=%s",
        query,
        ",".join(indexers) or "all",
        category or "auto",
        len(results),
    )
    return {
        "trace_id": trace_id,
        "query": query,
        "category": category or "auto",
        "indexers": indexers or ["all"],
        "count": len(results),
        "results": results,
    }


def result_from_download_payload(payload: dict) -> dict:
    result_id = str(payload.get("result_id") or payload.get("id") or "").strip()
    result = cached_result(result_id) if result_id else None
    if not result and isinstance(payload.get("result"), dict):
        result = normalize_result(payload["result"])
    if not result:
        title = str(payload.get("title") or "jackett").strip()
        download_url = str(payload.get("download_url") or payload.get("magnet") or "").strip()
        if not download_url:
            if result_id:
                raise ValueError("result_id no encontrado o caducado")
            raise ValueError("falta result_id o download_url")
        result = normalize_result({
            "title": title,
            "download_url": download_url,
            "tracker": payload.get("tracker", ""),
            "tracker_id": payload.get("tracker_id", ""),
            "size": payload.get("size", ""),
            "seeders": payload.get("seeders", 0),
            "peers": payload.get("peers", 0),
        })
    return result


def download_torrent_payload(url: str, attempts: int = 4) -> tuple[bytes, str]:
    target = absolute_jackett_url(url)
    last_error = None
    attempt_count = max(1, min(int(attempts or 1), 4))
    for attempt in range(attempt_count):
        try:
            for _redirect in range(6):
                response = requests.get(target, timeout=(15, 90), allow_redirects=False)
                if 300 <= response.status_code < 400:
                    location = (response.headers.get("Location") or "").strip()
                    if location.lower().startswith("magnet:"):
                        return b"", location
                    if not location:
                        break
                    target = urljoin(target, location)
                    continue
                break
            response.raise_for_status()
            content = response.content or b""
            text = content[:4096].decode("utf-8", errors="ignore").strip()
            if text.lower().startswith("magnet:"):
                return b"", text.splitlines()[0].strip()
            if not content.startswith(b"d"):
                text = content[:120].decode("utf-8", errors="ignore")
                raise RuntimeError(f"Jackett no ha devuelto un .torrent valido: {text}")
            return content, ""
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < attempt_count:
                time.sleep(3 + attempt * 3)
    raise RuntimeError(f"No he podido descargar el .torrent: {str(last_error)[:180]}")


def api_error(prefix: str, response: requests.Response) -> str:
    try:
        data = response.json()
        detail = data.get("error") or data.get("message") or response.reason
        code = data.get("error_code")
        if code:
            return f"{prefix} HTTP {response.status_code}: {detail} ({code})"
        return f"{prefix} HTTP {response.status_code}: {detail}"
    except Exception:
        text = (response.text or response.reason or "").strip().replace("\n", " ")[:180]
        return f"{prefix} HTTP {response.status_code}: {text or response.reason}"


def retry_delay(response: requests.Response | None, attempt: int) -> int:
    if response is not None:
        raw = response.headers.get("Retry-After", "").strip()
        if raw.isdigit():
            return min(45, max(2, int(raw)))
    return min(20, 2 + attempt * 3)


def retryable_response(response: requests.Response) -> bool:
    if response.status_code in RETRYABLE_HTTP:
        return True
    text = (response.text or "").lower()
    return "database is locked" in text or "database table is locked" in text


def real_debrid_request(method: str, path: str, **kwargs) -> Any:
    if not REAL_DEBRID_TOKEN:
        raise RuntimeError("falta REAL_DEBRID_TOKEN")
    headers = kwargs.pop("headers", {}) or {}
    timeout = kwargs.pop("timeout", 60)
    headers.update(
        {
            "Authorization": f"Bearer {REAL_DEBRID_TOKEN}",
            "Accept": "application/json",
            "User-Agent": "Buscador-Puente-ARR/1.0",
        }
    )
    attempts = load_settings().get("rdt", {}).get("rd_retry_attempts", 5)
    last_response = None
    for attempt in range(attempts):
        response = requests.request(method, f"{REAL_DEBRID_API}{path}", headers=headers, timeout=timeout, **kwargs)
        if response.status_code < 400:
            if response.status_code in {202, 204} or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {}
        last_response = response
        if not retryable_response(response) or attempt == attempts - 1:
            break
        time.sleep(retry_delay(response, attempt))
    raise RuntimeError(api_error("Real-Debrid", last_response))


def real_debrid_select_all(torrent_id: str) -> None:
    last_error = None
    for _attempt in range(6):
        try:
            real_debrid_request(
                "POST",
                f"/torrents/selectFiles/{quote(torrent_id, safe='')}",
                data={"files": "all"},
                timeout=35,
            )
            return
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(str(last_error or "Real-Debrid no ha aceptado la seleccion"))


def real_debrid_add_torrent(raw: bytes) -> str:
    data = real_debrid_request(
        "PUT",
        "/torrents/addTorrent",
        data=raw,
        headers={"Content-Type": "application/x-bittorrent"},
        timeout=80,
    )
    torrent_id = str(data.get("id") or "")
    if not torrent_id:
        raise RuntimeError("Real-Debrid no ha devuelto id")
    real_debrid_select_all(torrent_id)
    return torrent_id


def real_debrid_add_magnet(magnet: str) -> str:
    data = real_debrid_request(
        "POST",
        "/torrents/addMagnet",
        data={"magnet": magnet},
        timeout=60,
    )
    torrent_id = str(data.get("id") or "")
    if not torrent_id:
        raise RuntimeError("Real-Debrid no ha devuelto id")
    real_debrid_select_all(torrent_id)
    return torrent_id


def real_debrid_delete(torrent_id: str) -> None:
    if torrent_id:
        try:
            real_debrid_request("DELETE", f"/torrents/delete/{quote(torrent_id, safe='')}", timeout=25)
        except Exception as exc:
            logger.warning("rd cleanup failed id=%s error=%s", torrent_id, str(exc)[:180])


def real_debrid_ids() -> set[str]:
    try:
        rows = real_debrid_request("GET", "/torrents", timeout=25)
        if isinstance(rows, list):
            return {str(row.get("id") or "") for row in rows if str(row.get("id") or "")}
    except Exception as exc:
        logger.warning("rd list failed error=%s", str(exc)[:180])
    return set()


def real_debrid_preflight(raw: bytes | None = None, magnet: str = "") -> dict:
    if magnet:
        torrent_id = real_debrid_add_magnet(magnet)
    elif raw:
        torrent_id = real_debrid_add_torrent(raw)
    else:
        raise RuntimeError("entrada RD vacia")
    return {"ok": True, "rd_id": torrent_id}


def normalized_category(category: str) -> str:
    category = (category or "").strip().lower()
    if category in {"movies", "tv", "manual"}:
        return category
    return load_settings().get("qbit", {}).get("default_category", "manual")


def rdt_login() -> requests.Session:
    if not RDT_BASE or not RDT_USER or not RDT_PASS:
        raise RuntimeError("falta configuracion de RDT-Client")
    session = requests.Session()
    last_response = None
    for attempt in range(5):
        response = session.post(
            f"{RDT_BASE}/Api/Authentication/Login",
            json={"userName": RDT_USER, "password": RDT_PASS},
            timeout=20,
        )
        if response.status_code < 400:
            text = (response.text or "").strip()
            if text and text not in {"Ok.", "Ok"}:
                raise RuntimeError(f"RDT-Client login: {text[:120]}")
            return session
        last_response = response
        if not retryable_response(response) or attempt == 4:
            break
        time.sleep(retry_delay(response, attempt))
    raise RuntimeError(api_error("RDT-Client", last_response))


def rdt_qbit_login() -> requests.Session:
    if not RDT_BASE or not RDT_USER or not RDT_PASS:
        raise RuntimeError("falta configuracion de RDT-Client")
    session = requests.Session()
    last_response = None
    for attempt in range(5):
        response = session.post(
            f"{RDT_BASE}/api/v2/auth/login",
            data={"username": RDT_USER, "password": RDT_PASS},
            timeout=30,
        )
        if response.status_code < 400 and response.text.strip().lower().startswith("ok"):
            return session
        last_response = response
        if not retryable_response(response) or attempt == 4:
            break
        time.sleep(retry_delay(response, attempt))
    raise RuntimeError(api_error("RDT-Client", last_response))


def rdt_save_path(category: str) -> str:
    return f"{RDT_SAVE_ROOT}/{normalized_category(category)}"


def qbit_save_path(category: str) -> str:
    return f"{QBIT_SAVE_ROOT}/{normalized_category(category)}"


def rdt_json(session: requests.Session, path: str) -> Any:
    last_response = None
    for attempt in range(5):
        response = session.get(f"{RDT_BASE}{path}", timeout=30)
        if response.status_code < 400:
            return response.json()
        last_response = response
        if not retryable_response(response) or attempt == 4:
            break
        time.sleep(retry_delay(response, attempt))
    raise RuntimeError(api_error("RDT-Client", last_response))


def rdt_post(session: requests.Session, path: str, payload: dict) -> Any:
    last_response = None
    for attempt in range(5):
        response = session.post(f"{RDT_BASE}{path}", json=payload, timeout=45)
        if response.status_code < 400:
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {}
        last_response = response
        if not retryable_response(response) or attempt == 4:
            break
        time.sleep(retry_delay(response, attempt))
    raise RuntimeError(api_error("RDT-Client", last_response))


def rdt_delete(session: requests.Session, torrent_id: str) -> None:
    if not torrent_id:
        return
    payload = {"deleteData": True, "deleteRdTorrent": True, "deleteLocalFiles": True}
    last_error = None
    for _attempt in range(6):
        try:
            rdt_post(session, f"/Api/Torrents/Delete/{quote(torrent_id, safe='')}", payload)
            return
        except Exception as exc:
            last_error = exc
        time.sleep(2)
    logger.warning("rdt cleanup failed id=%s error=%s", torrent_id, str(last_error)[:180])


def rdt_cleanup_finished(session: requests.Session, torrent_id: str) -> None:
    if not torrent_id:
        return
    payload = {"deleteData": True, "deleteRdTorrent": True, "deleteLocalFiles": False}
    last_error = None
    for _attempt in range(6):
        try:
            rdt_post(session, f"/Api/Torrents/Delete/{quote(torrent_id, safe='')}", payload)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"RDT finished cleanup failed id={torrent_id} error={str(last_error)[:180]}")


def rdt_settings(category: str, manual_files: str = "") -> dict:
    cat = normalized_category(category)
    return {
        "category": cat,
        "hostDownloadAction": 0,
        "downloadAction": 2 if manual_files else 0,
        "finishedAction": 1,
        "finishedActionDelay": 0,
        "downloadMinSize": 0,
        "includeRegex": "",
        "excludeRegex": "",
        "downloadManualFiles": manual_files or None,
        "priority": 0,
        "torrentRetryAttempts": 1,
        "downloadRetryAttempts": 3,
        "deleteOnError": 0,
        "lifetime": 0,
        "downloadClient": 0,
        "type": 0,
    }


def rdt_upload_file_response(session: requests.Session, files: dict) -> requests.Response:
    last_response = None
    for attempt in range(5):
        response = session.post(f"{RDT_BASE}/Api/Torrents/UploadFile", files=files, timeout=80)
        if response.status_code < 400:
            return response
        last_response = response
        if not retryable_response(response) or attempt == 4:
            break
        time.sleep(retry_delay(response, attempt))
    return last_response


def rdt_upload_magnet_response(session: requests.Session, magnet: str, category: str) -> requests.Response:
    payload = {"magnetLink": magnet, "torrent": rdt_settings(category, "")}
    last_response = None
    for attempt in range(5):
        response = session.post(f"{RDT_BASE}/Api/Torrents/UploadMagnet", json=payload, timeout=80)
        if response.status_code < 400:
            return response
        last_response = response
        if not retryable_response(response) or attempt == 4:
            break
        time.sleep(retry_delay(response, attempt))
    return last_response


def rdt_new_id(session: requests.Session, before: set[str], expected_hash: str = "") -> str:
    timeout = max(45, int(load_settings().get("rdt", {}).get("start_timeout_sec", 120)))
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = rdt_json(session, "/Api/Torrents") or []
        for row in rows:
            torrent_id = str(row.get("torrentId") or "")
            row_hash = str(row.get("hash") or "").lower()
            if torrent_id and expected_hash and row_hash == expected_hash:
                if torrent_id not in before:
                    return torrent_id
            if torrent_id and torrent_id not in before and (not expected_hash or not row_hash or row_hash == expected_hash):
                return torrent_id
        time.sleep(1)
    raise RuntimeError("RDT no ha creado entrada")


def rdt_find_row(session: requests.Session, torrent_id: str, expected_hash: str = "", before: set[str] | None = None) -> dict:
    before = before or set()
    rows = rdt_json(session, "/Api/Torrents") or []
    for row in rows:
        if str(row.get("torrentId") or "") == torrent_id:
            return row
    if expected_hash:
        for row in rows:
            row_id = str(row.get("torrentId") or "")
            row_hash = str(row.get("hash") or "").lower()
            if row_id and row_id not in before and row_hash == expected_hash:
                return row
    return {}


def rdt_ready_result(torrent_id: str, row: dict, pending: bool = False) -> dict:
    status = str(row.get("statusText") or "")
    raw_status = str(row.get("rdStatusRaw") or "")
    error = str(row.get("error") or "")
    try:
        downloads = int(row.get("downloadsCount") or 0)
    except (TypeError, ValueError):
        downloads = 0
    files_selected = bool(row.get("filesSelected"))
    low = " ".join([status, raw_status, error]).lower()
    if error or "failed" in low or "error" in low:
        raise RuntimeError((error or status or "RDT fallo")[:180])
    result = {
        "engine": "RDT-Client",
        "rdt_id": str(row.get("torrentId") or torrent_id),
        "status": status or raw_status,
        "downloads": downloads,
        "files_selected": files_selected,
    }
    if pending:
        result["pending"] = True
    return result


def rdt_wait_ready(session: requests.Session, torrent_id: str, expected_hash: str = "", before: set[str] | None = None) -> dict:
    timeout = int(load_settings().get("rdt", {}).get("ready_timeout_sec", 5))
    deadline = time.time() + max(1, timeout)
    last = {}
    while time.time() < deadline:
        try:
            last = rdt_json(session, f"/Api/Torrents/Get/{quote(torrent_id, safe='')}") or {}
        except Exception as exc:
            error = str(exc)
            if "HTTP 404" in error or "Not Found" in error:
                row = rdt_find_row(session, torrent_id, expected_hash, before)
                if row:
                    last = row
                else:
                    last = {"statusText": error}
                    time.sleep(1)
                    continue
            else:
                raise
        if not last:
            time.sleep(1)
            continue
        result = rdt_ready_result(torrent_id, last, False)
        status = str(last.get("statusText") or "")
        raw_status = str(last.get("rdStatusRaw") or "")
        error = str(last.get("error") or "")
        downloads = int(result.get("downloads") or 0)
        low = " ".join([status, raw_status, error]).lower()
        if downloads > 0 or "finished" in low or "downloaded" in low or "completed" in low:
            return result
        time.sleep(1)
    raise RuntimeError((f"RDT no listo en {timeout}s: " + str(last.get("statusText") or "sin estado"))[:180])


def rdt_upload_torrent(raw: bytes, title: str, category: str, cleanup: bool = False) -> dict:
    session = rdt_qbit_login()
    before = {str(row.get("torrentId") or "") for row in (rdt_json(session, "/Api/Torrents") or [])}
    info = torrent_info(raw)
    cat = normalized_category(category)
    response = session.post(
        f"{RDT_BASE}/api/v2/torrents/add",
        data={
            "category": cat,
            "savepath": rdt_save_path(cat),
            "paused": "false",
            "autoTMM": "false",
        },
        files={"torrents": (safe_name(title), raw, "application/x-bittorrent")},
        timeout=80,
    )
    if response.status_code >= 400:
        raise RuntimeError(api_error("RDT-Client", response))
    rdt_id = ""
    created = False
    row = {}
    rdt_id = rdt_new_id(session, before, info["hash"])
    created = rdt_id not in before
    try:
        result = rdt_wait_ready(session, rdt_id, info["hash"], before)
        if cleanup:
            rdt_delete(session, rdt_id)
            result["cleaned"] = True
        result["hash"] = info["hash"]
        return result
    except Exception:
        if created:
            rdt_delete(session, rdt_id)
        raise


def rdt_upload_magnet(magnet: str, category: str, cleanup: bool = False) -> dict:
    session = rdt_qbit_login()
    before = {str(row.get("torrentId") or "") for row in (rdt_json(session, "/Api/Torrents") or [])}
    cat = normalized_category(category)
    info_hash = magnet_hash(magnet)
    response = session.post(
        f"{RDT_BASE}/api/v2/torrents/add",
        data={
            "urls": magnet,
            "category": cat,
            "savepath": rdt_save_path(cat),
            "paused": "false",
            "autoTMM": "false",
        },
        timeout=80,
    )
    if response.status_code >= 400:
        raise RuntimeError(api_error("RDT-Client", response))
    rdt_id = ""
    created = False
    row = {}
    expected_hash = info_hash if len(info_hash) == 40 else ""
    rdt_id = rdt_new_id(session, before, expected_hash)
    created = rdt_id not in before
    try:
        result = rdt_wait_ready(session, rdt_id, expected_hash, before)
        if cleanup:
            rdt_delete(session, rdt_id)
            result["cleaned"] = True
        result["hash"] = info_hash
        return result
    except Exception:
        if created:
            rdt_delete(session, rdt_id)
        raise


def qbit_login() -> requests.Session:
    if not QBIT_BASE or not QBIT_USER or not QBIT_PASS:
        raise RuntimeError("falta configuracion de qBittorrent")
    session = requests.Session()
    response = session.post(
        f"{QBIT_BASE}/api/v2/auth/login",
        data={"username": QBIT_USER, "password": QBIT_PASS},
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError(api_error("qBittorrent", response))
    text = (response.text or "").strip()
    if text and text not in {"Ok.", "Ok"}:
        raise RuntimeError(f"qBittorrent login: {text[:120]}")
    return session


def qbit_add_torrent(raw: bytes, title: str, category: str, cleanup: bool = False) -> dict:
    session = qbit_login()
    info = torrent_info(raw)
    cat = normalized_category(category)
    paused = "true" if load_settings().get("qbit", {}).get("add_paused") else "false"
    response = session.post(
        f"{QBIT_BASE}/api/v2/torrents/add",
        data={"category": cat, "savepath": qbit_save_path(cat), "paused": paused, "autoTMM": "false"},
        files={"torrents": (safe_name(title), raw, "application/x-bittorrent")},
        timeout=60,
    )
    if response.status_code == 409:
        return {"engine": "qBittorrent", "hash": info["hash"], "duplicate": True, "cleaned": False}
    if response.status_code >= 400:
        raise RuntimeError(api_error("qBittorrent", response))
    if cleanup:
        qbit_delete(session, info["hash"])
    return {"engine": "qBittorrent", "hash": info["hash"], "cleaned": cleanup}


def qbit_add_magnet(magnet: str, category: str, cleanup: bool = False) -> dict:
    session = qbit_login()
    cat = normalized_category(category)
    paused = "true" if load_settings().get("qbit", {}).get("add_paused") else "false"
    response = session.post(
        f"{QBIT_BASE}/api/v2/torrents/add",
        data={"urls": magnet, "category": cat, "savepath": qbit_save_path(cat), "paused": paused, "autoTMM": "false"},
        timeout=60,
    )
    info_hash = magnet_hash(magnet)
    if response.status_code == 409:
        return {"engine": "qBittorrent", "hash": info_hash, "duplicate": True, "cleaned": False}
    if response.status_code >= 400:
        raise RuntimeError(api_error("qBittorrent", response))
    if cleanup and info_hash:
        qbit_delete(session, info_hash)
    return {"engine": "qBittorrent", "hash": info_hash, "cleaned": cleanup}


def qbit_delete(session: requests.Session, info_hash: str) -> None:
    if not info_hash:
        return
    for _attempt in range(4):
        response = session.post(
            f"{QBIT_BASE}/api/v2/torrents/delete",
            data={"hashes": info_hash, "deleteFiles": "true"},
            timeout=20,
        )
        if response.status_code < 400:
            return
        time.sleep(1)
    logger.warning("qbit cleanup failed hash=%s", info_hash)


def load_monitor_state() -> dict:
    with MONITOR_LOCK:
        try:
            if MONITOR_STATE_PATH.exists():
                data = json.loads(MONITOR_STATE_PATH.read_text(encoding="utf-8") or "{}")
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("monitor state load failed error=%s", str(exc)[:160])
        return {}


def save_monitor_state(state: dict) -> None:
    with MONITOR_LOCK:
        tmp = MONITOR_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(MONITOR_STATE_PATH)


def reusable_submission_result(row: dict, title: str, requested_category: str, category: str) -> dict:
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    result = dict(result or {})
    result.update({
        "ok": True,
        "title": result.get("title") or title,
        "category": result.get("category") or category,
        "requested_category": result.get("requested_category") or requested_category or "auto",
        "engine": result.get("engine") or row.get("engine") or "ya_enviado",
        "submission_state": row.get("state"),
        "submission_key": row.get("key"),
        "duplicate_guard": True,
        "message": "Ya estaba enviado o vigilado; no lo repito.",
    })
    return result


def finish_submission(
    key: str,
    state: str,
    result: dict,
    title: str,
    requested_category: str,
    category: str,
) -> dict:
    result = dict(result or {})
    result.update({
        "ok": True,
        "title": title,
        "category": category,
        "requested_category": requested_category or "auto",
        "submission_state": state,
        "submission_key": key,
    })
    submissions.update(
        key,
        state=state,
        engine=result.get("engine", ""),
        rdt_id=result.get("rdt_id", ""),
        qbit_hash=result.get("hash", ""),
        result=result,
    )
    return result


def cleanup_orphan_monitor_artifacts(min_age_sec: int = MONITOR_ORPHAN_CLEANUP_SEC) -> int:
    now = time.time()
    state = load_monitor_state()
    referenced: set[Path] = set()
    for item in state.values():
        torrent_path = str(item.get("torrent_path") or "").strip()
        if not torrent_path:
            continue
        try:
            path = Path(torrent_path)
            if not path.is_absolute():
                path = MONITOR_DIR / path
            referenced.add(path.resolve())
        except Exception:
            continue

    cleaned = 0
    monitor_root = MONITOR_DIR.resolve()
    for path in MONITOR_DIR.glob("*.torrent"):
        try:
            resolved = path.resolve()
            if monitor_root not in resolved.parents:
                continue
            if resolved in referenced:
                continue
            age = now - path.stat().st_mtime
            if age < min_age_sec:
                continue
            resolved.unlink(missing_ok=True)
            cleaned += 1
        except Exception as exc:
            logger.warning("monitor orphan cleanup failed file=%s error=%s", path.name, str(exc)[:160])
    if cleaned:
        logger.info("monitor orphan artifacts cleaned count=%s", cleaned)
    return cleaned


def cleanup_monitor_artifact(item: dict, reason: str) -> None:
    torrent_path = str(item.get("torrent_path") or "").strip()
    if not torrent_path:
        return
    try:
        path = Path(torrent_path)
        if not path.is_absolute():
            path = MONITOR_DIR / path
        resolved = path.resolve()
        monitor_root = MONITOR_DIR.resolve()
        if monitor_root not in resolved.parents and resolved != monitor_root:
            logger.warning("monitor artifact cleanup skipped outside dir path=%s", torrent_path)
            return
        resolved.unlink(missing_ok=True)
        logger.info("monitor artifact cleaned path=%s reason=%s", resolved.name, reason)
    except Exception as exc:
        logger.warning("monitor artifact cleanup failed path=%s error=%s", torrent_path, str(exc)[:160])


def register_monitor(result: dict, title: str, category: str, raw: bytes | None = None, magnet: str = "", submission_key_value: str = "") -> None:
    rdt_id = str(result.get("rdt_id") or "")
    if not rdt_id:
        return
    now = int(time.time())
    item = {
        "rdt_id": rdt_id,
        "title": title,
        "category": normalized_category(category),
        "first_seen": now,
        "last_progress_ts": now,
        "last_progress": -1.0,
        "last_status": str(result.get("status") or ""),
        "finished_seen_ts": 0,
        "submission_key": submission_key_value,
    }
    if magnet:
        item["kind"] = "magnet"
        item["magnet"] = magnet
        item["hash"] = magnet_hash(magnet)
    elif raw:
        info = torrent_info(raw)
        torrent_path = MONITOR_DIR / f"{info['hash']}.torrent"
        if not torrent_path.exists():
            torrent_path.write_bytes(raw)
        item["kind"] = "torrent"
        item["hash"] = info["hash"]
        item["torrent_path"] = str(torrent_path)
    else:
        return
    state = load_monitor_state()
    state[rdt_id] = item
    save_monitor_state(state)


def status_progress(row: dict) -> float:
    for key in ("progress", "rdProgress", "downloadProgress", "percent", "percentage"):
        value = row.get(key)
        if value is None:
            continue
        try:
            number = float(str(value).replace(",", "."))
            return number * 100 if 0 <= number <= 1 else number
        except ValueError:
            pass
    text = " ".join(str(row.get(key) or "") for key in ("statusText", "rdStatusRaw", "status", "error"))
    match = re.search(r"(\d+(?:[\.,]\d+)?)\s*%", text)
    if match:
        try:
            return float(match.group(1).replace(",", "."))
        except ValueError:
            return -1.0
    return -1.0


def rdt_status_text(row: dict) -> str:
    return " ".join(str(row.get(key) or "") for key in ("statusText", "rdStatusRaw", "status", "error")).strip()


def rdt_local_download_complete(row: dict) -> bool:
    downloads = row.get("downloads")
    if isinstance(downloads, list):
        if not downloads:
            return False
        return all(
            isinstance(download, dict)
            and download.get("completed") not in (None, "", False)
            for download in downloads
        )

    return row.get("completed") not in (None, "", False)


def monitor_is_finished(row: dict) -> bool:
    return rdt_local_download_complete(row)


def monitor_should_fallback(row: dict, item: dict, settings: dict, now: int) -> tuple[bool, str]:
    text = rdt_status_text(row)
    low = text.lower()
    if any(word in low for word in ("error", "failed", "fallo", "missing")):
        return True, text[:160] or "estado malo"

    progress = status_progress(row)
    downloads = int(row.get("downloadsCount") or 0)
    if downloads > 0 or "finished" in low or "downloaded" in low or "completed" in low:
        return False, "ya tiene descarga"

    last_progress = float(item.get("last_progress", -1))
    if progress > last_progress + 0.01:
        item["last_progress"] = progress
        item["last_progress_ts"] = now
        item["last_status"] = text
        return False, "ha progresado"

    last_ts = int(item.get("last_progress_ts") or item.get("first_seen") or now)
    quiet_for = now - last_ts
    rdt = settings.get("rdt", {})
    limit = int(rdt.get("ready_timeout_sec", 5))
    if quiet_for >= limit:
        return True, f"sin progreso {quiet_for}s progreso={progress:.2f}%"
    item["last_status"] = text
    return False, f"esperando {quiet_for}s progreso={progress:.2f}%"


def monitor_fallback(session: requests.Session, item: dict, reason: str, settings: dict) -> None:
    category = normalized_category(str(item.get("category") or ""))
    title = str(item.get("title") or "jackett")
    key = str(item.get("submission_key") or "")
    if key:
        submissions.update(key, state="fallback_to_qbit", last_error=reason)
    if item.get("kind") == "magnet":
        qbit = qbit_add_magnet(str(item.get("magnet") or ""), category, False)
    else:
        torrent_path = Path(str(item.get("torrent_path") or ""))
        if not torrent_path.exists():
            raise RuntimeError("no encuentro torrent guardado para fallback")
        qbit = qbit_add_torrent(torrent_path.read_bytes(), title, category, False)
    if settings.get("rdt", {}).get("cleanup_on_fallback", True):
        rdt_delete(session, str(item.get("rdt_id") or ""))
    if key:
        qbit_result = {
            **qbit,
            "ok": True,
            "title": title,
            "category": category,
            "requested_category": str(item.get("requested_category") or category),
            "fallback_from": reason[:180],
            "submission_key": key,
        }
        submissions.update(
            key,
            state="submitted_qbit",
            engine=qbit_result.get("engine", "qBittorrent"),
            qbit_hash=qbit_result.get("hash", ""),
            result=qbit_result,
            last_error=reason[:180],
        )
    cleanup_monitor_artifact(item, "fallback")
    logger.info("monitor fallback title=%s category=%s reason=%s", title, category, reason)


def monitor_cleanup_finished(session: requests.Session, key: str, item: dict, row: dict, now: int, state: dict) -> bool:
    text = rdt_status_text(row)
    progress = status_progress(row)
    try:
        old_progress = float(item.get("last_progress", -1))
    except (TypeError, ValueError):
        old_progress = -1.0

    item["last_progress"] = max(old_progress, progress, 100.0)
    item["last_progress_ts"] = now
    item["last_status"] = text or str(item.get("last_status") or "")

    finished_seen_ts = int(item.get("finished_seen_ts") or 0)
    if not finished_seen_ts:
        item["finished_seen_ts"] = now
        logger.info("monitor finished seen id=%s title=%s wait=%ss", key, str(item.get("title") or "")[:120], RDT_FINISHED_CLEANUP_DELAY_SEC)
        return False

    if now - finished_seen_ts < RDT_FINISHED_CLEANUP_DELAY_SEC:
        return False

    rdt_cleanup_finished(session, str(item.get("rdt_id") or key))
    submission_key_value = str(item.get("submission_key") or "")
    if submission_key_value:
        result = {
            "ok": True,
            "title": str(item.get("title") or ""),
            "category": str(item.get("category") or "manual"),
            "requested_category": str(item.get("requested_category") or item.get("category") or "manual"),
            "engine": "RDT-Client",
            "rdt_id": str(item.get("rdt_id") or key),
            "submission_key": submission_key_value,
        }
        submissions.update(
            submission_key_value,
            state="transport_done",
            engine="RDT-Client",
            rdt_id=str(item.get("rdt_id") or key),
            result=result,
        )
    cleanup_monitor_artifact(item, "finished")
    state.pop(key, None)
    logger.info("monitor cleaned finished rdt item id=%s preserve_local=true", key)
    return True


def monitor_missing_item(session: requests.Session, key: str, item: dict, settings: dict, now: int, state: dict) -> bool:
    row = rdt_find_row(session, str(item.get("rdt_id") or key), str(item.get("hash") or ""), set())
    if row:
        if monitor_is_finished(row):
            monitor_cleanup_finished(session, key, item, row, now, state)
            return True
        fallback, reason = monitor_should_fallback(row, item, settings, now)
        if fallback:
            monitor_fallback(session, item, reason, settings)
            state.pop(key, None)
        return True

    if monitor_is_finished(item):
        submission_key_value = str(item.get("submission_key") or "")
        if submission_key_value:
            submissions.update(submission_key_value, state="transport_done", engine="RDT-Client", rdt_id=str(item.get("rdt_id") or key))
        cleanup_monitor_artifact(item, "finished-missing")
        state.pop(key, None)
        logger.info("monitor removed finished missing rdt item id=%s", key)
        return True

    monitor_fallback(session, item, "rdt item missing before progress", settings)
    state.pop(key, None)
    logger.info("monitor fallback missing rdt item id=%s", key)
    return True


def monitor_once() -> None:
    settings = load_settings()
    if not settings.get("rdt", {}).get("fallback_enabled", True) or not settings.get("qbit", {}).get("fallback_enabled", True):
        return
    state = load_monitor_state()
    if not state:
        return
    session = rdt_login()
    now = int(time.time())
    changed = False
    for key, item in list(state.items()):
        try:
            row = rdt_json(session, f"/Api/Torrents/Get/{quote(str(item.get('rdt_id') or key), safe='')}") or {}
            if monitor_is_finished(row):
                monitor_cleanup_finished(session, key, item, row, now, state)
                changed = True
                continue
            fallback, reason = monitor_should_fallback(row, item, settings, now)
            if fallback:
                monitor_fallback(session, item, reason, settings)
                state.pop(key, None)
            changed = True
        except Exception as exc:
            error = str(exc)
            if "HTTP 404" in error or "Not Found" in error:
                monitor_missing_item(session, key, item, settings, now, state)
                changed = True
                continue
            item["last_error"] = error[:180]
            changed = True
            logger.warning("monitor item failed id=%s error=%s", key, error[:180])
    if changed:
        save_monitor_state(state)


def monitor_loop() -> None:
    while True:
        try:
            monitor_once()
        except Exception as exc:
            logger.warning("monitor loop failed error=%s", str(exc)[:180])
        time.sleep(60)


def start_monitor() -> None:
    global MONITOR_STARTED
    with MONITOR_START_LOCK:
        if MONITOR_STARTED:
            return
        thread = threading.Thread(target=monitor_loop, name="rdt-monitor", daemon=True)
        thread.start()
        MONITOR_STARTED = True


def create_app() -> Flask:
    cleanup_orphan_monitor_artifacts()
    start_monitor()
    return app


def deliver(
    title: str,
    download_url: str,
    category: str,
    cleanup: bool = False,
    source_result_id: str = "",
    trace_id: str = "",
    progress: Callable[..., None] | None = None,
) -> dict:
    with ENGINE_LOCK:
        settings = load_settings()
        requested_category = (category or "").strip().lower()
        category = resolved_category(requested_category, title, settings)
        fallback_enabled = settings.get("rdt", {}).get("fallback_enabled", True) and settings.get("qbit", {}).get("fallback_enabled", True)
        requested_for_key = requested_category or "auto"
        key = submission_key(title, download_url, requested_for_key, category, cleanup, source_result_id)
        trace_id = trace_id or f"download-{key[:16]}"
        arr_trace.start(
            "download",
            trace_id,
            {
                "title": title,
                "requested_category": requested_for_key,
                "resolved_category": category,
                "cleanup": cleanup,
                "source_result_id": source_result_id,
                "download_ref": download_url[:240],
            },
        )
        reused_row, reused = submissions.begin(
            key,
            title,
            requested_for_key,
            category,
            source_result_id,
            download_url,
            SUBMISSION_REUSE_SEC,
        )
        if reused and reused_row:
            arr_trace.finish(
                "download",
                trace_id,
                "reused",
                reusable_submission_result(reused_row, title, requested_for_key, category),
            )
            logger.info("download duplicate guarded title=%s category=%s state=%s key=%s", title, category, reused_row.get("state"), key[:12])
            return reusable_submission_result(reused_row, title, requested_for_key, category)

        emit_download_progress(progress, "rdt_sending", "Enviando a RD", "rd", engine="RDT-Client")
        if download_url.startswith("magnet:"):
            try:
                submissions.update(key, state="submitting_rdt", engine="RDT-Client")
                arr_trace.event("download", trace_id, "transport", "started", "Enviando magnet a RDT", {"engine": "RDT-Client"})
                result = rdt_upload_magnet(download_url, category, cleanup)
                if not cleanup:
                    register_monitor(result, title, category, magnet=download_url, submission_key_value=key)
                    final = finish_submission(key, "rdt_monitoring", result, title, requested_for_key, category)
                    arr_trace.finish("download", trace_id, "rdt_monitoring", final)
                    return final
                final = finish_submission(key, "transport_done", result, title, requested_for_key, category)
                arr_trace.finish("download", trace_id, "transport_done", final)
                return final
            except Exception as main_error:
                arr_trace.event("download", trace_id, "transport", "warning", "RDT fallo, evaluando fallback", {"error": str(main_error)[:500]})
                if not fallback_enabled:
                    submissions.update(key, state="transport_error", last_error=str(main_error)[:500])
                    arr_trace.finish("download", trace_id, "transport_error", error=str(main_error)[:500])
                    raise
                submissions.update(key, state="fallback_to_qbit", last_error=str(main_error)[:500])
                emit_download_progress(
                    progress,
                    "qbit_sending",
                    "Enviando a qB",
                    "qbit",
                    engine="qBittorrent",
                    fallback_from=str(main_error)[:180],
                )
                try:
                    arr_trace.event("download", trace_id, "fallback", "started", "Enviando magnet a qBittorrent", {"engine": "qBittorrent"})
                    qbit = qbit_add_magnet(download_url, category, cleanup)
                except Exception as fallback_error:
                    submissions.update(key, state="transport_error", last_error=f"RDT: {str(main_error)[:220]} | qB: {str(fallback_error)[:220]}")
                    arr_trace.finish(
                        "download",
                        trace_id,
                        "transport_error",
                        error=f"RDT: {str(main_error)[:220]} | qB: {str(fallback_error)[:220]}",
                    )
                    raise
                qbit["fallback_from"] = str(main_error)[:180]
                final = finish_submission(key, "submitted_qbit", qbit, title, requested_for_key, category)
                arr_trace.finish("download", trace_id, "submitted_qbit", final)
                return final

        try:
            raw, resolved_magnet = download_torrent_payload(download_url)
        except Exception as exc:
            arr_trace.finish("download", trace_id, "source_error", error=str(exc)[:500])
            raise
        arr_trace.event(
            "download",
            trace_id,
            "source",
            "finished",
            "Torrent resuelto",
            {"bytes": len(raw), "resolved_magnet": bool(resolved_magnet)},
        )
        try:
            submissions.update(key, state="submitting_rdt", engine="RDT-Client")
            arr_trace.event("download", trace_id, "transport", "started", "Enviando torrent a RDT", {"engine": "RDT-Client"})
            if resolved_magnet:
                result = rdt_upload_magnet(resolved_magnet, category, cleanup)
            else:
                result = rdt_upload_torrent(raw, title, category, cleanup)
            if not cleanup:
                if resolved_magnet:
                    register_monitor(result, title, category, magnet=resolved_magnet, submission_key_value=key)
                else:
                    register_monitor(result, title, category, raw=raw, submission_key_value=key)
                final = finish_submission(key, "rdt_monitoring", result, title, requested_for_key, category)
                arr_trace.finish("download", trace_id, "rdt_monitoring", final)
                return final
            final = finish_submission(key, "transport_done", result, title, requested_for_key, category)
            arr_trace.finish("download", trace_id, "transport_done", final)
            return final
        except Exception as main_error:
            arr_trace.event("download", trace_id, "transport", "warning", "RDT fallo, evaluando fallback", {"error": str(main_error)[:500]})
            if not fallback_enabled:
                submissions.update(key, state="transport_error", last_error=str(main_error)[:500])
                arr_trace.finish("download", trace_id, "transport_error", error=str(main_error)[:500])
                raise
            submissions.update(key, state="fallback_to_qbit", last_error=str(main_error)[:500])
            emit_download_progress(
                progress,
                "qbit_sending",
                "Enviando a qB",
                "qbit",
                engine="qBittorrent",
                fallback_from=str(main_error)[:180],
            )
            try:
                arr_trace.event("download", trace_id, "fallback", "started", "Enviando torrent a qBittorrent", {"engine": "qBittorrent"})
                if resolved_magnet:
                    qbit = qbit_add_magnet(resolved_magnet, category, cleanup)
                else:
                    qbit = qbit_add_torrent(raw, title, category, cleanup)
            except Exception as fallback_error:
                submissions.update(key, state="transport_error", last_error=f"RDT: {str(main_error)[:220]} | qB: {str(fallback_error)[:220]}")
                arr_trace.finish(
                    "download",
                    trace_id,
                    "transport_error",
                    error=f"RDT: {str(main_error)[:220]} | qB: {str(fallback_error)[:220]}",
                )
                raise
            qbit["fallback_from"] = str(main_error)[:180]
            final = finish_submission(key, "submitted_qbit", qbit, title, requested_for_key, category)
            arr_trace.finish("download", trace_id, "submitted_qbit", final)
            return final


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "buscador-puente-arr"})


@app.get("/api/indexers")
def api_indexers():
    try:
        return jsonify({"ok": True, "indexers": configured_indexers()})
    except Exception as exc:
        logger.exception("indexers failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/search")
def api_search():
    query = (request.args.get("q") or "").strip()
    raw_indexers = (request.args.get("indexers") or request.args.get("indexer") or "").strip()
    indexers = selected_indexers(raw_indexers)
    category = (request.args.get("category") or "auto").strip().lower()
    metadata = {
        "section": request.args.get("section") or "",
        "title": request.args.get("title") or "",
        "quality": request.args.get("quality") or "",
        "year": request.args.get("year") or "",
    }
    return search_response(query, indexers, category, metadata)


@app.post("/api/search")
def api_search_post():
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query") or payload.get("q") or "").strip()
    category = str(payload.get("category") or "auto").strip().lower()
    indexers = selected_indexers_from_payload(payload)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    metadata = dict(metadata)
    for key in ("title", "title_clean", "year", "quality", "section", "content_type"):
        if key in payload and key not in metadata:
            metadata[key] = payload.get(key)
    return search_response(query, indexers, category, metadata)


@app.post("/api/jobs/search")
def api_search_job_start():
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query") or payload.get("q") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "busqueda vacia"}), 400
    category = str(payload.get("category") or "auto").strip().lower()
    indexers = selected_indexers_from_payload(payload)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    metadata = dict(metadata)
    request_data = {
        "query": query,
        "category": category,
        "indexers": indexers,
        "metadata": metadata,
    }
    fingerprint = job_fingerprint({"kind": "search", **request_data})
    trace_id = f"search-{fingerprint[:16]}"
    request_data["trace_id"] = trace_id
    job, reused = ui_jobs.create_or_get(
        "search",
        fingerprint,
        request_data,
        lambda: run_search_job(query, indexers, category, metadata, trace_id),
        requested_id=str(payload.get("job_id") or ""),
        reuse_states={"queued", "running", "done"},
        reuse_age_sec=20,
    )
    return jsonify({"ok": True, "reused": reused, "job": public_job(job)})


@app.get("/api/jobs/search/<job_id>")
def api_search_job_get(job_id: str):
    job = ui_jobs.get(job_id, "search")
    if not job:
        return jsonify({"ok": False, "error": "busqueda no encontrada"}), 404
    return jsonify({"ok": True, "job": public_job(job)})


@app.get("/api/settings")
def api_settings_get():
    return jsonify({"ok": True, "settings": load_settings(), "defaults": copy_defaults()})


@app.post("/api/settings")
def api_settings_post():
    payload = request.get_json(silent=True) or {}
    try:
        settings = save_settings(payload.get("settings") if "settings" in payload else payload)
        return jsonify({"ok": True, "settings": settings})
    except Exception as exc:
        logger.exception("settings save failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/settings/reset")
def api_settings_reset():
    settings = save_settings(copy_defaults())
    return jsonify({"ok": True, "settings": settings})


@app.post("/api/classify")
def api_classify():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title") or "").strip()
    settings = sanitize_settings(payload.get("settings")) if isinstance(payload.get("settings"), dict) else load_settings()
    return jsonify({"ok": True, "category": classify_auto(title, settings), "title": title})


@app.get("/api/engine-status")
def api_engine_status():
    state = load_monitor_state()
    return jsonify({
        "ok": True,
        "monitoring": len(state),
        "items": list(state.values())[:20],
        "submissions": submissions.stats(),
        "recent_submissions": submissions.recent(10),
    })


@app.get("/api/history/searches")
def api_search_history():
    return jsonify({"ok": True, "history": search_history.overview()})


@app.get("/api/history/searches/<int:search_id>/results")
def api_search_history_results(search_id: int):
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    payload = search_history.results_page(search_id, page)
    if payload is None:
        return jsonify({"ok": False, "error": "busqueda no encontrada"}), 404
    for item in payload["results"]:
        internal_url = str(item.pop("download_url", "") or "").strip()
        cached_magnet = str(item.pop("copy_magnet", "") or "").strip()
        if internal_url.startswith("magnet:"):
            item["copy_value"] = internal_url
            item["copy_kind"] = "magnet"
            item["convert_url"] = ""
        elif cached_magnet.startswith("magnet:"):
            item["copy_value"] = cached_magnet
            item["copy_kind"] = "magnet"
            item["convert_url"] = ""
        elif internal_url:
            item["copy_value"] = (
                f"{request.url_root.rstrip('/')}/api/history/results/{int(item['result_id'])}/torrent"
            )
            item["copy_kind"] = "torrent"
            item["convert_url"] = (
                f"{request.url_root.rstrip('/')}/api/history/results/{int(item['result_id'])}/magnet"
            )
        else:
            item["copy_value"] = ""
            item["copy_kind"] = "empty"
            item["convert_url"] = ""
    return jsonify({"ok": True, **payload})


@app.get("/api/history/results/<int:result_id>/torrent")
def api_search_history_torrent(result_id: int):
    item = search_history.result(result_id)
    if item is None:
        return jsonify({"ok": False, "error": "resultado no encontrado"}), 404
    internal_url = str(item.get("download_url") or "").strip()
    if not internal_url:
        return jsonify({"ok": False, "error": "resultado sin enlace"}), 404
    if internal_url.startswith("magnet:"):
        return redirect(internal_url, code=302)
    try:
        raw, resolved_magnet = download_torrent_payload(internal_url)
    except Exception as exc:
        logger.warning("history torrent proxy failed result_id=%s error=%s", result_id, str(exc)[:180])
        return jsonify({"ok": False, "error": "no se ha podido recuperar el torrent"}), 502
    if resolved_magnet:
        return redirect(resolved_magnet, code=302)
    title = clean_text(str(item.get("title") or "descarga"))
    ascii_name = unicodedata.normalize("NFKD", title).encode("ascii", errors="ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("._")[:120] or "descarga"
    response = Response(raw, content_type="application/x-bittorrent")
    response.headers["Content-Disposition"] = f'attachment; filename="{ascii_name}.torrent"'
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.post("/api/history/results/<int:result_id>/magnet")
def api_search_history_magnet(result_id: int):
    item = search_history.result(result_id)
    if item is None:
        return jsonify({"ok": False, "error": "resultado no encontrado"}), 404
    internal_url = str(item.get("download_url") or "").strip()
    cached_magnet = str(item.get("copy_magnet") or "").strip()
    if internal_url.startswith("magnet:"):
        return jsonify({"ok": True, "magnet": internal_url, "cached": True})
    if cached_magnet.startswith("magnet:"):
        return jsonify({"ok": True, "magnet": cached_magnet, "cached": True})
    if not internal_url:
        return jsonify({"ok": False, "error": "resultado sin enlace"}), 404
    try:
        raw, resolved_magnet = download_torrent_payload(internal_url, attempts=1)
        magnet = resolved_magnet or torrent_to_magnet(raw)
    except ValueError as exc:
        return jsonify({"ok": False, "convertible": False, "error": str(exc)}), 409
    except Exception as exc:
        logger.warning("history magnet conversion failed result_id=%s error=%s", result_id, str(exc)[:180])
        return jsonify({"ok": False, "error": "no se ha podido convertir el torrent"}), 502
    if not search_history.cache_magnet(result_id, magnet):
        logger.warning("history magnet cache failed result_id=%s", result_id)
    return jsonify({"ok": True, "magnet": magnet, "cached": False})


@app.post("/api/test/rdt")
def api_test_rdt():
    try:
        real_debrid_request("GET", "/user", timeout=20)
        session = rdt_login()
        rdt_json(session, "/Api/Torrents")
        return jsonify({"ok": True})
    except Exception as exc:
        logger.warning("test rdt failed error=%s", str(exc)[:180])
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/test/qbit")
def api_test_qbit():
    try:
        session = qbit_login()
        response = session.get(f"{QBIT_BASE}/api/v2/app/version", timeout=15)
        if response.status_code >= 400:
            raise RuntimeError(api_error("qBittorrent", response))
        return jsonify({"ok": True})
    except Exception as exc:
        logger.warning("test qbit failed error=%s", str(exc)[:180])
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/send")
def api_send():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title") or "jackett").strip()
    download_url = str(payload.get("download_url") or "").strip()
    category = str(payload.get("category") or "").strip().lower()
    if not download_url:
        return jsonify({"ok": False, "error": "falta download_url"}), 400
    try:
        result = deliver(
            title,
            download_url,
            category,
            bool(payload.get("test_cleanup")),
            str(payload.get("result_id") or payload.get("id") or ""),
            str(payload.get("trace_id") or ""),
        )
        logger.info("sent title=%s category=%s engine=%s fallback=%s", title, result.get("category", category or "auto"), result.get("engine"), result.get("fallback_from", ""))
        return jsonify(result)
    except Exception as exc:
        logger.exception("send failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/download")
def api_download():
    payload = request.get_json(silent=True) or {}
    category = str(payload.get("category") or "auto").strip().lower()
    try:
        item = result_from_download_payload(payload)
        result = deliver(
            item["title"],
            item["download_url"],
            category,
            bool(payload.get("test_cleanup")),
            str(item.get("id") or ""),
            str(payload.get("trace_id") or ""),
        )
        result.update({
            "source_result_id": item.get("id"),
            "source_tracker": item.get("tracker"),
            "source_tracker_id": item.get("tracker_id"),
        })
        logger.info("download title=%s category=%s engine=%s tracker=%s fallback=%s", item["title"], result.get("category", category), result.get("engine"), item.get("tracker_id") or item.get("tracker"), result.get("fallback_from", ""))
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("download failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/jobs/download")
def api_download_job_start():
    payload = request.get_json(silent=True) or {}
    category = str(payload.get("category") or "auto").strip().lower()
    try:
        item = result_from_download_payload(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    cleanup = bool(payload.get("test_cleanup"))
    request_data = {
        "source_result_id": item.get("id"),
        "title": item.get("title"),
        "tracker": item.get("tracker"),
        "category": category,
        "test_cleanup": cleanup,
    }
    fingerprint = job_fingerprint({
        "kind": "download",
        "source_result_id": item.get("id"),
        "download_url": item.get("download_url"),
        "category": category,
        "test_cleanup": cleanup,
    })
    trace_id = f"download-{fingerprint[:16]}"
    request_data["trace_id"] = trace_id

    def run_download_job(progress: Callable[..., None]) -> dict:
        result = deliver(item["title"], item["download_url"], category, cleanup, str(item.get("id") or ""), trace_id, progress)
        result.update({
            "source_result_id": item.get("id"),
            "source_tracker": item.get("tracker"),
            "source_tracker_id": item.get("tracker_id"),
        })
        logger.info(
            "download job title=%s category=%s engine=%s tracker=%s fallback=%s",
            item["title"],
            result.get("category", category),
            result.get("engine"),
            item.get("tracker_id") or item.get("tracker"),
            result.get("fallback_from", ""),
        )
        return result

    job, reused = ui_jobs.create_or_get(
        "download",
        fingerprint,
        request_data,
        run_download_job,
        requested_id=str(payload.get("job_id") or ""),
        reuse_states={"queued", "running", "done"},
        reuse_age_sec=6 * 60 * 60,
    )
    return jsonify({"ok": True, "reused": reused, "job": public_job(job)})


@app.get("/api/jobs/download/<job_id>")
def api_download_job_get(job_id: str):
    job = ui_jobs.get(job_id, "download")
    if not job:
        return jsonify({"ok": False, "error": "envio no encontrado"}), 404
    return jsonify({"ok": True, "job": public_job(job)})


@app.post("/api/jobs/download/<job_id>/dismiss")
def api_download_job_dismiss(job_id: str):
    job = ui_jobs.get(job_id, "download")
    stale_after = download_job_stale_after_sec(job) if job else 0
    result = ui_jobs.dismiss(job_id, "download", {"done", "error", "interrupted"}, stale_after)
    reason = str(result.get("reason") or "")
    if result.get("removed") or reason == "missing":
        return jsonify({"ok": True, **result})
    if reason == "active":
        return jsonify({"ok": False, **result, "error": "envio activo"}), 409
    return jsonify({"ok": False, **result, "error": "envio no descartable"}), 400


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=PORT)
