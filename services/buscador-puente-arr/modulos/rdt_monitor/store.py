import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

from .model import MonitorDelta, monitor_identity


class MonitorStore:
    def __init__(self, state_path: Path, monitor_dir: Path, logger: Any) -> None:
        self.state_path = Path(state_path)
        self.monitor_dir = Path(monitor_dir)
        self.logger = logger
        self._lock = threading.RLock()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.monitor_dir.mkdir(parents=True, exist_ok=True)

    def _load_unlocked(self) -> dict:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            self.logger.warning("monitor state load failed error=%s", str(exc)[:160])
        return {}

    def _replace_unlocked(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def snapshot(self) -> dict:
        with self._lock:
            return self._load_unlocked()

    def apply_deltas(self, deltas: list[MonitorDelta]) -> int:
        if not deltas:
            return 0
        with self._lock:
            state = self._load_unlocked()
            applied = 0
            changed = False
            for delta in deltas:
                current = state.get(delta.key)
                if not isinstance(current, dict):
                    continue
                if monitor_identity(delta.key, current) != delta.expected:
                    continue
                if delta.remove:
                    state.pop(delta.key, None)
                    applied += 1
                    changed = True
                    continue

                updated = dict(current)
                for field in delta.delete_fields:
                    updated.pop(field, None)
                for field, value in delta.changes.items():
                    updated[field] = copy.deepcopy(value)
                if updated == current:
                    continue
                state[delta.key] = updated
                applied += 1
                changed = True

            if changed:
                self._replace_unlocked(state)
            return applied

    def register_item(self, item: dict, raw: bytes | None = None) -> None:
        with self._lock:
            if raw:
                torrent_path = self.monitor_dir / f"{item['hash']}.torrent"
                if not torrent_path.exists():
                    torrent_path.write_bytes(raw)
                item["torrent_path"] = str(torrent_path)
            state = self._load_unlocked()
            state[str(item["rdt_id"])] = item
            self._replace_unlocked(state)

    def cleanup_artifact(self, item: dict, reason: str) -> None:
        torrent_path = str(item.get("torrent_path") or "").strip()
        if not torrent_path:
            return
        try:
            path = Path(torrent_path)
            if not path.is_absolute():
                path = self.monitor_dir / path
            resolved = path.resolve()
            monitor_root = self.monitor_dir.resolve()
            if monitor_root not in resolved.parents and resolved != monitor_root:
                self.logger.warning("monitor artifact cleanup skipped outside dir path=%s", torrent_path)
                return
            resolved.unlink(missing_ok=True)
            self.logger.info("monitor artifact cleaned path=%s reason=%s", resolved.name, reason)
        except Exception as exc:
            self.logger.warning("monitor artifact cleanup failed path=%s error=%s", torrent_path, str(exc)[:160])

    def cleanup_orphans(self, min_age_sec: int) -> int:
        now = time.time()
        state = self.snapshot()
        referenced: set[Path] = set()
        for item in state.values():
            torrent_path = str(item.get("torrent_path") or "").strip()
            if not torrent_path:
                continue
            try:
                path = Path(torrent_path)
                if not path.is_absolute():
                    path = self.monitor_dir / path
                referenced.add(path.resolve())
            except Exception:
                continue

        cleaned = 0
        monitor_root = self.monitor_dir.resolve()
        for path in self.monitor_dir.glob("*.torrent"):
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
                self.logger.warning("monitor orphan cleanup failed file=%s error=%s", path.name, str(exc)[:160])
        if cleaned:
            self.logger.info("monitor orphan artifacts cleaned count=%s", cleaned)
        return cleaned
