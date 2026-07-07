import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


ACTIVE_STATES = {"received", "submitting_rdt", "fallback_to_qbit"}
BLOCKING_STATES = {"rdt_monitoring", "submitted_qbit"}
FINAL_REUSABLE_STATES = {"transport_done", "cleanup_done"}
BTIH_RE = re.compile(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})")


def stable_download_ref(download_url: str, source_result_id: str = "") -> str:
    value = str(download_url or "").strip()
    match = BTIH_RE.search(value)
    if match:
        return f"btih:{match.group(1).lower()}"
    if value:
        return value
    if source_result_id:
        return f"result:{source_result_id}"
    return ""


def submission_key(
    title: str,
    download_url: str,
    requested_category: str,
    resolved_category: str,
    cleanup: bool = False,
    source_result_id: str = "",
) -> str:
    payload = {
        "ref": stable_download_ref(download_url, source_result_id),
        "requested_category": str(requested_category or "auto").strip().lower(),
        "resolved_category": str(resolved_category or "manual").strip().lower(),
        "cleanup": bool(cleanup),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SubmissionStore:
    def __init__(self, path: Path, logger):
        self.path = Path(path)
        self.logger = logger
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.mark_interrupted_startup()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS submissions (
                    key TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    requested_category TEXT NOT NULL DEFAULT 'auto',
                    resolved_category TEXT NOT NULL DEFAULT 'manual',
                    source_result_id TEXT NOT NULL DEFAULT '',
                    download_ref_hash TEXT NOT NULL DEFAULT '',
                    engine TEXT NOT NULL DEFAULT '',
                    rdt_id TEXT NOT NULL DEFAULT '',
                    qbit_hash TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    last_error TEXT NOT NULL DEFAULT '',
                    duplicate_hits INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_state ON submissions(state)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_updated ON submissions(updated_at)")

    def mark_interrupted_startup(self) -> None:
        now = int(time.time())
        with self.lock, self._connect() as conn:
            conn.execute(
                f"""
                UPDATE submissions
                SET state = 'interrupted',
                    last_error = 'Servicio reiniciado durante el envio',
                    updated_at = ?
                WHERE state IN ({','.join('?' for _ in ACTIVE_STATES)})
                """,
                [now, *sorted(ACTIVE_STATES)],
            )

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        try:
            data["result"] = json.loads(data.get("result_json") or "{}")
        except Exception:
            data["result"] = {}
        return data

    def get(self, key: str) -> dict | None:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM submissions WHERE key = ?", (key,)).fetchone()
            return self._row_to_dict(row)

    def begin(
        self,
        key: str,
        title: str,
        requested_category: str,
        resolved_category: str,
        source_result_id: str,
        download_url: str,
        reuse_age_sec: int,
    ) -> tuple[dict | None, bool]:
        now = int(time.time())
        download_ref_hash = hashlib.sha256(stable_download_ref(download_url, source_result_id).encode("utf-8")).hexdigest()
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM submissions WHERE key = ?", (key,)).fetchone()
            if row:
                data = self._row_to_dict(row) or {}
                age = now - int(data.get("updated_at") or data.get("created_at") or 0)
                if data.get("state") in BLOCKING_STATES or (data.get("state") in FINAL_REUSABLE_STATES and age <= reuse_age_sec):
                    conn.execute(
                        "UPDATE submissions SET duplicate_hits = duplicate_hits + 1, updated_at = ? WHERE key = ?",
                        (now, key),
                    )
                    data["duplicate_hits"] = int(data.get("duplicate_hits") or 0) + 1
                    data["updated_at"] = now
                    return data, True

            conn.execute(
                """
                INSERT INTO submissions (
                    key, state, title, requested_category, resolved_category, source_result_id,
                    download_ref_hash, result_json, created_at, updated_at
                )
                VALUES (?, 'received', ?, ?, ?, ?, ?, '{}', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    state = 'received',
                    title = excluded.title,
                    requested_category = excluded.requested_category,
                    resolved_category = excluded.resolved_category,
                    source_result_id = excluded.source_result_id,
                    download_ref_hash = excluded.download_ref_hash,
                    engine = '',
                    rdt_id = '',
                    qbit_hash = '',
                    result_json = '{}',
                    last_error = '',
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    title,
                    requested_category,
                    resolved_category,
                    source_result_id,
                    download_ref_hash,
                    now,
                    now,
                ),
            )
            return None, False

    def update(self, key: str, **changes: Any) -> None:
        if not changes:
            return
        allowed = {
            "state",
            "engine",
            "rdt_id",
            "qbit_hash",
            "last_error",
            "result_json",
        }
        values: dict[str, Any] = {}
        for name, value in changes.items():
            if name == "result":
                values["result_json"] = json.dumps(value or {}, ensure_ascii=False, sort_keys=True)
            elif name in allowed:
                values[name] = str(value or "")
        values["updated_at"] = int(time.time())
        assignments = ", ".join(f"{name} = ?" for name in values)
        params = [*values.values(), key]
        with self.lock, self._connect() as conn:
            conn.execute(f"UPDATE submissions SET {assignments} WHERE key = ?", params)

    def stats(self) -> dict:
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS count FROM submissions GROUP BY state").fetchall()
            return {str(row["state"]): int(row["count"]) for row in rows}

    def recent(self, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit or 20), 100))
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM submissions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._row_to_dict(row) or {} for row in rows]
