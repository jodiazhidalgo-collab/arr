from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class SearchHistoryStore:
    def __init__(
        self,
        path: Path,
        logger,
        retention_days: int = 30,
        max_searches: int = 300,
        page_size: int = 25,
    ) -> None:
        self.path = Path(path)
        self.logger = logger
        self.retention_days = max(1, int(retention_days or 30))
        self.max_searches = max(1, int(max_searches or 300))
        self.page_size = max(1, min(int(page_size or 25), 100))
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def initialize(self) -> None:
        with self.lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    query TEXT NOT NULL,
                    category TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS search_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    download_url TEXT NOT NULL,
                    FOREIGN KEY(search_id) REFERENCES searches(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_searches_created ON searches(created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_search_results_search ON search_results(search_id, position);
                """
            )
            self._prune(conn, int(time.time()))

    def _prune(self, conn: sqlite3.Connection, now: int) -> None:
        cutoff = now - (self.retention_days * 24 * 60 * 60)
        conn.execute("DELETE FROM searches WHERE created_at < ?", (cutoff,))
        conn.execute(
            """
            DELETE FROM searches
            WHERE id NOT IN (
                SELECT id FROM searches ORDER BY created_at DESC, id DESC LIMIT ?
            )
            """,
            (self.max_searches,),
        )

    def record(self, query: str, category: str, results: list[dict[str, Any]], state: str = "done") -> int:
        now = int(time.time())
        clean_query = str(query or "").strip()[:300]
        clean_category = str(category or "auto").strip()[:24] or "auto"
        clean_state = str(state or "done").strip()[:24] or "done"
        rows: list[tuple[int, str, str]] = []
        for position, item in enumerate(results or [], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "Sin titulo").strip()[:500] or "Sin titulo"
            download_url = str(item.get("download_url") or item.get("magnet") or "").strip()
            rows.append((position, title, download_url))
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO searches(created_at, query, category, state, result_count) VALUES (?, ?, ?, ?, ?)",
                (now, clean_query, clean_category, clean_state, len(rows)),
            )
            search_id = int(cursor.lastrowid)
            if rows:
                conn.executemany(
                    "INSERT INTO search_results(search_id, position, title, download_url) VALUES (?, ?, ?, ?)",
                    [(search_id, position, title, download_url) for position, title, download_url in rows],
                )
            self._prune(conn, now)
            return search_id

    def overview(self) -> dict[str, Any]:
        with self.lock, self._connect() as conn:
            self._prune(conn, int(time.time()))
            rows = conn.execute(
                "SELECT id, created_at, query, category, state, result_count FROM searches ORDER BY created_at DESC, id DESC"
            ).fetchall()
        days: list[dict[str, Any]] = []
        by_date: dict[str, dict[str, Any]] = {}
        for row in rows:
            stamp = datetime.fromtimestamp(int(row["created_at"])).astimezone()
            date_key = stamp.strftime("%Y-%m-%d")
            day = by_date.get(date_key)
            if day is None:
                day = {"date": date_key, "label": stamp.strftime("%d/%m/%Y"), "searches": []}
                by_date[date_key] = day
                days.append(day)
            day["searches"].append(
                {
                    "id": int(row["id"]),
                    "time": stamp.strftime("%H:%M"),
                    "query": str(row["query"]),
                    "category": str(row["category"]),
                    "state": str(row["state"]),
                    "result_count": int(row["result_count"]),
                }
            )
        return {
            "days": days,
            "retention_days": self.retention_days,
            "max_searches": self.max_searches,
            "page_size": self.page_size,
        }

    def results_page(self, search_id: int, page: int = 1) -> dict[str, Any] | None:
        page = max(1, int(page or 1))
        with self.lock, self._connect() as conn:
            search = conn.execute(
                "SELECT id, query, result_count FROM searches WHERE id = ?", (int(search_id),)
            ).fetchone()
            if search is None:
                return None
            total = int(search["result_count"])
            page_count = max(1, (total + self.page_size - 1) // self.page_size)
            page = min(page, page_count)
            offset = (page - 1) * self.page_size
            rows = conn.execute(
                """
                SELECT position, title, download_url
                FROM search_results
                WHERE search_id = ?
                ORDER BY position
                LIMIT ? OFFSET ?
                """,
                (int(search_id), self.page_size, offset),
            ).fetchall()
        return {
            "search_id": int(search["id"]),
            "query": str(search["query"]),
            "page": page,
            "page_count": page_count,
            "page_size": self.page_size,
            "total": total,
            "results": [
                {
                    "position": int(row["position"]),
                    "title": str(row["title"]),
                    "download_url": str(row["download_url"]),
                }
                for row in rows
            ],
        }
