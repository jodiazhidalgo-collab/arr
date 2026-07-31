import io
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import patch

from media_panel import server


class _CapturedHandler:
    def __init__(self, path: str, payload=None) -> None:
        self.path = path
        self.payload = payload or {}
        self.response = None
        self.headers = {"Content-Length": "0"}

    def _send(self, status, body, content_type) -> None:
        self.response = (status, body, content_type)

    def _json(self, status, payload) -> None:
        self.response = (status, payload)

    def _read_payload(self):
        return self.payload

    def _content_length(self):
        return int(self.headers["Content-Length"])


class RulesProxyContractTests(unittest.TestCase):
    def test_proxy_preserves_http_error_status_and_body(self) -> None:
        body = b'{"ok":false,"error":"upstream_conflict","revision":9}'
        conflict = urllib.error.HTTPError(
            "http://upstream.invalid/settings/rules",
            409,
            "Conflict",
            None,
            io.BytesIO(body),
        )
        with patch.object(
            conflict, "close", wraps=conflict.close
        ) as close_error, patch.object(
            server.urllib.request, "urlopen", side_effect=conflict
        ):
            status, payload = server._proxy_upstream_json(
                "http://upstream.invalid/settings/rules",
                {"rules": {}, "expected_fingerprint": "old"},
            )

        self.assertEqual(status, 409)
        self.assertEqual(
            payload,
            {"ok": False, "error": "upstream_conflict", "revision": 9},
        )
        close_error.assert_called_once_with()

    def test_removed_rules_endpoint_returns_not_found(self) -> None:
        get_handler = _CapturedHandler("/api/filebot-rules")
        server.Handler.do_GET(get_handler)
        self.assertEqual(get_handler.response[0], 404)

        post_handler = _CapturedHandler(
            "/api/filebot-rules",
            {"rules": {}},
        )
        server.Handler.do_POST(post_handler)
        self.assertEqual(
            post_handler.response,
            (404, {"ok": False, "error": "Ruta no reconocida."}),
        )


class RulesPanelContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        web = Path(server.__file__).resolve().parent / "web"
        cls.panel_js = (web / "static" / "js" / "panel.js").read_text(
            encoding="utf-8"
        )
        cls.panel_css = (web / "static" / "css" / "panel.css").read_text(
            encoding="utf-8"
        )
        cls.server_py = Path(server.__file__).read_text(encoding="utf-8")

    def test_profile_sources_use_only_the_scoped_endpoints(self) -> None:
        self.assertIn("const RULE_VIEW_CONFIG = Object.freeze({", self.panel_js)
        for endpoint in (
            "/api/movie-rules",
            "/api/series-rules",
            "/api/trailer-rules",
            "/api/watcher-rules/movies",
            "/api/watcher-rules/tv",
        ):
            self.assertIn(f'endpoint: "{endpoint}"', self.panel_js)
        self.assertNotIn('endpoint: "/api/rules"', self.panel_js)
        self.assertIn(
            'const CLEANING_SECTIONS = Object.freeze(["entrada", "video", '
            '"audio", "subtitulos", "limpieza"]);',
            self.panel_js,
        )
        self.assertIn(
            'const SETTINGS_SECTIONS = Object.freeze(["trailers", "vigilantes"]);',
            self.panel_js,
        )

    def test_save_updates_only_the_selected_source(self) -> None:
        self.assertIn("state.documents[source] = savedState;", self.panel_js)
        self.assertIn("state.drafts[source]", self.panel_js)
        self.assertIn("state.dirty[source]", self.panel_js)
        self.assertIn("state.requestEpoch[source]", self.panel_js)
        self.assertIn(
            "expected_fingerprint: documentState.fingerprint ?? null",
            self.panel_js,
        )

    def test_hash_priority_persistence_and_legacy_aliases_are_explicit(self) -> None:
        self.assertIn("const PANEL_ROUTE_STORAGE_KEY", self.panel_js)
        self.assertIn("function exactCanonicalRoute(hash)", self.panel_js)
        self.assertIn("function canonicalRouteFromHash", self.panel_js)
        self.assertIn("const exact = exactCanonicalRoute(hash);", self.panel_js)
        self.assertIn("const storedHash = storageGet(", self.panel_js)
        self.assertIn("const stored = exactCanonicalRoute(storedHash);", self.panel_js)
        self.assertIn('history.replaceState(null, "", route.hash)', self.panel_js)
        self.assertIn("#identidad/comun/parser", self.panel_js)
        self.assertIn("#limpieza-peliculas/${section}", self.panel_js)
        self.assertIn("#ajustes/trailers", self.panel_js)
        self.assertIn("#ajustes/vigilante-peliculas", self.panel_js)
        self.assertIn("#ajustes/vigilante-series", self.panel_js)
        self.assertIn('/^#ajustes\\/vigilantes$/', self.panel_js)
        self.assertIn("/^#reglas", self.panel_js)
        self.assertIn("window.ArrIdentityUI.resolveTarget(hash)", self.panel_js)

    def test_series_is_complete_but_locked_to_its_own_contract(self) -> None:
        self.assertIn('endpoint: "/api/series-rules"', self.panel_js)
        self.assertIn("documentState.connected !== false", self.panel_js)
        self.assertIn("documentState.editable !== false", self.panel_js)
        self.assertIn('class="rules-editor-locked"', self.panel_js)
        self.assertIn("La configuración se muestra completa", self.panel_js)
        self.assertIn("config.sections.map", self.panel_js)

    def test_review_and_reports_are_profile_scoped(self) -> None:
        self.assertIn("/api/jobs?profile=${encodeURIComponent(profile)}", self.panel_js)
        self.assertIn("/api/review?profile=${encodeURIComponent(profile)}", self.panel_js)
        self.assertIn("/api/reports?profile=${encodeURIComponent(profile)}", self.panel_js)
        self.assertIn(
            "/api/codex-diagnostics?profile=${encodeURIComponent(profile)}",
            self.panel_js,
        )
        self.assertIn(
            "/api/report?profile=${encodeURIComponent(profile)}&file=${encodeURIComponent(file)}",
            self.panel_js,
        )
        self.assertIn("arr-media-panel-historial-profile", self.panel_js)
        self.assertIn("arr-media-panel-revision-profile", self.panel_js)
        self.assertIn("arr-media-panel-informes-profile", self.panel_js)

    def test_series_status_distinguishes_legacy_canary_and_active(self) -> None:
        self.assertIn("modo legacy · no enruta trabajos nuevos", self.panel_js)
        self.assertIn("modo canary · solo pruebas seleccionadas", self.panel_js)
        self.assertIn("activo para todos los trabajos nuevos", self.panel_js)
        self.assertIn('const runtimeStatus = await api("/api/status")', self.panel_js)

    def test_mobile_history_and_review_are_width_safe(self) -> None:
        self.assertIn('class="table jobs-table"', self.panel_js)
        for label in ("Nombre", "Categoría", "Estado", "Actualizado", "Diagnóstico"):
            self.assertIn(f'data-label="{label}"', self.panel_js)
        self.assertIn(".jobs-table td::before", self.panel_css)
        self.assertIn("content: attr(data-label);", self.panel_css)
        self.assertIn(".review-top > div", self.panel_css)
        self.assertIn("overflow-wrap: anywhere;", self.panel_css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", self.panel_css)
        self.assertIn(".report-row > b", self.panel_css)

    def test_removed_filebot_editor_and_proxy_are_not_exposed(self) -> None:
        for text in (
            'title: "FileBot Películas"',
            'title: "FileBot Series"',
            'endpoint: "/api/filebot-rules"',
            'source: "filebot"',
            "renderFileBotExtras",
            "fileBotPreview",
            "Protecciones activas",
            "payload.expected_revision",
        ):
            self.assertNotIn(text, self.panel_js)
        self.assertNotIn("/api/filebot-rules", self.server_py)
        self.assertNotIn("def _filebot_rules_payload(", self.server_py)
        self.assertNotIn("def _save_filebot_rules(", self.server_py)
        self.assertNotIn(".readonly-value", self.panel_css)

    def test_list_controls_use_textarea_without_trash_icons(self) -> None:
        self.assertIn("Una entrada por línea.", self.panel_js)
        self.assertNotIn("Papelera", self.panel_js)
        self.assertNotIn("trash", self.panel_js.lower())


if __name__ == "__main__":
    unittest.main()
