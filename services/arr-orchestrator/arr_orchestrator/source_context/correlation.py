"""Correlacion aislada entre la ficha del buscador y la descarga materializada."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, Dict, MutableMapping, Optional

from ..db import Database
from ..filesystem import top_level_item
from ..job_states import TERMINAL_JOB_STATES
from .store import SourceContextStore


TERMINAL_STATES = set(TERMINAL_JOB_STATES)


class SourceContextCorrelator:
    def __init__(
        self,
        database: Database,
        store: SourceContextStore,
        *,
        qbt_client: Callable[[], object],
        rdt_client: Callable[[], object],
        dependencies: MutableMapping[str, str],
        qbt_materialized_source: Callable[[Path], Optional[Path]],
        translate_rdt_path: Callable[[str], Optional[Path]],
        complete_category_path: Callable[[Path], Optional[Path]],
        same_path: Callable[[Path, Path], bool],
    ) -> None:
        self.db = database
        self.store = store
        self._qbt_client = qbt_client
        self._rdt_client = rdt_client
        self.dependencies = dependencies
        self._qbt_materialized_source = qbt_materialized_source
        self._translate_rdt_path = translate_rdt_path
        self._complete_category_path = complete_category_path
        self._same_path = same_path
        self._deferred_since: Dict[str, float] = {}
        self._cycle_active = False
        self._qbt_inventory: Optional[list] = None
        self._rdt_inventory: Optional[list] = None

    def begin_cycle(self) -> None:
        self._cycle_active = True
        self._qbt_inventory = None
        self._rdt_inventory = None

    def end_cycle(self) -> None:
        self._cycle_active = False
        self._qbt_inventory = None
        self._rdt_inventory = None

    def remember_qbt(self, torrents: object) -> None:
        if self._cycle_active:
            self._qbt_inventory = list(torrents) if isinstance(torrents, list) else []

    def remember_rdt(self, torrents: object) -> None:
        if self._cycle_active:
            self._rdt_inventory = list(torrents) if isinstance(torrents, list) else []

    def qbt_inventory(self) -> list:
        if self._qbt_inventory is not None:
            return self._qbt_inventory
        try:
            torrents = list(self._qbt_client().torrents("completed"))
            self.dependencies["qbittorrent"] = "ok"
        except Exception as error:
            self.dependencies["qbittorrent"] = f"error: {error}"
            torrents = []
        if self._cycle_active:
            self._qbt_inventory = torrents
        return torrents

    def rdt_inventory(self) -> list:
        if self._rdt_inventory is not None:
            return self._rdt_inventory
        try:
            torrents = list(self._rdt_client().torrents("all"))
            self.dependencies["rdtclient"] = "ok"
        except Exception as error:
            self.dependencies["rdtclient"] = f"error: {error}"
            torrents = []
        if self._cycle_active:
            self._rdt_inventory = torrents
        return torrents

    def correlate_materialized(
        self,
        category: str,
        item: Path,
        materialized_job: Optional[Dict[str, object]] = None,
    ) -> Optional[Dict[str, object]]:
        if category not in {"movies", "tv"} or not self.store.has_correlatable(category):
            return None
        torrents = self.qbt_inventory()
        for torrent in torrents:
            infohash = str(torrent.get("hash") or "").strip().lower()
            content_path = Path(str(torrent.get("content_path") or ""))
            if len(infohash) != 40 or not content_path.exists():
                continue
            source_path = self._qbt_materialized_source(content_path)
            if not source_path or not self._same_path(source_path, item):
                continue
            job = self.store.job_by_hash(infohash, category)
            if job:
                if materialized_job and str(materialized_job.get("job_id")) != str(
                    job.get("job_id")
                ):
                    job = self.store.merge_into_materialized_job(
                        job, materialized_job, infohash
                    )
                    if not job:
                        return None
                return self.attach_qbt(
                    job,
                    infohash,
                    category,
                    source_path,
                    content_path,
                    float(torrent.get("added_on") or time.time()),
                    "Materializacion qB correlacionada por infohash con contexto de origen",
                )

        torrents = self.rdt_inventory()
        for torrent in torrents:
            infohash = str(torrent.get("hash") or "").strip().lower()
            if len(infohash) != 40:
                continue
            content_path = self._translate_rdt_path(
                str(torrent.get("content_path") or "")
            )
            if not content_path or not content_path.exists():
                continue
            root = self._complete_category_path(content_path)
            source_path = top_level_item(root, content_path) if root else content_path
            if not source_path or not self._same_path(source_path, item):
                continue
            job = self.store.job_by_hash(infohash, category)
            if job:
                if materialized_job and str(materialized_job.get("job_id")) != str(
                    job.get("job_id")
                ):
                    job = self.store.merge_into_materialized_job(
                        job, materialized_job, infohash
                    )
                    if not job:
                        return None
                return self.attach_rdt(
                    job,
                    torrent,
                    source_path,
                    "Materializacion RDT correlacionada por infohash con contexto de origen",
                )
        return None

    def context_job_by_hash(self, infohash: str) -> Optional[Dict[str, object]]:
        job = self.db.get_active_job_by_infohash(infohash)
        if job:
            return job
        terminal = self.db.get_job_by_infohash(infohash)
        return terminal if self.store.has_context(terminal) else None

    def job_for_source_path(self, source_path: Path) -> Optional[Dict[str, object]]:
        jobs = self.db.get_active_jobs_by_source_path(str(source_path))
        if not jobs:
            return None
        context_jobs = [job for job in jobs if self.store.has_context(job)]
        materialized_jobs = [job for job in jobs if not self.store.has_context(job)]
        if context_jobs and materialized_jobs:
            context_job = context_jobs[0]
            infohash = str(context_job.get("infohash") or "").strip().lower()
            if len(infohash) == 40:
                merged = self.store.merge_into_materialized_job(
                    context_job, materialized_jobs[0], infohash
                )
                if merged:
                    return merged
        return jobs[0]

    def job_for_qbt_content(
        self, infohash: str, source_path: Path, content_path: Path
    ) -> Optional[Dict[str, object]]:
        hash_job = self.context_job_by_hash(infohash)
        seen = set()
        path_job = None
        for candidate in (source_path, content_path):
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            path_job = self.job_for_source_path(candidate)
            if path_job:
                break
        if (
            hash_job
            and path_job
            and str(hash_job.get("job_id")) != str(path_job.get("job_id"))
            and self.store.has_context(hash_job)
        ):
            merged = self.store.merge_into_materialized_job(
                hash_job, path_job, infohash
            )
            if merged:
                return merged
        return hash_job or path_job

    def should_defer(self, category: str, item: Path, grace_seconds: int) -> bool:
        """Aplica una sola ventana de gracia por item; nuevos intents no la reinician."""

        key = f"{category}:{str(item).casefold()}"
        if grace_seconds <= 0 or not self.store.has_recent_pending(
            category, grace_seconds
        ):
            self._deferred_since.pop(key, None)
            return False
        now = time.monotonic()
        started = self._deferred_since.setdefault(key, now)
        return now - started < float(grace_seconds)

    def resolved(self, category: str, item: Path) -> None:
        self._deferred_since.pop(f"{category}:{str(item).casefold()}", None)

    def physical_name_updates(
        self, job: Dict[str, object], physical_name: str
    ) -> Dict[str, object]:
        return self.store.physical_name_updates(job, physical_name)

    def adopt_watch_torrent(
        self, job: Dict[str, object], physical_name: str, torrent_path: Path
    ) -> Dict[str, object]:
        """Enlaza atomically un torrent watch con su intent previo por infohash."""

        if (
            str(job.get("state") or "") != "source_submitted"
            or not self.store.has_context(job)
        ):
            return job
        connection = self.db.connect()
        committed_event = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            name_updates = self.store.physical_name_updates(job, physical_name)
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state='received', origin='watch', torrent_path=?, name=?, updated_at=?
                WHERE job_id=? AND state='source_submitted'
                """,
                (
                    str(torrent_path),
                    str(name_updates.get("name") or job.get("name") or physical_name),
                    time.time(),
                    str(job["job_id"]),
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return self.db.get_job(str(job["job_id"])) or job
            committed_event = self.db.append_event(
                connection,
                str(job["job_id"]),
                "source_context",
                "decision",
                "Torrent watch enlazado con el contexto por infohash",
                {
                    "state": "received",
                    "action": "watch_linked",
                    "infohash": str(job.get("infohash") or ""),
                    "category": str(job.get("category") or ""),
                },
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        if committed_event:
            self.db.publish_event(committed_event)
        return self.db.get_job(str(job["job_id"])) or job

    def attach_qbt(
        self,
        job: Dict[str, object],
        infohash: str,
        category: str,
        source_path: Path,
        content_path: Path,
        submitted_at: float,
        message: str,
    ) -> Dict[str, object]:
        if str(job.get("state") or "") in TERMINAL_STATES:
            return job
        job_id = str(job["job_id"])
        observed_hash = _infohash(infohash)
        current_infohash = _infohash(job.get("infohash"))
        current_qbt_hash = _infohash(job.get("qbt_hash"))
        if not observed_hash:
            self.db.add_event(
                job_id,
                "source_context",
                "warning",
                "Materializacion qB rechazada por infohash no valido",
                {
                    "action": "qbt_materialization_invalid_infohash",
                    "current_infohash": current_infohash,
                },
            )
            return job
        if observed_hash and any(
            current_hash and current_hash != observed_hash
            for current_hash in (current_infohash, current_qbt_hash)
        ):
            self.db.add_event(
                job_id,
                "source_context",
                "warning",
                "Materializacion qB rechazada por conflicto de infohash",
                {
                    "action": "qbt_materialization_hash_conflict",
                    "current_infohash": current_infohash,
                    "current_qbt_hash": current_qbt_hash,
                    "observed_infohash": observed_hash,
                },
            )
            return job
        infohash = observed_hash
        current_state = str(job.get("state") or "")
        materializing_states = {
            "received",
            "source_submitted",
            "waiting_materialization",
            "waiting_stable",
        }
        target_state = (
            "waiting_stable" if current_state in materializing_states else current_state
        )
        updates: Dict[str, object] = {}
        if str(job.get("infohash") or "").lower() != infohash:
            updates["infohash"] = infohash
        if str(job.get("qbt_hash") or "").lower() != infohash:
            updates["qbt_hash"] = infohash
        if str(job.get("category") or "") != category:
            updates["category"] = category
        if str(job.get("source_path") or "") != str(source_path):
            updates["source_path"] = str(source_path)
        if not job.get("submitted_at") and submitted_at:
            updates["submitted_at"] = submitted_at
        updates.update(self.store.physical_name_updates(job, source_path.name))

        if target_state != current_state:
            updates["state"] = target_state
        if updates:
            return self._update_with_safe_event(
                job_id,
                updates,
                "qbt",
                message,
                {
                    "state": target_state,
                    "infohash": infohash,
                    "qbt_hash": infohash,
                    "category": category,
                    "materialized": True,
                },
            )
        return job

    def attach_rdt(
        self,
        job: Dict[str, object],
        torrent: Dict[str, object],
        source_path: Optional[Path],
        message: str,
    ) -> Dict[str, object]:
        if str(job.get("state") or "") in TERMINAL_STATES:
            return job
        job_id = str(job["job_id"])
        current_state = str(job.get("state") or "")
        has_source_context = self.store.has_context(job)
        infohash = _infohash(torrent.get("hash"))
        current_hash = _infohash(job.get("infohash"))
        if current_hash and infohash and current_hash != infohash:
            self.db.add_event(
                job_id,
                "source_context",
                "warning",
                "Materializacion RDT rechazada por conflicto de infohash",
                {
                    "action": "rdt_materialization_hash_conflict",
                    "current_infohash": current_hash,
                    "observed_infohash": infohash,
                },
            )
            return job
        materialized = bool(source_path is not None and source_path.exists())
        updates: Dict[str, object] = {
            "rdt_id": str(torrent.get("id") or torrent.get("hash") or ""),
            "rdt_progress": float(torrent.get("progress") or 0),
        }
        if infohash and current_hash != infohash:
            updates["infohash"] = infohash
        target_state = current_state
        if source_path is not None:
            updates["source_path"] = str(source_path)
            if materialized:
                updates.update(self.store.physical_name_updates(job, source_path.name))
            if materialized and current_state in {
                "received",
                "source_submitted",
                "waiting_materialization",
                "waiting_stable",
            }:
                target_state = "waiting_stable"
            elif not materialized and current_state == "source_submitted":
                target_state = "waiting_materialization"
        if target_state != current_state:
            if not has_source_context:
                updates["state"] = target_state
                return self.db.update_job(job_id, **updates)
            updates["state"] = target_state
            return self._update_with_safe_event(
                job_id,
                updates,
                "rdt",
                message,
                {
                    "state": target_state,
                    "infohash": infohash or str(job.get("infohash") or ""),
                    "category": str(job.get("category") or ""),
                    "materialized": materialized,
                },
            )
        changed = {
            key: value
            for key, value in updates.items()
            if str(job.get(key) or "") != str(value or "")
        }
        if not changed:
            return job
        if not has_source_context:
            return self.db.update_job(job_id, **changed)
        source_path_changed = bool(
            source_path is not None
            and str(job.get("source_path") or "") != str(source_path)
        )
        if has_source_context and (not job.get("rdt_id") or source_path_changed):
            return self._update_with_safe_event(
                job_id,
                changed,
                "rdt",
                message,
                {
                    "state": target_state,
                    "infohash": infohash or str(job.get("infohash") or ""),
                    "category": str(job.get("category") or ""),
                    "materialized": materialized,
                },
            )
        return self.db.update_job(job_id, **changed)

    def adopt_rdt_for_materialized_job(
        self, job: Dict[str, object], category: str, item: Path
    ) -> Dict[str, object]:
        """Adopta por ruta una fila RDT retenida y fija su infohash canonico."""

        matches = []
        for torrent in self.rdt_inventory():
            infohash = _infohash(torrent.get("hash"))
            if not infohash:
                continue
            content_path = self._translate_rdt_path(
                str(torrent.get("content_path") or "")
            )
            if not content_path:
                continue
            category_root = self._complete_category_path(content_path)
            source_path = (
                top_level_item(category_root, content_path) if category_root else None
            )
            if not source_path or not self._same_path(source_path, item):
                continue
            matches.append((torrent, infohash))

        if not matches:
            return job
        observed_hashes = sorted({infohash for _torrent, infohash in matches})
        if len(observed_hashes) != 1:
            self.db.add_event(
                str(job["job_id"]),
                "source_context",
                "warning",
                "No se adopto RDT porque la ruta corresponde a varios infohash",
                {
                    "action": "rdt_materialized_path_ambiguous",
                    "observed_infohashes": observed_hashes,
                },
            )
            return job

        torrent, infohash = matches[0]

        current_hash = _infohash(job.get("infohash"))
        if current_hash and current_hash != infohash:
            self.db.add_event(
                str(job["job_id"]),
                "source_context",
                "warning",
                "No se adopto RDT por conflicto de infohash",
                {
                    "action": "rdt_materialized_job_hash_conflict",
                    "current_infohash": current_hash,
                    "observed_infohash": infohash,
                },
            )
            return job

        context_job = self.db.get_active_job_by_infohash(infohash)
        target = job
        if context_job and str(context_job.get("job_id")) != str(job.get("job_id")):
            merged = self.store.merge_into_materialized_job(
                context_job, job, infohash
            )
            if not merged:
                return job
            target = merged
        try:
            return self.attach_rdt(
                target,
                torrent,
                item,
                "Materializacion RDT adoptada por trabajo detectado en carpeta",
            )
        except sqlite3.IntegrityError:
            self.db.connect().rollback()
            context_job = self.db.get_active_job_by_infohash(infohash)
            if not context_job or str(context_job.get("job_id")) == str(
                target.get("job_id")
            ):
                raise
            merged = self.store.merge_into_materialized_job(
                context_job, target, infohash
            )
            if not merged:
                return target
            return self.attach_rdt(
                merged,
                torrent,
                item,
                "Materializacion RDT adoptada tras carrera de contexto",
            )

    def _update_with_safe_event(
        self,
        job_id: str,
        updates: Dict[str, object],
        phase: str,
        message: str,
        structured: Dict[str, object],
    ) -> Dict[str, object]:
        """Actualiza y traza sin copiar rutas absolutas al historial humano."""

        connection = self.db.connect()
        committed_event = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            fields = dict(updates)
            fields["updated_at"] = time.time()
            assignments = ", ".join(f"{key}=?" for key in fields)
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id=?",
                tuple(fields.values()) + (job_id,),
            )
            committed_event = self.db.append_event(
                connection,
                job_id,
                phase,
                "decision",
                message,
                structured,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        if committed_event:
            self.db.publish_event(committed_event)
        return self.db.get_job(job_id) or {"job_id": job_id, **updates}


def _infohash(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return (
        candidate
        if len(candidate) == 40 and all(char in "0123456789abcdef" for char in candidate)
        else ""
    )
