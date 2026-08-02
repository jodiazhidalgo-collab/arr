import copy
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    MonitorDelta,
    MonitorIdentity,
    find_row,
    index_rows,
    is_finished,
    monitor_identity,
    should_fallback,
    status_progress,
    status_text,
)
from .ports import MonitorStorePort, RdtMonitorPorts, TorrentListing


@dataclass(frozen=True)
class _ListingCache:
    listing: TorrentListing | None
    failed: bool
    fetched_at: float
    expires_at: float


@dataclass(frozen=True)
class _ProgressObservation:
    progress: float | None
    status: str
    observed_at: int | None


class RdtMonitor:
    def __init__(
        self,
        store: MonitorStorePort,
        ports: RdtMonitorPorts,
        logger: Any,
        finished_cleanup_delay_sec: int,
        orphan_cleanup_sec: int,
        visual_cache_ttl_sec: int = 5,
    ) -> None:
        self._store = store
        self._ports = ports
        self._logger = logger
        self._finished_cleanup_delay_sec = int(finished_cleanup_delay_sec)
        self._orphan_cleanup_sec = int(orphan_cleanup_sec)
        self._visual_cache_ttl_sec = max(1, int(visual_cache_ttl_sec))
        self._listing_lock = threading.RLock()
        self._observation_lock = threading.RLock()
        self._listing_cache: _ListingCache | None = None
        self._observations: dict[MonitorIdentity, _ProgressObservation] = {}

    def register(
        self,
        result: dict,
        title: str,
        category: str,
        raw: bytes | None = None,
        magnet: str = "",
        submission_key_value: str = "",
        trace_id: str = "",
    ) -> None:
        rdt_id = str(result.get("rdt_id") or "")
        if not rdt_id:
            return
        now = int(self._ports.now())
        item = {
            "rdt_id": rdt_id,
            "title": title,
            "category": self._ports.normalized_category(category),
            "first_seen": now,
            "last_progress_ts": now,
            "last_progress": -1.0,
            "last_status": str(result.get("status") or ""),
            "finished_seen_ts": 0,
            "submission_key": submission_key_value,
            "trace_id": trace_id,
        }
        if magnet:
            item["kind"] = "magnet"
            item["magnet"] = magnet
            item["hash"] = self._ports.magnet_hash(magnet)
        elif raw:
            info = self._ports.torrent_info(raw)
            item["kind"] = "torrent"
            item["hash"] = info["hash"]
        else:
            return
        self._store.register_item(item, raw)

    def cleanup_orphans(self) -> int:
        return self._store.cleanup_orphans(self._orphan_cleanup_sec)

    def _load_listing(self, force: bool) -> _ListingCache:
        with self._listing_lock:
            now = float(self._ports.now())
            if not force and self._listing_cache and now < self._listing_cache.expires_at:
                return self._listing_cache
            try:
                listing = self._ports.list_torrents()
                if not isinstance(listing, TorrentListing):
                    raise RuntimeError("listado RDT no valido")
                rows = tuple(row for row in listing.rows if isinstance(row, dict))
                entry = _ListingCache(
                    listing=TorrentListing(context=listing.context, rows=rows),
                    failed=False,
                    fetched_at=now,
                    expires_at=now + self._visual_cache_ttl_sec,
                )
            except Exception as exc:
                entry = _ListingCache(
                    listing=None,
                    failed=True,
                    fetched_at=now,
                    expires_at=now + self._visual_cache_ttl_sec,
                )
                self._logger.warning("monitor rdt list failed error=%s", str(exc)[:180])
            self._listing_cache = entry
            return entry

    @staticmethod
    def _delta(key: str, original: dict, updated: dict, remove: bool = False) -> MonitorDelta | None:
        expected = monitor_identity(key, original)
        if remove:
            return MonitorDelta(key=key, expected=expected, remove=True)
        changes = {
            field: copy.deepcopy(value)
            for field, value in updated.items()
            if field not in original or original.get(field) != value
        }
        delete_fields = tuple(field for field in original if field not in updated)
        if not changes and not delete_fields:
            return None
        return MonitorDelta(
            key=key,
            expected=expected,
            changes=changes,
            delete_fields=delete_fields,
        )

    def _perform_fallback(self, context: Any, item: dict, reason: str, settings: dict) -> None:
        category = self._ports.normalized_category(str(item.get("category") or ""))
        title = str(item.get("title") or "jackett")
        key = str(item.get("submission_key") or "")
        if key:
            self._ports.submissions_update(key, state="fallback_to_qbit", last_error=reason)
        if item.get("kind") == "magnet":
            qbit = self._ports.qbit_add_magnet(str(item.get("magnet") or ""), category, False)
        else:
            torrent_path = Path(str(item.get("torrent_path") or ""))
            if not torrent_path.exists():
                raise RuntimeError("no encuentro torrent guardado para fallback")
            qbit = self._ports.qbit_add_torrent(torrent_path.read_bytes(), title, category, False)
        if settings.get("rdt", {}).get("cleanup_on_fallback", True):
            self._ports.rdt_delete(context, str(item.get("rdt_id") or ""))
        if key:
            qbit_result = {
                **qbit,
                "ok": True,
                "title": title,
                "category": category,
                "requested_category": str(item.get("requested_category") or category),
                "fallback_from": reason[:180],
                "submission_key": key,
                "trace_id": str(item.get("trace_id") or ""),
            }
            self._ports.submissions_update(
                key,
                state="submitted_qbit",
                engine=qbit_result.get("engine", "qBittorrent"),
                qbit_hash=qbit_result.get("hash", ""),
                result=qbit_result,
                last_error=reason[:180],
            )
        self._store.cleanup_artifact(item, "fallback")
        self._logger.info("monitor fallback title=%s category=%s reason=%s", title, category, reason)

    def _cleanup_finished(self, context: Any, key: str, item: dict, row: dict, now: int) -> bool:
        text = status_text(row)
        progress = status_progress(row)
        try:
            old_progress = float(item.get("last_progress", -1))
        except (TypeError, ValueError):
            old_progress = -1.0

        item["last_progress"] = max(old_progress, progress, 100.0)
        item["last_progress_ts"] = now
        item["last_status"] = text or str(item.get("last_status") or "")
        item["completed"] = True
        item.pop("magnet", None)

        finished_seen_ts = int(item.get("finished_seen_ts") or 0)
        if not finished_seen_ts:
            item["finished_seen_ts"] = now
            self._logger.info(
                "monitor finished seen id=%s title=%s wait=%ss",
                key,
                str(item.get("title") or "")[:120],
                self._finished_cleanup_delay_sec,
            )
            return False

        if now - finished_seen_ts < self._finished_cleanup_delay_sec:
            return False

        self._ports.rdt_cleanup_finished(context, str(item.get("rdt_id") or key))
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
                "trace_id": str(item.get("trace_id") or ""),
            }
            self._ports.submissions_update(
                submission_key_value,
                state="transport_done",
                engine="RDT-Client",
                rdt_id=str(item.get("rdt_id") or key),
                result=result,
            )
        self._store.cleanup_artifact(item, "finished")
        self._logger.info("monitor cleaned finished rdt item id=%s preserve_local=true", key)
        return True

    def _finalize_missing_finished(self, key: str, item: dict) -> None:
        submission_key_value = str(item.get("submission_key") or "")
        if submission_key_value:
            self._ports.submissions_update(
                submission_key_value,
                state="transport_done",
                engine="RDT-Client",
                rdt_id=str(item.get("rdt_id") or key),
            )
        self._store.cleanup_artifact(item, "finished-missing")
        self._logger.info("monitor removed finished missing rdt item id=%s", key)

    def poll_once(self) -> None:
        state = self._store.snapshot()
        if not state:
            return
        settings = self._ports.load_settings()
        fallback_enabled = bool(
            settings.get("rdt", {}).get("fallback_enabled", True)
            and settings.get("qbit", {}).get("fallback_enabled", True)
        )
        listing_entry = self._load_listing(force=True)
        if listing_entry.failed or not listing_entry.listing:
            return

        listing = listing_entry.listing
        by_id, by_hash = index_rows(listing.rows)
        now = int(self._ports.now())
        deltas: list[MonitorDelta] = []
        for key, original in state.items():
            if not isinstance(original, dict):
                continue
            item = copy.deepcopy(original)
            remove = False
            try:
                row = find_row(key, item, by_id, by_hash)
                if row is None:
                    if is_finished(item):
                        self._finalize_missing_finished(key, item)
                        remove = True
                    elif fallback_enabled:
                        self._perform_fallback(
                            listing.context,
                            item,
                            "rdt item missing before progress",
                            settings,
                        )
                        remove = True
                        self._logger.info("monitor fallback missing rdt item id=%s", key)
                elif is_finished(row) or is_finished(item):
                    remove = self._cleanup_finished(listing.context, key, item, row, now)
                elif fallback_enabled:
                    fallback, reason = should_fallback(row, item, settings, now)
                    if fallback:
                        self._perform_fallback(listing.context, item, reason, settings)
                        remove = True
            except Exception as exc:
                error = str(exc)
                item["last_error"] = error[:180]
                remove = False
                self._logger.warning("monitor item failed id=%s error=%s", key, error[:180])

            delta = self._delta(key, original, item, remove)
            if delta:
                deltas.append(delta)
        self._store.apply_deltas(deltas)

    @staticmethod
    def _terminal_observed_at(item: dict, fallback: float) -> int:
        for field in ("finished_seen_ts", "last_progress_ts", "first_seen"):
            try:
                value = int(item.get(field) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        return int(fallback)

    def _enrich_item(
        self,
        key: str,
        item: dict,
        listing_entry: _ListingCache,
        by_id: dict[str, dict],
        by_hash: dict[str, dict],
    ) -> dict:
        enriched = copy.deepcopy(item)
        identity = monitor_identity(key, item)
        previous = self._observations.get(identity)
        row = find_row(key, item, by_id, by_hash) if listing_entry.listing else None

        if is_finished(item):
            current_status = status_text(row or {}) or str(item.get("last_status") or "")
            observation = _ProgressObservation(
                progress=100.0,
                status=current_status,
                observed_at=self._terminal_observed_at(item, listing_entry.fetched_at),
            )
            self._observations[identity] = observation
            stale = False
        elif listing_entry.failed:
            observation = previous or _ProgressObservation(
                progress=None,
                status=str(item.get("last_status") or ""),
                observed_at=None,
            )
            stale = previous is not None
        elif row is None:
            observation = previous or _ProgressObservation(
                progress=None,
                status=str(item.get("last_status") or ""),
                observed_at=None,
            )
            stale = previous is not None
        else:
            current_status = status_text(row) or str(item.get("last_status") or "")
            if is_finished(row):
                observation = _ProgressObservation(
                    progress=100.0,
                    status=current_status,
                    observed_at=int(listing_entry.fetched_at),
                )
                stale = False
            else:
                measured = status_progress(row)
                if measured >= 0:
                    measured = min(99.0, float(measured))
                    observation = _ProgressObservation(
                        progress=max(0.0, measured),
                        status=current_status,
                        observed_at=int(listing_entry.fetched_at),
                    )
                    stale = False
                elif previous is not None and previous.progress is not None:
                    observation = _ProgressObservation(
                        progress=previous.progress,
                        status=current_status or previous.status,
                        observed_at=previous.observed_at,
                    )
                    stale = True
                else:
                    observation = _ProgressObservation(
                        progress=None,
                        status=current_status,
                        observed_at=int(listing_entry.fetched_at),
                    )
                    stale = False
            self._observations[identity] = observation

        enriched["progress"] = observation.progress
        enriched["progress_status"] = observation.status
        enriched["progress_observed_at"] = observation.observed_at
        enriched["progress_stale"] = stale
        return enriched

    def snapshot(self) -> dict:
        state = self._store.snapshot()
        if not state:
            return {}
        listing_entry = self._load_listing(force=False)
        if listing_entry.listing:
            by_id, by_hash = index_rows(listing_entry.listing.rows)
        else:
            by_id, by_hash = {}, {}

        active_identities = {
            monitor_identity(key, item)
            for key, item in state.items()
            if isinstance(item, dict)
        }
        with self._observation_lock:
            self._observations = {
                identity: observation
                for identity, observation in self._observations.items()
                if identity in active_identities
            }
            return {
                key: self._enrich_item(key, item, listing_entry, by_id, by_hash)
                for key, item in state.items()
                if isinstance(item, dict)
            }
