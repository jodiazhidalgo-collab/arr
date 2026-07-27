from pathlib import Path
import unittest
from unittest.mock import patch

from media_panel import server


class _CapturedHandler:
    def __init__(self, path: str, payload=None) -> None:
        self.path = path
        self.payload = payload or {}
        self.response = None
        self.headers = {"Content-Length": "0"}

    def _json(self, status, payload) -> None:
        self.response = (status, payload)

    def _read_payload(self):
        return self.payload

    def _content_length(self):
        return int(self.headers["Content-Length"])


class MediaRulesProxyTests(unittest.TestCase):
    def test_get_and_post_use_media_worker_settings_endpoint(self) -> None:
        payload = {"rules": {}, "expected_fingerprint": "abc"}
        expected = {"ok": True, "fingerprint": "def", "applied": True}
        with patch.object(server, "_proxy_upstream_json", return_value=(200, expected)) as upstream:
            self.assertEqual(server._media_rules_payload(), (200, expected))
            upstream.assert_called_once_with(f"{server.WORKER_URL}/settings/rules", timeout=8)
        with patch.object(server, "_proxy_upstream_json", return_value=(200, expected)) as upstream:
            self.assertEqual(server._save_media_rules(payload), (200, expected))
            upstream.assert_called_once_with(
                f"{server.WORKER_URL}/settings/rules", payload, timeout=20
            )

    def test_api_preserves_worker_status_and_body(self) -> None:
        conflict = {"ok": False, "error": "fingerprint_conflict"}
        get_handler = _CapturedHandler("/api/rules")
        with patch.object(server, "_media_rules_payload", return_value=(503, conflict)):
            server.Handler.do_GET(get_handler)
        self.assertEqual(get_handler.response, (503, conflict))

        payload = {"rules": {}, "expected_fingerprint": "old"}
        post_handler = _CapturedHandler("/api/rules", payload)
        with patch.object(server, "_save_media_rules", return_value=(409, conflict)) as save:
            server.Handler.do_POST(post_handler)
        save.assert_called_once_with(payload)
        self.assertEqual(post_handler.response, (409, conflict))


class MediaRulesPanelContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        web = Path(server.__file__).resolve().parent / "web"
        cls.panel_js = (web / "static" / "js" / "panel.js").read_text(encoding="utf-8")
        cls.identity_js = (
            web / "static" / "js" / "limpieza-arr" / "view.js"
        ).read_text(encoding="utf-8")
        cls.server_py = Path(server.__file__).read_text(encoding="utf-8")

    def test_media_save_sends_fingerprint_and_requires_activation(self) -> None:
        self.assertIn(
            "payload.expected_fingerprint = rulesStates[savingSource]?.fingerprint;",
            self.panel_js,
        )
        self.assertIn('savedState.applied !== true', self.panel_js)
        self.assertIn(
            "Reglas guardadas y activas para trabajos nuevos.", self.panel_js
        )
        self.assertIn(
            "Reglas guardadas y activas para nuevas detecciones.", self.panel_js
        )
        self.assertIn(
            'path === "video.idiomas_indeterminados_como_es" && index === 0',
            self.panel_js,
        )

    def test_panel_no_longer_writes_media_rules_directly(self) -> None:
        self.assertNotIn("def _save_rules(", self.server_py)
        self.assertNotIn("tmp.replace(RULES_PATH)", self.server_py)
        self.assertIn("def _save_media_rules(", self.server_py)

    def test_identity_confirms_activation_for_new_jobs(self) -> None:
        self.assertIn(
            "Configuración guardada, versionada y activa para trabajos nuevos.",
            self.identity_js,
        )


if __name__ == "__main__":
    unittest.main()
