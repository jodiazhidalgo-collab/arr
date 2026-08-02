from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .model import MonitorDelta


@dataclass(frozen=True)
class TorrentListing:
    context: Any
    rows: tuple[dict, ...]


class MonitorStorePort(Protocol):
    def snapshot(self) -> dict: ...

    def apply_deltas(self, deltas: list[MonitorDelta]) -> int: ...

    def register_item(self, item: dict, raw: bytes | None = None) -> None: ...

    def cleanup_artifact(self, item: dict, reason: str) -> None: ...

    def cleanup_orphans(self, min_age_sec: int) -> int: ...


@dataclass(frozen=True)
class RdtMonitorPorts:
    load_settings: Callable[[], dict]
    list_torrents: Callable[[], TorrentListing]
    rdt_delete: Callable[[Any, str], None]
    rdt_cleanup_finished: Callable[[Any, str], None]
    qbit_add_magnet: Callable[[str, str, bool], dict]
    qbit_add_torrent: Callable[[bytes, str, str, bool], dict]
    submissions_update: Callable[..., Any]
    normalized_category: Callable[[str], str]
    magnet_hash: Callable[[str], str]
    torrent_info: Callable[[bytes], dict]
    now: Callable[[], float]
