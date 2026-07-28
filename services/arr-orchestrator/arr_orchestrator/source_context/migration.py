"""Migracion atomica del indice de correlacion por infohash."""

from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
from typing import Dict, List

from .contract import SourceContextContractError, SourceContextEvent
from .policy import (
    CONTEXT_TTL_SECONDS,
    DELIVERY_STATE_ORDER,
    MAX_SOURCE_TITLES,
    USABLE_DELIVERY_STATES,
)


TERMINAL_STATES = (
    "done",
    "manual_review",
    "duplicate",
    "error_terminal",
    "discarded",
)
_STATE_PROGRESS = {
    "source_submitted": 5,
    "received": 10,
    "waiting_materialization": 15,
    "waiting_stable": 20,
    "retry_wait": 25,
    "identity_retry": 25,
    "staging": 30,
    "extracting": 40,
    "ready_extract": 45,
    "filebot_running": 50,
    "ready_filebot": 55,
    "media_postprocess_running": 60,
    "media_postprocess_ready": 65,
    "trailer_running": 70,
    "trailer_ready": 75,
    "verifying_output": 80,
    "ready_cleanup": 90,
}


def migrate_active_infohash_duplicates(connection: sqlite3.Connection) -> None:
    """Fusiona contexto valido y cierra duplicados legacy antes del indice unico."""

    placeholders = ", ".join("?" for _ in TERMINAL_STATES)
    now = time.time()
    try:
        connection.execute("BEGIN IMMEDIATE")
        duplicate_hashes = _active_duplicate_hashes(connection, placeholders)
        for duplicate_hash in duplicate_hashes:
            jobs = _active_jobs_for_hash(
                connection, str(duplicate_hash["infohash"] or ""), placeholders
            )
            categories = {str(job.get("category") or "") for job in jobs}
            if len(categories) > 1:
                raise RuntimeError(
                    "No se pueden migrar trabajos activos con el mismo infohash "
                    "y categorias distintas."
                )

        connection.execute(
            """
            UPDATE jobs
            SET infohash=lower(trim(infohash))
            WHERE infohash IS NOT NULL
              AND length(trim(infohash))=40
              AND lower(trim(infohash)) NOT GLOB '*[^0-9a-f]*'
              AND infohash<>lower(trim(infohash))
            """
        )
        for duplicate_hash in duplicate_hashes:
            infohash = str(duplicate_hash["infohash"] or "")
            jobs = _active_jobs_for_hash(connection, infohash, placeholders)
            if len(jobs) < 2:
                continue
            canonical = max(jobs, key=_job_rank)
            canonical_id = str(canonical["job_id"])
            canonical_category = str(canonical.get("category") or "")
            merged_meta = _json_object(canonical.get("source_meta_json"))
            if "identity_rules" not in merged_meta:
                for job in jobs:
                    identity_rules = _json_object(job.get("source_meta_json")).get(
                        "identity_rules"
                    )
                    if isinstance(identity_rules, dict):
                        merged_meta["identity_rules"] = identity_rules
                        break
            contexts = _merged_contexts(
                jobs, infohash=infohash, category=canonical_category, now=now
            )
            if contexts:
                merged_meta["source_contexts"] = contexts
            if merged_meta:
                connection.execute(
                    "UPDATE jobs SET source_meta_json=?, updated_at=? WHERE job_id=?",
                    (
                        json.dumps(
                            merged_meta,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now,
                        canonical_id,
                    ),
                )

            duplicate_ids: List[str] = []
            for duplicate in jobs:
                duplicate_id = str(duplicate["job_id"])
                if duplicate_id == canonical_id:
                    continue
                duplicate_ids.append(duplicate_id)
                connection.execute(
                    """
                    UPDATE jobs
                    SET state='duplicate', updated_at=?,
                        last_error_code='duplicate_active_infohash_migration',
                        last_error_message=?
                    WHERE job_id=?
                    """,
                    (
                        now,
                        f"Trabajo legacy duplicado; continua {canonical_id}",
                        duplicate_id,
                    ),
                )
                _insert_event(
                    connection,
                    duplicate_id,
                    now,
                    "skipped",
                    "Duplicado legacy por infohash cerrado durante la migracion",
                    {
                        "state": "duplicate",
                        "reason": "active_infohash_migration",
                        "infohash": infohash,
                        "canonical_job_id": canonical_id,
                    },
                )
            if contexts:
                _insert_event(
                    connection,
                    canonical_id,
                    now,
                    "decision",
                    "Contextos de origen legacy unidos al trabajo canonico",
                    {
                        "reason": "active_infohash_context_merge",
                        "infohash": infohash,
                        "context_count": len(contexts),
                        "duplicate_job_ids": duplicate_ids,
                    },
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _merged_contexts(
    jobs: List[Dict[str, object]], *, infohash: str, category: str, now: float
) -> List[Dict[str, object]]:
    by_event_title: Dict[tuple, Dict[str, object]] = {}
    cutoff = now - CONTEXT_TTL_SECONDS
    for job in jobs:
        raw_contexts = _json_object(job.get("source_meta_json")).get("source_contexts")
        if not isinstance(raw_contexts, list):
            continue
        for raw in raw_contexts:
            if not isinstance(raw, dict):
                continue
            try:
                received_at = float(raw.get("received_at") or 0)
                event = SourceContextEvent.from_payload(
                    {
                        "schema_version": 1,
                        **{
                            key: raw.get(key)
                            for key in (
                            "event_id",
                            "source",
                            "infohash",
                            "destination",
                            "source_title",
                            "route",
                            "delivery_state",
                            "created_at",
                            )
                        },
                    }
                )
            except (TypeError, ValueError, SourceContextContractError):
                continue
            if (
                event.infohash != infohash
                or event.destination != category
                or received_at < cutoff
                or received_at > now + 60
            ):
                continue
            context = event.context_payload(received_at)
            event_title_key = (event.source, event.event_id, _title_key(event.source_title))
            existing = by_event_title.get(event_title_key)
            if existing is None or _context_rank(context) > _context_rank(existing):
                by_event_title[event_title_key] = context

    by_title: Dict[str, Dict[str, object]] = {}
    for context in by_event_title.values():
        title_key = _title_key(str(context.get("source_title") or ""))
        existing = by_title.get(title_key)
        if existing is None or _prefer_same_title(context, existing):
            by_title[title_key] = context
    return sorted(by_title.values(), key=_context_rank, reverse=True)[:MAX_SOURCE_TITLES]


def _prefer_same_title(
    candidate: Dict[str, object], existing: Dict[str, object]
) -> bool:
    """Un fallo de otro click nunca eclipsa una entrega aun utilizable."""

    candidate_state = str(candidate.get("delivery_state") or "")
    existing_state = str(existing.get("delivery_state") or "")
    if candidate_state == "failed" and existing_state in USABLE_DELIVERY_STATES:
        return False
    if existing_state == "failed" and candidate_state in USABLE_DELIVERY_STATES:
        return True
    return _context_rank(candidate) > _context_rank(existing)


def _active_duplicate_hashes(
    connection: sqlite3.Connection, placeholders: str
) -> List[sqlite3.Row]:
    return connection.execute(
        f"""
        SELECT lower(trim(infohash)) AS infohash
        FROM jobs
        WHERE infohash IS NOT NULL AND length(trim(infohash))=40
          AND state NOT IN ({placeholders})
        GROUP BY lower(trim(infohash))
        HAVING COUNT(*) > 1
        """,
        TERMINAL_STATES,
    ).fetchall()


def _active_jobs_for_hash(
    connection: sqlite3.Connection, infohash: str, placeholders: str
) -> List[Dict[str, object]]:
    rows = connection.execute(
        f"""
        SELECT * FROM jobs
        WHERE lower(trim(infohash))=? AND state NOT IN ({placeholders})
        """,
        (infohash, *TERMINAL_STATES),
    ).fetchall()
    return [dict(row) for row in rows]


def _context_rank(context: Dict[str, object]) -> tuple:
    return (
        DELIVERY_STATE_ORDER.get(str(context.get("delivery_state") or ""), -1),
        float(context.get("received_at") or 0),
        str(context.get("source") or ""),
        str(context.get("event_id") or ""),
    )


def _job_rank(job: Dict[str, object]) -> tuple:
    return (
        bool(job.get("source_path")),
        _STATE_PROGRESS.get(str(job.get("state") or ""), 0),
        bool(job.get("identity_json")),
        bool(job.get("qbt_hash") or job.get("rdt_id")),
        float(job.get("updated_at") or 0),
        float(job.get("created_at") or 0),
        str(job.get("job_id") or ""),
    )


def _insert_event(
    connection: sqlite3.Connection,
    job_id: str,
    ts: float,
    event_type: str,
    message: str,
    structured: Dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO job_events(job_id, ts, phase, event_type, message, structured_json)
        VALUES(?, ?, 'source_context', ?, ?, ?)
        """,
        (
            job_id,
            ts,
            event_type,
            message,
            json.dumps(structured, ensure_ascii=False, sort_keys=True),
        ),
    )


def _json_object(value: object) -> Dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _title_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
