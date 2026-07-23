from pathlib import Path
import unittest
from unittest.mock import patch

from media_panel import server


class WatcherRulesProxyTests(unittest.TestCase):
    def test_watcher_rules_get_uses_orchestrator_settings_endpoint(self) -> None:
        expected = {
            "ok": True,
            "rules": {"ignored_suffixes": [".delay-audio-part"]},
        }
        with patch.object(server, "_upstream_json", return_value=expected) as upstream:
            result = server._watcher_rules_payload()

        self.assertEqual(result, expected)
        upstream.assert_called_once_with(f"{server.ORCH_URL}/settings/watcher")

    def test_watcher_rules_post_forwards_complete_payload(self) -> None:
        payload = {"rules": {"ignored_suffixes": [".personal"]}}
        expected = {"ok": True, "rules": payload["rules"], "saved": True}
        with patch.object(server, "_upstream_post_json", return_value=expected) as upstream:
            result = server._save_watcher_rules(payload)

        self.assertEqual(result, expected)
        upstream.assert_called_once_with(f"{server.ORCH_URL}/settings/watcher", payload)

    def test_save_pins_source_before_waiting_for_response(self) -> None:
        panel_js = (
            Path(server.__file__).resolve().parent
            / "web"
            / "static"
            / "js"
            / "panel.js"
        ).read_text(encoding="utf-8")

        self.assertIn("const savingSection = currentRuleSection;", panel_js)
        self.assertIn("const savingSource = RULE_SECTIONS[savingSection]?.source", panel_js)
        self.assertIn("const savedState = await api(savingEndpoint", panel_js)
        self.assertIn('if (savingSource === "watcher")', panel_js)

    def test_long_rule_status_wraps_on_mobile(self) -> None:
        panel_css = (
            Path(server.__file__).resolve().parent
            / "web"
            / "static"
            / "css"
            / "panel.css"
        ).read_text(encoding="utf-8")

        status_rule = panel_css.split(".status {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-wrap: anywhere;", status_rule)


if __name__ == "__main__":
    unittest.main()
