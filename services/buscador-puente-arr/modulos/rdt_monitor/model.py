import re
from dataclasses import dataclass, field
from typing import Any, Mapping


TERMINAL_STATUS_RE = re.compile(r"^\s*(finished|completed)\b", re.I)
WAITING_LOCAL_DOWNLOAD_RE = re.compile(r"\bwaiting for download links\b", re.I)


@dataclass(frozen=True)
class MonitorIdentity:
    rdt_id: str
    first_seen: str
    submission_key: str
    info_hash: str
    kind: str


@dataclass(frozen=True)
class MonitorDelta:
    key: str
    expected: MonitorIdentity
    changes: Mapping[str, Any] = field(default_factory=dict)
    delete_fields: tuple[str, ...] = ()
    remove: bool = False


def monitor_identity(key: str, item: dict) -> MonitorIdentity:
    return MonitorIdentity(
        rdt_id=str(item.get("rdt_id") or key),
        first_seen=str(item.get("first_seen") or ""),
        submission_key=str(item.get("submission_key") or ""),
        info_hash=str(item.get("hash") or "").strip().lower(),
        kind=str(item.get("kind") or ""),
    )


def status_progress(row: dict) -> float:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("statusText", "rdStatusRaw", "status", "last_status", "error")
    )
    match = re.search(r"(\d+(?:[\.,]\d+)?)\s*%", text)
    if match:
        try:
            return float(match.group(1).replace(",", "."))
        except ValueError:
            return -1.0

    if WAITING_LOCAL_DOWNLOAD_RE.search(str(row.get("statusText") or "")):
        return -1.0

    for key in ("progress", "downloadProgress"):
        value = row.get(key)
        if value is None:
            continue
        try:
            number = float(str(value).replace(",", "."))
            return number * 100 if 0 <= number <= 1 else number
        except ValueError:
            pass
    for key in ("rdProgress", "RdProgress", "percent", "percentage"):
        value = row.get(key)
        if value is None:
            continue
        try:
            number = float(str(value).replace(",", "."))
            if key in {"rdProgress", "RdProgress"} and number >= 100 and not TERMINAL_STATUS_RE.search(text):
                return -1.0
            return number
        except ValueError:
            pass
    return -1.0


def status_text(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("statusText", "rdStatusRaw", "status", "last_status", "error")
    ).strip()


def local_download_complete(row: dict) -> bool:
    downloads = row.get("downloads")
    if isinstance(downloads, list):
        if not downloads:
            return False
        return all(
            isinstance(download, dict)
            and download.get("completed") not in (None, "", False)
            for download in downloads
        )

    return False


def is_finished(row: dict) -> bool:
    try:
        if int(row.get("finished_seen_ts") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    if local_download_complete(row):
        return True
    terminal_text = " ".join(
        str(row.get(key) or "")
        for key in ("statusText", "rdStatusRaw", "status", "last_status")
    )
    return bool(TERMINAL_STATUS_RE.search(terminal_text))


def should_fallback(row: dict, item: dict, settings: dict, now: int) -> tuple[bool, str]:
    text = status_text(row)
    low = text.lower()
    if any(word in low for word in ("error", "failed", "fallo", "missing")):
        return True, text[:160] or "estado malo"

    progress = status_progress(row)
    downloads = int(row.get("downloadsCount") or 0)
    if downloads > 0 or is_finished(row):
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


def row_hash(row: dict) -> str:
    for key in ("hash", "infoHash", "infohash", "torrentHash"):
        value = str(row.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def index_rows(rows: tuple[dict, ...]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_id: dict[str, dict] = {}
    by_hash: dict[str, dict] = {}
    for row in rows:
        torrent_id = str(row.get("torrentId") or "").strip().casefold()
        info_hash = row_hash(row)
        if torrent_id:
            by_id[torrent_id] = row
        if info_hash:
            by_hash[info_hash] = row
    return by_id, by_hash


def find_row(key: str, item: dict, by_id: dict[str, dict], by_hash: dict[str, dict]) -> dict | None:
    torrent_id = str(item.get("rdt_id") or key).strip().casefold()
    if torrent_id and torrent_id in by_id:
        return by_id[torrent_id]
    info_hash = str(item.get("hash") or "").strip().lower()
    return by_hash.get(info_hash) if info_hash else None
