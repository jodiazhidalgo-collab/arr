import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from modulos.search_history import SearchHistoryStore


class SearchHistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.temporary.name) / "history.sqlite3"
        self.store = SearchHistoryStore(
            self.path,
            app_module.logger,
            retention_days=30,
            max_searches=3,
            page_size=2,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_records_empty_searches(self) -> None:
        search_id = self.store.record("Sin coincidencias", "movies", [], "done")

        history = self.store.overview()
        search = history["days"][0]["searches"][0]
        page = self.store.results_page(search_id, 1)

        self.assertEqual(search["query"], "Sin coincidencias")
        self.assertEqual(search["result_count"], 0)
        self.assertEqual(search["source"], "bridge")
        self.assertEqual(page["results"], [])
        self.assertEqual(page["page_count"], 1)

    def test_paginates_results_without_losing_links(self) -> None:
        rows = [
            {"title": f"Resultado {index}", "download_url": f"magnet:?xt=urn:btih:{index:040d}"}
            for index in range(1, 6)
        ]
        search_id = self.store.record("Cinco", "auto", rows)

        first = self.store.results_page(search_id, 1)
        third = self.store.results_page(search_id, 3)

        self.assertEqual(first["page_count"], 3)
        self.assertEqual([item["title"] for item in first["results"]], ["Resultado 1", "Resultado 2"])
        self.assertEqual(third["results"][0]["download_url"], rows[4]["download_url"])
        self.assertIsInstance(third["results"][0]["result_id"], int)

    def test_records_wolfmax_source(self) -> None:
        search_id = self.store.record(
            "Olivia S01E06 1080p",
            "tv",
            [{"title": "Olivia S01E06", "download_url": "http://gluetun:9117/dl/wolfmax4k/?path=x"}],
            source="wolfmax",
        )

        overview = self.store.overview()
        page = self.store.results_page(search_id, 1)
        result = self.store.result(page["results"][0]["result_id"])

        self.assertEqual(overview["days"][0]["searches"][0]["source"], "wolfmax")
        self.assertEqual(page["source"], "wolfmax")
        self.assertEqual(result["search_id"], search_id)

    def test_migrates_and_marks_legacy_wolfmax_searches(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as conn:
            conn.executescript(
                """
                CREATE TABLE searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    query TEXT NOT NULL,
                    category TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE search_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    download_url TEXT NOT NULL
                );
                INSERT INTO searches(created_at, query, category, state, result_count)
                VALUES (2000000000, 'La Casa del Dragon S01E08', 'tv', 'done', 1);
                INSERT INTO search_results(search_id, position, title, download_url)
                VALUES (1, 1, 'La Casa del Dragon S01E08', 'http://gluetun:9117/dl/wolfmax4k/?path=x');
                """
            )

        legacy_store = SearchHistoryStore(legacy_path, app_module.logger)

        self.assertEqual(legacy_store.overview()["days"][0]["searches"][0]["source"], "wolfmax")

    def test_prunes_to_maximum_searches(self) -> None:
        for index in range(5):
            self.store.record(f"Busqueda {index}", "auto", [])

        searches = [search for day in self.store.overview()["days"] for search in day["searches"]]

        self.assertEqual(len(searches), 3)
        self.assertEqual(searches[0]["query"], "Busqueda 4")
        self.assertEqual(searches[-1]["query"], "Busqueda 2")


class SearchHistoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.previous = app_module.search_history
        app_module.search_history = SearchHistoryStore(
            Path(self.temporary.name) / "history.sqlite3",
            app_module.logger,
            page_size=25,
        )

    def tearDown(self) -> None:
        app_module.search_history = self.previous
        self.temporary.cleanup()

    def test_history_endpoints_return_overview_and_page(self) -> None:
        search_id = app_module.search_history.record(
            "Pelicula",
            "movies",
            [
                {
                    "title": "Pelicula 2026",
                    "download_url": "http://gluetun:9117/dl/wolfmax4k/?jackett_apikey=secret&path=x",
                }
            ],
            source="wolfmax",
        )
        client = app_module.app.test_client()

        overview = client.get("/api/history/searches")
        page = client.get(f"/api/history/searches/{search_id}/results?page=1")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.get_json()["history"]["days"][0]["searches"][0]["result_count"], 1)
        self.assertEqual(overview.get_json()["history"]["days"][0]["searches"][0]["source"], "wolfmax")
        self.assertEqual(page.status_code, 200)
        item = page.get_json()["results"][0]
        self.assertEqual(item["title"], "Pelicula 2026")
        self.assertEqual(page.get_json()["source"], "wolfmax")
        self.assertNotIn("download_url", item)
        self.assertNotIn("jackett_apikey", item["copy_value"])
        self.assertEqual(
            item["copy_value"],
            f"http://localhost/api/history/results/{item['result_id']}/torrent",
        )

        with patch.object(app_module, "download_torrent_payload", return_value=(b"d4:infodee", "")):
            torrent = client.get(f"/api/history/results/{item['result_id']}/torrent")

        self.assertEqual(torrent.status_code, 200)
        self.assertEqual(torrent.data, b"d4:infodee")
        self.assertEqual(torrent.mimetype, "application/x-bittorrent")
        self.assertIn(".torrent", torrent.headers["Content-Disposition"])
        self.assertEqual(torrent.headers["Cache-Control"], "private, no-store")

    def test_history_keeps_magnets_as_the_copy_value(self) -> None:
        magnet = "magnet:?xt=urn:btih:" + "b" * 40
        search_id = app_module.search_history.record(
            "Batman",
            "movies",
            [{"title": "Batman", "download_url": magnet}],
        )

        payload = app_module.app.test_client().get(f"/api/history/searches/{search_id}/results?page=1").get_json()

        self.assertEqual(payload["results"][0]["copy_value"], magnet)
        self.assertNotIn("download_url", payload["results"][0])

    def test_async_search_job_records_the_history_used_by_the_ui(self) -> None:
        rows = [{"title": "Batman", "download_url": "magnet:?xt=urn:btih:" + "a" * 40}]
        with (
            patch.object(app_module, "search_jackett_many", return_value=rows),
            patch.object(app_module, "cache_results"),
            patch.object(app_module.arr_trace, "start"),
            patch.object(app_module.arr_trace, "finish"),
        ):
            result = app_module.run_search_job(
                "Batman",
                ["wolfmax4k"],
                "auto",
                {"section": "peliculas4k"},
                "search-test",
            )

        searches = app_module.search_history.overview()["days"][0]["searches"]
        self.assertEqual(result["count"], 1)
        self.assertEqual(searches[0]["query"], "Batman")
        self.assertEqual(searches[0]["result_count"], 1)
        self.assertEqual(searches[0]["source"], "wolfmax")


class SearchHistoryFrontendContractTests(unittest.TestCase):
    def test_mobile_history_keeps_copy_buttons_fixed_and_syncs_title_scroll(self) -> None:
        service_root = Path(__file__).resolve().parents[1]
        script = (service_root / "static" / "js" / "app.js").read_text(encoding="utf-8")
        styles = (service_root / "static" / "css" / "app.css").read_text(encoding="utf-8")

        self.assertIn('rows.querySelectorAll(".history-result-title-scroll")', script)
        self.assertIn('row.append(titleScroll, copy)', script)
        self.assertIn('equalizeHistoryTitleWidths(rows)', script)
        self.assertIn('bindHistoryResultsPan(rows)', script)
        self.assertIn('event.target.closest("button")', script)
        self.assertIn('item.copy_value', script)
        self.assertIn('searchCard.classList.add("is-wolfmax")', script)
        self.assertIn('sourceMark.textContent = "W"', script)
        self.assertIn('historyState = { day: "", search: "", pages: {} }', script)
        self.assertIn(".history-result-title-scroll", styles)
        self.assertIn(".history-results.is-dragging", styles)
        self.assertIn(".history-search.is-wolfmax", styles)
        self.assertIn(".history-source-mark", styles)
        self.assertIn("pointer-events: none", styles)
        self.assertIn(".history-page-button:disabled", styles)
        self.assertIn("cursor: default", styles)


if __name__ == "__main__":
    unittest.main()
