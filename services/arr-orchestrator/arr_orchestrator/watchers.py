import queue
from pathlib import Path
from typing import Tuple

from watchdog.events import FileSystemEvent, FileSystemEventHandler


class EventHandler(FileSystemEventHandler):
    def __init__(self, events: "queue.Queue[Tuple[str, Path]]", event_type: str):
        self.events = events
        self.event_type = event_type

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return
        self.events.put((self.event_type, Path(event.src_path)))
        destination = getattr(event, "dest_path", None)
        if destination:
            self.events.put((self.event_type, Path(destination)))
