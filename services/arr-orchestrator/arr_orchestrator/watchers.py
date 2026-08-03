import queue
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, Optional, Set, Tuple

from watchdog.events import FileSystemEvent, FileSystemEventHandler


@dataclass(frozen=True)
class WatcherEvent:
    event_type: str
    path: Path
    key: Tuple[str, str]


class WatcherEventInbox:
    """Bandeja acotada que agrupa el ruido sin retrasar el primer evento."""

    def __init__(self, capacity: int = 2048):
        if capacity <= 0:
            raise ValueError("La capacidad del watcher debe ser positiva")
        self.capacity = int(capacity)
        self._events: Deque[WatcherEvent] = deque()
        self._pending: Set[Tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._received = 0
        self._coalesced = 0
        self._overflowed = 0
        self._high_watermark = 0
        self._reconcile_version = 0
        self._reconcile_pending = False

    @staticmethod
    def _key(event_type: str, path: Path) -> Tuple[str, str]:
        return str(event_type), str(path)

    def offer(self, event_type: str, path: Path) -> bool:
        normalized_path = Path(path)
        key = self._key(event_type, normalized_path)
        with self._lock:
            self._received += 1
            if key in self._pending:
                self._coalesced += 1
                return False
            if len(self._events) >= self.capacity:
                self._overflowed += 1
                self._reconcile_version += 1
                self._reconcile_pending = True
                return False
            event = WatcherEvent(str(event_type), normalized_path, key)
            self._events.append(event)
            self._pending.add(key)
            self._high_watermark = max(self._high_watermark, len(self._events))
            return True

    def get_nowait(self) -> WatcherEvent:
        with self._lock:
            if not self._events:
                raise queue.Empty
            return self._events.popleft()

    def acknowledge(self, event: WatcherEvent) -> None:
        with self._lock:
            self._pending.discard(event.key)

    def qsize(self) -> int:
        with self._lock:
            return len(self._events)

    def reconcile_ticket(self) -> int:
        with self._lock:
            return self._reconcile_version if self._reconcile_pending else 0

    def acknowledge_reconcile(self, ticket: int) -> None:
        with self._lock:
            if ticket and ticket == self._reconcile_version:
                self._reconcile_pending = False

    def stats(self) -> Dict[str, object]:
        with self._lock:
            return {
                "capacity": self.capacity,
                "received": self._received,
                "coalesced": self._coalesced,
                "overflowed": self._overflowed,
                "pending": len(self._events),
                "high_watermark": self._high_watermark,
                "reconcile_requested": self._reconcile_pending,
            }


class EventHandler(FileSystemEventHandler):
    def __init__(
        self,
        events: WatcherEventInbox,
        event_type: str,
        collapse_root: Optional[Path] = None,
    ):
        self.events = events
        self.event_type = event_type
        self.collapse_root = Path(collapse_root) if collapse_root is not None else None

    def _event_path(self, value: str) -> Optional[Path]:
        path = Path(value)
        if self.collapse_root is None:
            return path
        try:
            relative = path.relative_to(self.collapse_root)
        except ValueError:
            return None
        if not relative.parts:
            return None
        return self.collapse_root / relative.parts[0]

    def _offer(self, value: str) -> None:
        path = self._event_path(value)
        if path is not None:
            self.events.offer(self.event_type, path)

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return
        self._offer(event.src_path)
        destination = getattr(event, "dest_path", None)
        if destination:
            self._offer(destination)
