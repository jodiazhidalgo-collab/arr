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

    def test_sources_use_a_map_and_load_in_parallel(self) -> None:
        self.assertIn("const RULE_SOURCES = {", self.panel_js)
        self.assertIn('endpoint: "/api/rules"', self.panel_js)
        self.assertIn('endpoint: "/api/watcher-rules"', self.panel_js)
        self.assertIn(
            "Promise.all(Object.entries(RULE_SOURCES)", self.panel_js
        )
        self.assertIn("rulesStates[currentRulesSource()]", self.panel_js)
        self.assertNotIn('savingSource === "watcher"', self.panel_js)

    def test_save_updates_only_the_selected_source(self) -> None:
        self.assertIn(
            "rulesStates[savingSource] = savedState;", self.panel_js
        )
        self.assertIn(
            "payload.expected_fingerprint = "
            "rulesStates[savingSource]?.fingerprint;",
            self.panel_js,
        )

    def test_panel_keeps_view_and_persists_invalid_section_as_entrada(self) -> None:
        self.assertIn(
            "localStorage.getItem(RULE_SECTION_STORAGE_KEY)", self.panel_js
        )
        self.assertIn(
            "localStorage.setItem(RULE_SECTION_STORAGE_KEY, section)",
            self.panel_js,
        )
        self.assertIn(
            'const target = view === "limpieza-arr" '
            '? "limpieza-arr/parser" : view;',
            self.panel_js,
        )
        self.assertIn("else location.hash = target;", self.panel_js)
        self.assertIn("function routeFromHash()", self.panel_js)
        self.assertIn('window.addEventListener("hashchange"', self.panel_js)
        self.assertIn(
            'if (!RULE_SECTIONS[currentRuleSection]) {\n'
            '  currentRuleSection = "entrada";\n'
            "  storeRuleSection(currentRuleSection);\n"
            "}",
            self.panel_js,
        )

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
        self.assertIn("Una entrada por linea.", self.panel_js)
        self.assertNotIn("Papelera", self.panel_js)
        self.assertNotIn("trash", self.panel_js.lower())


if __name__ == "__main__":
    unittest.main()
