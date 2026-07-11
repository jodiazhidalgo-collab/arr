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
            [{"title": "Pelicula 2026", "download_url": "https://example.invalid/file.torrent"}],
        )
        client = app_module.app.test_client()

        overview = client.get("/api/history/searches")
        page = client.get(f"/api/history/searches/{search_id}/results?page=1")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.get_json()["history"]["days"][0]["searches"][0]["result_count"], 1)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.get_json()["results"][0]["title"], "Pelicula 2026")

    def test_async_search_job_records_the_history_used_by_the_ui(self) -> None:
        rows = [{"title": "Batman", "download_url": "magnet:?xt=urn:btih:" + "a" * 40}]
        with (
            patch.object(app_module, "search_jackett_many", return_value=rows),
            patch.object(app_module, "cache_results"),
            patch.object(app_module.arr_trace, "start"),
            patch.object(app_module.arr_trace, "finish"),
        ):
            result = app_module.run_search_job("Batman", [], "auto", {}, "search-test")

        searches = app_module.search_history.overview()["days"][0]["searches"]
        self.assertEqual(result["count"], 1)
        self.assertEqual(searches[0]["query"], "Batman")
        self.assertEqual(searches[0]["result_count"], 1)


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
        self.assertIn('historyState = { day: "", search: "", pages: {} }', script)
        self.assertIn(".history-result-title-scroll", styles)
        self.assertIn(".history-results.is-dragging", styles)
        self.assertIn("pointer-events: none", styles)
        self.assertIn(".history-page-button:disabled", styles)
        self.assertIn("cursor: default", styles)


if __name__ == "__main__":
    unittest.main()
