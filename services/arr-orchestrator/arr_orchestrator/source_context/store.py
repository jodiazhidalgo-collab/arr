"""Persistencia atomica del contexto de origen sobre jobs ya canonicos."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from ..db import Database
from .contract import SourceContextContractError, SourceContextEvent
from .policy import (
    CONTEXT_TTL_SECONDS,
    DELIVERY_STATE_ORDER,
    MAX_SOURCE_TITLES,
    USABLE_DELIVERY_STATES,
)


NEUTRAL_JOB_NAME = "Descarga pendiente"
TERMINAL_STATES = {"done", "manual_review", "duplicate", "error_terminal", "discarded"}
PENDING_STATES = {"source_submitted", "waiting_materialization"}
_CONTEXT_KEYS = {
    "event_id",
    "source",
    "infohash",
    "destination",
    "source_title",
    "route",
    "delivery_state",
    "created_at",
    "received_at",
}


@dataclass(frozen=True)
class StoreResult:
    action: str
    job_id: Optional[str]
    created: bool = False
    context_count: int = 0
    conflict_destination: str = ""


class SourceContextStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    def apply(
        self,
        event: SourceContextEvent,
        identity_context: Optional[Dict[str, object]],
    ) -> StoreResult:
        now = time.time()
        connection = self.db.connect()
        recorded: Optional[Tuple[str, str, str, str, Dict[str, object]]] = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            event_owner = _event_owner(connection, event.source, event.event_id)
            owner_event = (
                event_owner.get("_event") if isinstance(event_owner, dict) else None
            )
            immutable_conflict = bool(
                event_owner
                and (
                    str(event_owner.get("infohash") or "").strip().lower()
                    != event.infohash
                    or not isinstance(owner_event, dict)
                    or str(owner_event.get("destination") or "") != event.destination
                    or not _event_title_matches(owner_event, event.source_title)
                )
            )
            if immutable_conflict:
                job_id = str(event_owner["job_id"])
                committed_event = self.db.append_event(
                    connection,
                    job_id,
                    "source_context",
                    "warning",
                    "Evento de origen rechazado por contenido inmutable distinto",
                    _event_details(event, "event_conflict"),
                )
                connection.commit()
                self.db.publish_event(committed_event)
                return StoreResult(
                    "event_conflict",
                    job_id,
                    context_count=len(
                        _contexts(event_owner.get("source_meta_json"), now)
                    ),
                )
            if event_owner and not _live_event_context(
                event_owner.get("source_meta_json"), event.source, event.event_id, now
            ):
                job_id = str(event_owner["job_id"])
                committed_event = self.db.append_event(
                    connection,
                    job_id,
                    "source_context",
                    "skipped",
                    "Replay de evento de origen sin cambios",
                    _event_details(event, "duplicate"),
                )
                connection.commit()
                self.db.publish_event(committed_event)
                return StoreResult(
                    "duplicate",
                    job_id,
                    context_count=len(
                        _contexts(event_owner.get("source_meta_json"), now)
                    ),
                )
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE lower(trim(infohash))=?
                ORDER BY
                  CASE WHEN state NOT IN (
                    'done', 'manual_review', 'duplicate', 'error_terminal', 'discarded'
                  ) THEN 0 ELSE 1 END,
                  created_at DESC
                LIMIT 1
                """,
                (event.infohash,),
            ).fetchone()
            job = dict(row) if row else None

            if job and str(job.get("category") or "") != event.destination:
                job_id = str(job["job_id"])
                details = _event_details(event, "destination_conflict")
                details["current_destination"] = str(job.get("category") or "")
                committed_event = self.db.append_event(
                    connection,
                    job_id,
                    "source_context",
                    "warning",
                    "Contexto rechazado por conflicto de categoria",
                    details,
                )
                connection.commit()
                self.db.publish_event(committed_event)
                return StoreResult(
                    "destination_conflict",
                    job_id,
                    conflict_destination=str(job.get("category") or ""),
                )

            if job and str(job.get("state") or "") in TERMINAL_STATES:
                job_id = str(job["job_id"])
                details = _event_details(event, "terminal_unchanged")
                details["terminal_state"] = str(job.get("state") or "")
                committed_event = self.db.append_event(
                    connection,
                    job_id,
                    "source_context",
                    "skipped",
                    "Contexto tardio registrado sin reabrir el trabajo",
                    details,
                )
                connection.commit()
                self.db.publish_event(committed_event)
                return StoreResult(
                    "terminal_unchanged",
                    job_id,
                    context_count=len(_contexts(job.get("source_meta_json"), now)),
                )

            if event.delivery_state == "failed" and not job:
                connection.commit()
                return StoreResult("failed_without_job", None)

            if job:
                source_meta = _json_object(job.get("source_meta_json"))
            else:
                source_meta = {
                    "identity_rules": (
                        identity_context if isinstance(identity_context, dict) else {}
                    )
                }
            contexts = _contexts_from_meta(source_meta, now)
            context = event.context_payload(now)
            contexts, merge_action = _merge(contexts, context)
            source_meta["source_contexts"] = contexts
            source_meta_json = json.dumps(
                source_meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )

            if not job:
                job_id = str(uuid.uuid4())
                values = {
                    "job_id": job_id,
                    "source_uid": f"source-context:{event.infohash}",
                    "infohash": event.infohash,
                    "origin": "bridge",
                    "category": event.destination,
                    "name": NEUTRAL_JOB_NAME,
                    "state": "source_submitted",
                    "submitted_at": now,
                    "created_at": now,
                    "updated_at": now,
                    "source_meta_json": source_meta_json,
                }
                columns = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                connection.execute(
                    f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
                    tuple(values.values()),
                )
                recorded = (
                    job_id,
                    "source_context",
                    "started",
                    "Contexto de origen recibido; esperando materializacion",
                    _event_details(event, "created"),
                )
                result = StoreResult("created", job_id, True, len(contexts))
            else:
                job_id = str(job["job_id"])
                current_state = str(job.get("state") or "")
                if merge_action == "event_conflict":
                    recorded = (
                        job_id,
                        "source_context",
                        "warning",
                        "Evento de origen rechazado por contenido contradictorio",
                        _event_details(event, "event_conflict"),
                    )
                    result = StoreResult(
                        "event_conflict", job_id, context_count=len(contexts)
                    )
                elif (
                    event.delivery_state == "failed"
                    and current_state in PENDING_STATES
                    and not any(
                        str(item.get("delivery_state") or "")
                        in USABLE_DELIVERY_STATES
                        for item in contexts
                    )
                ):
                    connection.execute(
                        """
                        UPDATE jobs
                        SET source_meta_json=?, state='discarded', updated_at=?,
                            last_error_code='source_delivery_failed',
                            last_error_message='La entrega de origen ha fallado'
                        WHERE job_id=?
                        """,
                        (source_meta_json, now, job_id),
                    )
                    recorded = (
                        job_id,
                        "source_context",
                        "error",
                        "Entrega de origen fallida; trabajo pendiente descartado",
                        _event_details(event, "discarded"),
                    )
                    result = StoreResult("discarded", job_id, context_count=len(contexts))
                elif event.delivery_state == "failed" and current_state in PENDING_STATES:
                    connection.execute(
                        "UPDATE jobs SET source_meta_json=?, updated_at=? WHERE job_id=?",
                        (source_meta_json, now, job_id),
                    )
                    recorded = (
                        job_id,
                        "source_context",
                        "warning",
                        "Una entrega de origen fallo; se conserva otro contexto valido",
                        _event_details(event, "failed_context_preserved"),
                    )
                    result = StoreResult(
                        "failed_context_preserved",
                        job_id,
                        context_count=len(contexts),
                    )
                elif merge_action in {"duplicate", "stale"}:
                    recorded = (
                        job_id,
                        "source_context",
                        "skipped",
                        (
                            "Contexto de origen duplicado"
                            if merge_action == "duplicate"
                            else "Estado de origen antiguo descartado"
                        ),
                        _event_details(event, merge_action),
                    )
                    result = StoreResult(
                        "duplicate" if merge_action == "duplicate" else "stale_event",
                        job_id,
                        context_count=len(contexts),
                    )
                elif merge_action == "limit":
                    recorded = (
                        job_id,
                        "source_context",
                        "warning",
                        "Contexto recibido sin añadir otro titulo: limite alcanzado",
                        _event_details(event, "title_limit"),
                    )
                    result = StoreResult("title_limit", job_id, context_count=len(contexts))
                else:
                    updates = ["source_meta_json=?", "updated_at=?"]
                    params: List[object] = [source_meta_json, now]
                    params.append(job_id)
                    connection.execute(
                        f"UPDATE jobs SET {', '.join(updates)} WHERE job_id=?",
                        tuple(params),
                    )
                    event_type = (
                        "warning" if event.delivery_state == "failed" else "decision"
                    )
                    message = (
                        "Fallo tardio registrado sin alterar el trabajo materializado"
                        if event.delivery_state == "failed"
                        else "Contexto de origen actualizado"
                    )
                    recorded = (
                        job_id,
                        "source_context",
                        event_type,
                        message,
                        _event_details(event, merge_action),
                    )
                    result = StoreResult(
                        "failed_after_materialization"
                        if event.delivery_state == "failed"
                        else merge_action,
                        job_id,
                        context_count=len(contexts),
                    )
            committed_event = self.db.append_event(connection, *recorded)
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            concurrent = self.db.get_active_job_by_infohash(event.infohash)
            if concurrent:
                return self.apply(event, identity_context)
            raise
        except Exception:
            connection.rollback()
            raise

        self.db.publish_event(committed_event)
        return result

    def has_context(self, job: Optional[Dict[str, object]]) -> bool:
        if not job:
            return False
        return bool(_contexts(job.get("source_meta_json"), time.time()))

    def pending_by_hash(
        self, infohash: str, destination: str
    ) -> Optional[Dict[str, object]]:
        job = self.db.get_active_job_by_infohash(infohash)
        if not job or str(job.get("category") or "") != destination:
            return None
        if str(job.get("state") or "") not in PENDING_STATES | {"waiting_stable"}:
            return None
        return job if self.has_context(job) else None

    def job_by_hash(
        self, infohash: str, destination: str
    ) -> Optional[Dict[str, object]]:
        job = self.db.get_active_job_by_infohash(infohash)
        if not job:
            job = self.db.get_job_by_infohash(infohash)
        if not job or str(job.get("category") or "") != destination:
            return None
        return job if self.has_context(job) else None

    def has_pending(self, destination: str) -> bool:
        now = time.time()
        rows = self.db.connect().execute(
            """
            SELECT source_meta_json FROM jobs
            WHERE category=?
              AND state IN ('source_submitted', 'waiting_materialization')
              AND source_meta_json IS NOT NULL
            """,
            (destination,),
        ).fetchall()
        return any(_contexts(row["source_meta_json"], now) for row in rows)

    def has_recent_pending(self, destination: str, max_age_seconds: float) -> bool:
        """Indica si existe un intent reciente que aun puede correlacionarse."""

        if max_age_seconds <= 0:
            return False
        now = time.time()
        cutoff = now - float(max_age_seconds)
        rows = self.db.connect().execute(
            """
            SELECT source_meta_json FROM jobs
            WHERE category=?
              AND state IN ('source_submitted', 'waiting_materialization')
              AND source_meta_json IS NOT NULL
            """,
            (destination,),
        ).fetchall()
        for row in rows:
            for context in _contexts(row["source_meta_json"], now):
                try:
                    received_at = float(context.get("received_at") or 0)
                except (TypeError, ValueError):
                    continue
                if (
                    received_at >= cutoff
                    and str(context.get("delivery_state") or "")
                    in USABLE_DELIVERY_STATES
                ):
                    return True
        return False

    def has_correlatable(self, destination: str) -> bool:
        now = time.time()
        terminal_placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        rows = self.db.connect().execute(
            f"""
            SELECT source_meta_json FROM jobs
            WHERE category=? AND state NOT IN ({terminal_placeholders})
              AND source_meta_json IS NOT NULL
            """,
            (destination, *sorted(TERMINAL_STATES)),
        ).fetchall()
        return any(_contexts(row["source_meta_json"], now) for row in rows)

    def physical_name_updates(
        self, job: Dict[str, object], physical_name: str
    ) -> Dict[str, object]:
        if not self.has_context(job):
            return {}
        cleaned = _clean_physical_name(physical_name)
        if not cleaned or cleaned == str(job.get("name") or ""):
            return {}
        return {"name": cleaned}

    def merge_into_materialized_job(
        self,
        context_job: Dict[str, object],
        materialized_job: Dict[str, object],
        infohash: str,
    ) -> Optional[Dict[str, object]]:
        """Une una carrera FS/contexto por hash y deja un unico trabajo canonico."""

        context_id = str(context_job.get("job_id") or "")
        materialized_id = str(materialized_job.get("job_id") or "")
        if not context_id or not materialized_id:
            return None
        if context_id == materialized_id:
            return materialized_job
        connection = self.db.connect()
        committed_events: List[Dict[str, object]] = []
        now = time.time()
        try:
            connection.execute("BEGIN IMMEDIATE")
            context_row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (context_id,)
            ).fetchone()
            materialized_row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (materialized_id,)
            ).fetchone()
            if not context_row or not materialized_row:
                connection.rollback()
                return None
            context_current = dict(context_row)
            materialized_current = dict(materialized_row)
            if str(context_current.get("state") or "") in TERMINAL_STATES:
                connection.rollback()
                return materialized_current
            if str(context_current.get("category") or "") != str(
                materialized_current.get("category") or ""
            ):
                connection.rollback()
                return None
            existing_hash = str(materialized_current.get("infohash") or "").strip().lower()
            if existing_hash and existing_hash != infohash:
                committed_events.append(
                    self.db.append_event(
                        connection,
                        context_id,
                        "source_context",
                        "warning",
                        "No se unieron trabajos por conflicto de infohash",
                        {
                            "action": "materialized_hash_conflict",
                            "infohash": infohash,
                            "materialized_job_id": materialized_id,
                        },
                    )
                )
                connection.commit()
                for event in committed_events:
                    self.db.publish_event(event)
                return None

            materialized_meta = _json_object(
                materialized_current.get("source_meta_json")
            )
            context_meta = _json_object(context_current.get("source_meta_json"))
            if "identity_rules" not in materialized_meta and isinstance(
                context_meta.get("identity_rules"), dict
            ):
                materialized_meta["identity_rules"] = context_meta["identity_rules"]
            merged: List[Dict[str, object]] = []
            for candidate in (
                *_contexts_from_meta(materialized_meta, now),
                *_contexts_from_meta(context_meta, now),
            ):
                merged, _action = _merge(merged, candidate)
            materialized_meta["source_contexts"] = merged[:MAX_SOURCE_TITLES]
            materialized_meta_json = json.dumps(
                materialized_meta,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

            connection.execute(
                """
                UPDATE jobs
                SET state='duplicate', updated_at=?,
                    last_error_code='source_context_merged_by_path',
                    last_error_message=?
                WHERE job_id=?
                """,
                (
                    now,
                    f"Contexto unido al trabajo materializado {materialized_id}",
                    context_id,
                ),
            )
            connection.execute(
                """
                UPDATE jobs
                SET infohash=?, source_meta_json=?, updated_at=?
                WHERE job_id=?
                """,
                (infohash, materialized_meta_json, now, materialized_id),
            )
            committed_events.append(
                self.db.append_event(
                    connection,
                    context_id,
                    "source_context",
                    "skipped",
                    "Trabajo de contexto unido al trabajo materializado",
                    {
                        "state": "duplicate",
                        "action": "merged_into_materialized",
                        "infohash": infohash,
                        "canonical_job_id": materialized_id,
                    },
                )
            )
            committed_events.append(
                self.db.append_event(
                    connection,
                    materialized_id,
                    "source_context",
                    "decision",
                    "Contexto de origen incorporado al trabajo materializado",
                    {
                        "action": "source_context_linked",
                        "infohash": infohash,
                        "source_job_id": context_id,
                        "context_count": len(merged),
                    },
                )
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        for event in committed_events:
            self.db.publish_event(event)
        return self.db.get_job(materialized_id)

    def expire_stale_pending(self) -> int:
        now = time.time()
        connection = self.db.connect()
        expired_ids: List[str] = []
        committed_events: List[Dict[str, object]] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job_id, source_meta_json FROM jobs
                WHERE state IN ('source_submitted', 'waiting_materialization')
                  AND source_meta_json IS NOT NULL
                """
            ).fetchall()
            for row in rows:
                source_meta = _json_object(row["source_meta_json"])
                raw_contexts = source_meta.get("source_contexts")
                if not isinstance(raw_contexts, list) or not raw_contexts:
                    continue
                if _contexts(row["source_meta_json"], now):
                    continue
                job_id = str(row["job_id"])
                connection.execute(
                    """
                    UPDATE jobs
                    SET state='discarded', updated_at=?,
                        last_error_code='source_context_expired',
                        last_error_message='El contexto de origen ha caducado'
                    WHERE job_id=? AND state IN ('source_submitted', 'waiting_materialization')
                    """,
                    (now, job_id),
                )
                expired_ids.append(job_id)
                committed_events.append(
                    self.db.append_event(
                        connection,
                        job_id,
                        "source_context",
                        "skipped",
                        "Contexto de origen caducado tras 24 horas",
                        {"state": "discarded", "reason": "source_context_expired"},
                    )
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        for event in committed_events:
            self.db.publish_event(event)
        return len(expired_ids)


def _merge(
    contexts: List[Dict[str, object]], context: Dict[str, object]
) -> Tuple[List[Dict[str, object]], str]:
    for index, existing in enumerate(contexts):
        if (
            existing.get("source") == context.get("source")
            and existing.get("event_id") == context.get("event_id")
        ):
            if _title_key(str(existing.get("source_title") or "")) != _title_key(
                str(context.get("source_title") or "")
            ):
                return contexts, "event_conflict"
            existing_state = str(existing.get("delivery_state") or "")
            incoming_state = str(context.get("delivery_state") or "")
            if (
                existing_state == incoming_state
                and existing.get("route") == context.get("route")
            ):
                return contexts, "duplicate"
            if DELIVERY_STATE_ORDER.get(incoming_state, -1) < DELIVERY_STATE_ORDER.get(
                existing_state, -1
            ):
                return contexts, "stale"
            contexts[index] = dict(context)
            return contexts, "updated"

    wanted = _title_key(str(context.get("source_title") or ""))
    for index, existing in enumerate(contexts):
        if _title_key(str(existing.get("source_title") or "")) != wanted:
            continue
        if (
            str(context.get("delivery_state") or "") == "failed"
            and str(existing.get("delivery_state") or "")
            in USABLE_DELIVERY_STATES
        ):
            return contexts, "failed_duplicate_title"
        if (
            str(context.get("delivery_state")) == "intent"
            and str(existing.get("delivery_state")) in {"accepted", "already_present"}
        ):
            return contexts, "stale"
        contexts[index] = dict(context)
        return contexts, "updated"

    if len(contexts) >= MAX_SOURCE_TITLES:
        return contexts, "limit"
    contexts.append(dict(context))
    return contexts, "appended"


def _contexts(raw_meta: object, now: float) -> List[Dict[str, object]]:
    return _contexts_from_meta(_json_object(raw_meta), now)


def _contexts_from_meta(
    source_meta: Dict[str, object], now: float
) -> List[Dict[str, object]]:
    raw = source_meta.get("source_contexts")
    if not isinstance(raw, list):
        return []
    cutoff = now - CONTEXT_TTL_SECONDS
    contexts: List[Dict[str, object]] = []
    seen_titles = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != _CONTEXT_KEYS:
            continue
        try:
            received_at = float(item.get("received_at") or 0)
            event = SourceContextEvent.from_payload(
                {
                    "schema_version": 1,
                    **{key: item.get(key) for key in _CONTEXT_KEYS - {"received_at"}},
                }
            )
        except (TypeError, ValueError, SourceContextContractError):
            continue
        if received_at < cutoff or received_at > now + 60:
            continue
        normalized = event.context_payload(received_at)
        title_key = _title_key(event.source_title)
        if not title_key or title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        contexts.append(normalized)
        if len(contexts) >= MAX_SOURCE_TITLES:
            break
    return contexts


def _json_object(value: object) -> Dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _event_owner(
    connection: sqlite3.Connection, source: str, event_id: str
) -> Optional[Dict[str, object]]:
    """Localiza el propietario global del click sin crear otra fuente de verdad."""

    rows = connection.execute(
        """
        SELECT job_id, infohash, source_meta_json
        FROM jobs
        WHERE source_meta_json IS NOT NULL
        ORDER BY created_at DESC
        """
    ).fetchall()
    for row in rows:
        meta = _json_object(row["source_meta_json"])
        contexts = meta.get("source_contexts")
        if not isinstance(contexts, list):
            continue
        for context in contexts:
            if not isinstance(context, dict):
                continue
            if (
                str(context.get("source") or "") == source
                and str(context.get("event_id") or "") == event_id
            ):
                owner = dict(row)
                owner["_event"] = dict(context)
                return owner
    row = connection.execute(
        """
        SELECT jobs.job_id, jobs.infohash, jobs.source_meta_json,
               job_events.structured_json
        FROM job_events
        JOIN jobs ON jobs.job_id = job_events.job_id
        WHERE job_events.phase='source_context'
          AND json_valid(job_events.structured_json)
          AND json_extract(job_events.structured_json, '$.source')=?
          AND json_extract(job_events.structured_json, '$.event_id')=?
        ORDER BY job_events.event_id DESC
        LIMIT 1
        """,
        (source, event_id),
    ).fetchone()
    if row:
        owner = dict(row)
        owner["_event"] = _json_object(owner.pop("structured_json", None))
        return owner
    return None


def _live_event_context(
    raw_meta: object, source: str, event_id: str, now: float
) -> Optional[Dict[str, object]]:
    cutoff = now - CONTEXT_TTL_SECONDS
    raw_contexts = _json_object(raw_meta).get("source_contexts")
    if not isinstance(raw_contexts, list):
        return None
    for context in raw_contexts:
        if not isinstance(context, dict):
            continue
        try:
            received_at = float(context.get("received_at") or 0)
        except (TypeError, ValueError):
            continue
        if (
            str(context.get("source") or "") == source
            and str(context.get("event_id") or "") == event_id
            and cutoff <= received_at <= now + 60
        ):
            return dict(context)
    return None


def _title_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _title_fingerprint(value: str) -> str:
    return hashlib.sha256(_title_key(value).encode("utf-8")).hexdigest()


def _event_title_matches(payload: Dict[str, object], source_title: str) -> bool:
    stored_title = str(payload.get("source_title") or "")
    if stored_title:
        return _title_key(stored_title) == _title_key(source_title)
    return str(payload.get("source_title_fingerprint") or "") == _title_fingerprint(
        source_title
    )


def _clean_physical_name(value: str) -> str:
    cleaned = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in str(value or "")
    )
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned[:512]


def _event_details(event: SourceContextEvent, action: str) -> Dict[str, object]:
    return {
        "action": action,
        "schema_version": 1,
        "source": event.source,
        "event_id": event.event_id,
        "infohash": event.infohash,
        "destination": event.destination,
        "source_title_fingerprint": _title_fingerprint(event.source_title),
        "route": event.route,
        "delivery_state": event.delivery_state,
        "created_at": event.created_at,
    }
