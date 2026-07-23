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

    def _json(self, status, payload) -> None:
        self.response = (status, payload)

    def _read_payload(self):
        return self.payload


class FileBotRulesProxyTests(unittest.TestCase):
    def test_filebot_get_uses_orchestrator_settings_endpoint(self) -> None:
        expected = {"ok": True, "rules": {}, "revision": 4}
        with patch.object(server, "_proxy_upstream_json", return_value=(200, expected)) as upstream:
            result = server._filebot_rules_payload()

        self.assertEqual(result, (200, expected))
        upstream.assert_called_once_with(f"{server.ORCH_URL}/settings/filebot", timeout=8)

    def test_filebot_post_forwards_rules_and_expected_revision(self) -> None:
        payload = {
            "rules": {"movies": {"language": "es-ES"}},
            "expected_revision": 7,
        }
        expected = {"ok": True, "rules": payload["rules"], "revision": 8}
        with patch.object(server, "_proxy_upstream_json", return_value=(200, expected)) as upstream:
            result = server._save_filebot_rules(payload)

        self.assertEqual(result, (200, expected))
        upstream.assert_called_once_with(
            f"{server.ORCH_URL}/settings/filebot",
            payload,
            timeout=20,
        )

    def test_proxy_preserves_http_409_body_and_status(self) -> None:
        body = b'{"ok":false,"error":"revision_conflict","revision":9}'
        conflict = urllib.error.HTTPError(
            "http://arr-orchestrator:8787/settings/filebot",
            409,
            "Conflict",
            None,
            io.BytesIO(body),
        )
        with patch.object(server.urllib.request, "urlopen", side_effect=conflict):
            status, payload = server._proxy_upstream_json(
                "http://arr-orchestrator:8787/settings/filebot",
                {"rules": {}, "expected_revision": 8},
            )

        self.assertEqual(status, 409)
        self.assertEqual(
            payload,
            {"ok": False, "error": "revision_conflict", "revision": 9},
        )

    def test_get_endpoint_returns_exact_upstream_status_and_body(self) -> None:
        handler = _CapturedHandler("/api/filebot-rules")
        expected = {"ok": False, "error": "orchestrator_unavailable"}
        with patch.object(server, "_filebot_rules_payload", return_value=(503, expected)):
            server.Handler.do_GET(handler)

        self.assertEqual(handler.response, (503, expected))

    def test_post_endpoint_returns_conflict_without_rewriting_it(self) -> None:
        payload = {"rules": {}, "expected_revision": 3}
        handler = _CapturedHandler("/api/filebot-rules", payload)
        expected = {"ok": False, "error": "revision_conflict", "revision": 4}
        with patch.object(server, "_save_filebot_rules", return_value=(409, expected)) as save:
            server.Handler.do_POST(handler)

        save.assert_called_once_with(payload)
        self.assertEqual(handler.response, (409, expected))


class FileBotRulesStaticPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        web = Path(server.__file__).resolve().parent / "web"
        cls.panel_js = (web / "static" / "js" / "panel.js").read_text(encoding="utf-8")

    def test_panel_has_both_filebot_sections_and_safe_controls(self) -> None:
        for text in (
            'title: "FileBot Películas"',
            'title: "FileBot Series"',
            'path: "movies.language"',
            'path: "movies.region"',
            'path: "movies.query_aliases"',
            'path: "movies.forced_matches"',
            'path: "movies.filename_style"',
            'path: "tv.episode_order"',
        ):
            self.assertIn(text, self.panel_js)
        self.assertNotIn('path: "tv.region"', self.panel_js)

    def test_sources_use_a_map_and_load_in_parallel(self) -> None:
        self.assertIn("const RULE_SOURCES = {", self.panel_js)
        self.assertIn('endpoint: "/api/filebot-rules"', self.panel_js)
        self.assertIn("Promise.all(Object.entries(RULE_SOURCES)", self.panel_js)
        self.assertIn("rulesStates[currentRulesSource()]", self.panel_js)
        self.assertNotIn('savingSource === "watcher"', self.panel_js)

    def test_filebot_save_includes_expected_revision_and_updates_only_source(self) -> None:
        self.assertIn("payload.expected_revision = rulesStates[savingSource]?.revision;", self.panel_js)
        self.assertIn("rulesStates[savingSource] = savedState;", self.panel_js)
        self.assertIn("Revisión ${savedState.revision", self.panel_js)
        self.assertIn("documentState?.resolver_fingerprint", self.panel_js)
        self.assertIn("documentState?.rules_path", self.panel_js)

    def test_panel_keeps_main_and_rule_section_after_reload(self) -> None:
        self.assertIn('localStorage.getItem(RULE_SECTION_STORAGE_KEY)', self.panel_js)
        self.assertIn('localStorage.setItem(RULE_SECTION_STORAGE_KEY, section)', self.panel_js)
        self.assertIn('location.hash = view;', self.panel_js)
        self.assertIn('window.addEventListener("hashchange"', self.panel_js)
        self.assertIn('if (!RULE_SECTIONS[currentRuleSection]) currentRuleSection = "entrada";', self.panel_js)

    def test_safety_is_read_only_and_preview_never_calls_filebot(self) -> None:
        self.assertIn("Protecciones activas", self.panel_js)
        self.assertIn('class="readonly-value"', self.panel_js)
        self.assertIn("No ejecuta FileBot ni mueve archivos.", self.panel_js)
        self.assertIn("fileBotPreview(collectRules(), currentRuleSection)", self.panel_js)

    def test_list_controls_use_existing_textarea_pattern_without_trash_icons(self) -> None:
        self.assertIn("Una entrada por linea.", self.panel_js)
        self.assertNotIn("Papelera", self.panel_js)
        self.assertNotIn("trash", self.panel_js.lower())


if __name__ == "__main__":
    unittest.main()
