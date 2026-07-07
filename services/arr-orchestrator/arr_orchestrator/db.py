import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    source_uid TEXT UNIQUE NOT NULL,
    infohash TEXT,
    origin TEXT NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    torrent_path TEXT,
    source_path TEXT,
    stage_path TEXT,
    output_root TEXT,
    qbt_hash TEXT,
    rdt_id TEXT,
    rdt_progress REAL,
    submitted_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    retry_source INTEGER NOT NULL DEFAULT 0,
    retry_extract INTEGER NOT NULL DEFAULT 0,
    retry_filebot INTEGER NOT NULL DEFAULT 0,
    source_meta_json TEXT,
    identity_json TEXT,
    identity_retry_at REAL,
    last_error_code TEXT,
    last_error_message TEXT,
    result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_infohash ON jobs(infohash);
CREATE INDEX IF NOT EXISTS idx_jobs_source_path ON jobs(source_path);

CREATE TABLE IF NOT EXISTS job_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts REAL NOT NULL,
    phase TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    structured_json TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, event_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolver_cache (
    cache_key TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resolver_cache_expires_at
ON resolver_cache(expires_at);
"""


RUNNING_STATES = {
    "received",
    "source_submitted",
    "waiting_materialization",
    "waiting_stable",
    "staging",
    "extracting",
    "filebot_running",
    "media_postprocess_running",
    "trailer_running",
    "verifying_output",
}
FINISHED_STATES = {
    "ready_stage",
    "ready_extract",
    "ready_filebot",
    "media_postprocess_ready",
    "trailer_ready",
    "ready_cleanup",
    "done",
}
ERROR_STATES = {"manual_review", "error_terminal"}
SKIPPED_STATES = {"duplicate", "discarded", "dry_run_ready"}
RETRY_STATES = {"identity_retry"}

PHASE_ALIASES = {
    "ingest": "received",
    "stability": "stable_wait",
    "stage": "staging",
}

EVENT_TYPE_ALIASES = {
    "created": "started",
    "resolved": "decision",
    "legacy": "decision",
    "observed": "started",
    "changed": "retry",
    "waiting": "decision",
    "qbt_deleted": "decision",
    "rdt_deleted": "decision",
}

VALID_EVENT_TYPES = {
    "started",
    "finished",
    "decision",
    "command",
    "error",
    "warning",
    "skipped",
    "retry",
}


class Database:
    def __init__(
        self,
        path: Path,
        event_recorder: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.path = path
        self.event_recorder = event_recorder
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            self._local.connection = connection
        return connection

    def initialize(self) -> None:
        connection = self.connect()
        connection.executescript(SCHEMA)
        self._ensure_column("jobs", "source_meta_json", "TEXT")
        self._ensure_column("jobs", "identity_json", "TEXT")
        self._ensure_column("jobs", "identity_retry_at", "REAL")
        connection.commit()

    def _ensure_column(self, table: str, column: str, sql_type: str) -> None:
        columns = {
            str(row["name"])
            for row in self.connect().execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.connect().execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def create_job(
        self,
        source_uid: str,
        origin: str,
        category: str,
        name: str,
        state: str = "received",
        **fields: Any,
    ) -> Dict[str, Any]:
        existing = self.get_job_by_source_uid(source_uid)
        if existing:
            return existing
        now = time.time()
        job_id = str(uuid.uuid4())
        values = {
            "job_id": job_id,
            "source_uid": source_uid,
            "origin": origin,
            "category": category,
            "name": name,
            "state": state,
            "created_at": now,
            "updated_at": now,
        }
        values.update(fields)
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        self.connect().execute(
            f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        self.connect().commit()
        self.add_event(job_id, "received", "started", f"Trabajo creado: {name}", values)
        return self.get_job(job_id)

    def update_job(self, job_id: str, **fields: Any) -> Dict[str, Any]:
        if not fields:
            return self.get_job(job_id)
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key}=?" for key in fields)
        self.connect().execute(
            f"UPDATE jobs SET {assignments} WHERE job_id=?",
            tuple(fields.values()) + (job_id,),
        )
        self.connect().commit()
        return self.get_job(job_id)

    def transition(
        self,
        job_id: str,
        state: str,
        phase: str,
        message: str,
        **fields: Any,
    ) -> Dict[str, Any]:
        fields["state"] = state
        job = self.update_job(job_id, **fields)
        self.add_event(job_id, phase, _event_type_for_state(state), message, {"state": state, **fields})
        return job

    def add_event(
        self,
        job_id: str,
        phase: str,
        event_type: str,
        message: str,
        structured: Optional[Dict[str, Any]] = None,
    ) -> None:
        phase = _clean_phase(phase)
        event_type = _clean_event_type(event_type, structured)
        ts = time.time()
        cursor = self.connect().execute(
            """
            INSERT INTO job_events(job_id, ts, phase, event_type, message, structured_json)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                ts,
                phase,
                event_type,
                message,
                json.dumps(structured, ensure_ascii=False, default=str) if structured else None,
            ),
        )
        self.connect().commit()
        self._record_event_best_effort(
            {
                "event_id": cursor.lastrowid,
                "job_id": job_id,
                "ts": ts,
                "phase": phase,
                "event_type": event_type,
                "message": message,
                "structured": structured,
            }
        )

    def _record_event_best_effort(self, event: Dict[str, Any]) -> None:
        if not self.event_recorder:
            return
        try:
            self.event_recorder(event)
        except Exception:
            return

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_job_by_source_uid(self, source_uid: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute(
            "SELECT * FROM jobs WHERE source_uid=?", (source_uid,)
        ).fetchone()
        return dict(row) if row else None

    def get_job_by_infohash(self, infohash: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute(
            "SELECT * FROM jobs WHERE lower(infohash)=lower(?) ORDER BY created_at DESC LIMIT 1",
            (infohash,),
        ).fetchone()
        return dict(row) if row else None

    def get_active_job_by_infohash(self, infohash: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute(
            """
            SELECT * FROM jobs
            WHERE lower(infohash)=lower(?)
              AND state NOT IN (
                'done', 'manual_review', 'duplicate', 'error_terminal', 'discarded'
              )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (infohash,),
        ).fetchone()
        return dict(row) if row else None

    def get_job_by_source_path(self, source_path: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute(
            """
            SELECT * FROM jobs
            WHERE source_path=? AND state NOT IN (
                'done', 'manual_review', 'duplicate', 'error_terminal', 'discarded'
            )
            ORDER BY created_at
            LIMIT 1
            """,
            (source_path,),
        ).fetchone()
        return dict(row) if row else None

    def find_waiting_job(self, category: str, name: str) -> Optional[Dict[str, Any]]:
        rows = self.connect().execute(
            """
            SELECT * FROM jobs
            WHERE category=? AND state IN (
                'received', 'source_submitted', 'waiting_materialization', 'waiting_stable',
                'retry_wait'
            )
            ORDER BY created_at
            """,
            (category,),
        ).fetchall()
        wanted = _normalize_name(name)
        for row in rows:
            current = dict(row)
            if _normalize_name(current["name"]) == wanted:
                return current
        return None

    def jobs_in_states(self, states: Iterable[str], limit: int = 100) -> List[Dict[str, Any]]:
        states = list(states)
        placeholders = ", ".join("?" for _ in states)
        rows = self.connect().execute(
            f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY updated_at LIMIT ?",
            tuple(states) + (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.connect().execute(
            "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def events_for_job(self, job_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self.connect().execute(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY event_id DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def job_detail(self, job_id: str, limit: int = 1000) -> Optional[Dict[str, Any]]:
        job = self.get_job(job_id)
        if not job:
            return None
        rows = self.connect().execute(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY event_id ASC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        previous_ts: Optional[float] = None
        events: List[Dict[str, Any]] = []
        reports: List[str] = []
        seen_reports: Set[str] = set()
        for row in rows:
            event = dict(row)
            structured = _load_json(event.get("structured_json"))
            _collect_report_paths(structured, reports, seen_reports)
            structured = _compact_structured(structured)
            phase = _clean_phase(str(event.get("phase") or ""))
            event_type = _clean_event_type(str(event.get("event_type") or ""), structured)
            ts = float(event.get("ts") or 0)
            delta = round(ts - previous_ts, 3) if previous_ts else None
            previous_ts = ts
            event_payload = {
                "event_id": event["event_id"],
                "ts": ts,
                "phase": phase,
                "event_type": event_type,
                "message": event.get("message") or "",
                "seconds_since_previous": delta,
                "structured": structured,
            }
            events.append(event_payload)

        result = _load_json(job.get("result_json"))
        source_meta = _load_json(job.get("source_meta_json"))
        identity = _load_json(job.get("identity_json"))
        for payload in (result, source_meta, identity):
            _collect_report_paths(payload, reports, seen_reports)
        job_payload = dict(job)
        job_payload.pop("result_json", None)
        job_payload.pop("source_meta_json", None)
        job_payload.pop("identity_json", None)

        return {
            "ok": True,
            "job": job_payload,
            "result_summary": _result_summary(result),
            "source_meta": source_meta,
            "identity": identity,
            "timeline": events,
            "timings": _phase_timings(events),
            "decisions": [
                event for event in events
                if event["event_type"] in {"decision", "warning", "skipped", "retry"}
            ],
            "errors": _job_errors(job, events),
            "reports": reports,
        }

    def get_resolver_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        row = self.connect().execute(
            "SELECT * FROM resolver_cache WHERE cache_key=? AND expires_at>?",
            (cache_key, now),
        ).fetchone()
        if row:
            return dict(row)
        self.connect().execute(
            "DELETE FROM resolver_cache WHERE cache_key=? AND expires_at<=?",
            (cache_key, now),
        )
        self.connect().commit()
        return None

    def set_resolver_cache(
        self,
        cache_key: str,
        media_type: str,
        payload_json: str,
        ttl_seconds: int,
    ) -> None:
        now = time.time()
        self.connect().execute(
            """
            INSERT INTO resolver_cache(
                cache_key, media_type, payload_json, created_at, expires_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                media_type=excluded.media_type,
                payload_json=excluded.payload_json,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at
            """,
            (cache_key, media_type, payload_json, now, now + ttl_seconds),
        )
        self.connect().commit()

    def purge_expired_resolver_cache(self) -> int:
        cursor = self.connect().execute(
            "DELETE FROM resolver_cache WHERE expires_at<=?", (time.time(),)
        )
        self.connect().commit()
        return int(cursor.rowcount)


def _normalize_name(value: str) -> str:
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())


def _clean_phase(phase: str) -> str:
    value = (phase or "unknown").strip().lower()
    return PHASE_ALIASES.get(value, value)


def _event_type_for_state(state: str) -> str:
    if state in ERROR_STATES:
        return "error"
    if state in SKIPPED_STATES:
        return "skipped"
    if state in RETRY_STATES:
        return "retry"
    if state in RUNNING_STATES:
        return "started"
    if state in FINISHED_STATES:
        return "finished"
    return "decision"


def _clean_event_type(event_type: str, structured: Optional[Dict[str, Any]] = None) -> str:
    value = (event_type or "decision").strip().lower()
    if value == "transition":
        state = str((structured or {}).get("state") or "")
        return _event_type_for_state(state)
    value = EVENT_TYPE_ALIASES.get(value, value)
    return value if value in VALID_EVENT_TYPES else "decision"


def _load_json(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _phase_timings(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for event in events:
        phase = str(event.get("phase") or "unknown")
        ts = float(event.get("ts") or 0)
        if phase not in grouped:
            grouped[phase] = {
                "phase": phase,
                "started_at": ts,
                "finished_at": ts,
                "events": 0,
            }
            order.append(phase)
        grouped[phase]["finished_at"] = ts
        grouped[phase]["events"] += 1
    result: List[Dict[str, Any]] = []
    for phase in order:
        item = grouped[phase]
        duration = max(0.0, float(item["finished_at"]) - float(item["started_at"]))
        result.append({**item, "duration_seconds": round(duration, 3)})
    return result


def _job_errors(job: Dict[str, Any], events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    errors = [event for event in events if event["event_type"] == "error"]
    if job.get("last_error_code") or job.get("last_error_message"):
        errors.append(
            {
                "event_id": None,
                "ts": job.get("updated_at"),
                "phase": "job",
                "event_type": "error",
                "message": job.get("last_error_message") or job.get("last_error_code") or "",
                "structured": {
                    "last_error_code": job.get("last_error_code"),
                    "last_error_message": job.get("last_error_message"),
                },
            }
        )
    return errors


def _compact_structured(payload: Any) -> Any:
    if isinstance(payload, dict):
        compact: Dict[str, Any] = {}
        for key, value in payload.items():
            if key == "result_json":
                compact["result_summary"] = _result_summary(_load_json(value))
                continue
            compact[key] = _compact_structured(value)
        return compact
    if isinstance(payload, list):
        if len(payload) > 20:
            return {
                "items": len(payload),
                "first": [_compact_structured(item) for item in payload[:5]],
            }
        return [_compact_structured(item) for item in payload]
    if isinstance(payload, str) and len(payload) > 1200:
        return payload[-1200:]
    return payload


def _result_summary(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    summary: Dict[str, Any] = {}
    for key in (
        "status",
        "job_id",
        "source",
        "review_path",
        "reason_file",
        "reports_dir",
        "duplicate",
        "mode",
        "log_file",
    ):
        if key in result:
            summary[key] = result.get(key)
    if isinstance(result.get("final"), dict):
        summary["final"] = result["final"]
    if isinstance(result.get("plan"), dict):
        plan = result["plan"]
        summary["plan"] = {
            "estado": plan.get("estado"),
            "problemas": plan.get("problemas"),
            "audio_modo": plan.get("audio_modo"),
            "subtitulo_titulo": plan.get("subtitulo_titulo"),
        }
    if isinstance(result.get("process"), dict):
        process = result["process"]
        summary["process"] = {
            "ok": process.get("ok"),
            "returncode": process.get("returncode"),
            "audio_modo": process.get("audio_modo"),
            "capitulos": process.get("capitulos"),
            "tamano_salida": process.get("tamano_salida"),
        }
    if isinstance(result.get("verification"), dict):
        verification = result["verification"]
        summary["verification"] = {
            "estado": verification.get("estado"),
            "problemas": verification.get("problemas"),
        }
    if result.get("rescue"):
        summary["rescue"] = _compact_structured(result.get("rescue"))
    if result.get("moves"):
        summary["moves"] = result.get("moves")
    if result.get("output_media"):
        summary["output_media"] = result.get("output_media")
    return summary


def _collect_report_paths(payload: Any, reports: List[str], seen: Set[str]) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered == "result_json":
                _collect_report_paths(_load_json(value), reports, seen)
                continue
            if lowered in {
                "log_file",
                "reports_dir",
                "report_path",
                "review_path",
                "reason_file",
                "final_dir",
                "final_video",
                "final_srt",
            }:
                _add_report_path(value, reports, seen)
            _collect_report_paths(value, reports, seen)
    elif isinstance(payload, list):
        for item in payload:
            _collect_report_paths(item, reports, seen)
    elif isinstance(payload, str):
        if (
            payload.startswith("/config/")
            or payload.startswith("/data/media/repetidas_vs_error")
            or payload.endswith((".json", ".log", ".txt"))
        ):
            _add_report_path(payload, reports, seen)


def _add_report_path(value: Any, reports: List[str], seen: Set[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    path = value.strip()
    if path not in seen:
        seen.add(path)
        reports.append(path)
