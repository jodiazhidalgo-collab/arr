import io
from pathlib import Path
import unittest
from unittest.mock import patch

from media_panel import server


class _CapturedHandler:
    def __init__(self, path: str, payload=None) -> None:
        self.path = path
        self.payload = payload or {}
        self.response = None
        self.headers = {
            "Content-Length": "0",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _json(self, status, payload) -> None:
        self.response = (status, payload)

    def _read_payload(self, **_kwargs):
        return self.payload

    def _content_length(self):
        return int(self.headers["Content-Length"])


class WatcherRulesProxyTests(unittest.TestCase):
    def test_watcher_rules_get_uses_orchestrator_settings_endpoint(self) -> None:
        expected = {
            "ok": True,
            "rules": {"ignored_suffixes": [".delay-audio-part"]},
        }
        with patch.object(
            server,
            "_proxy_upstream_json",
            return_value=(200, expected),
        ) as upstream:
            result = server._watcher_rules_payload()

        self.assertEqual(result, (200, expected))
        upstream.assert_called_once_with(
            f"{server.ORCH_URL}/settings/watcher",
            timeout=8,
        )

    def test_watcher_rules_post_forwards_complete_payload(self) -> None:
        payload = {"rules": {"ignored_suffixes": [".personal"]}}
        expected = {"ok": True, "rules": payload["rules"], "saved": True}
        with patch.object(
            server,
            "_proxy_upstream_json",
            return_value=(200, expected),
        ) as upstream:
            result = server._save_watcher_rules(payload)

        self.assertEqual(result, (200, expected))
        upstream.assert_called_once_with(
            f"{server.ORCH_URL}/settings/watcher",
            payload,
            timeout=20,
        )

    def test_movies_profile_is_alias_of_current_watcher(self) -> None:
        current = {
            "ok": True,
            "rules": {"ignored_suffixes": [".delay-audio-part"]},
        }
        with patch.object(
            server,
            "_watcher_rules_payload",
            return_value=(200, current),
        ) as legacy:
            status, result = server._watcher_rules_profile_payload("movies")

        legacy.assert_called_once_with()
        self.assertEqual(status, 200)
        self.assertEqual(result["rules"], current["rules"])
        self.assertEqual(result["profile"], "movies")
        self.assertTrue(result["connected"])
        self.assertTrue(result["editable"])

    def test_movies_profile_preserves_upstream_conflicts_and_failures(self) -> None:
        cases = (
            (409, "revision_conflict"),
            (503, "orchestrator_unavailable"),
            (500, "internal_error"),
        )
        for status, error in cases:
            with self.subTest(status=status), patch.object(
                server,
                "_save_watcher_rules",
                return_value=(status, {"ok": False, "error": error}),
            ):
                actual_status, result = server._save_watcher_rules_profile(
                    "movies",
                    {"rules": {"ignored_suffixes": []}},
                )

            self.assertEqual(actual_status, status)
            self.assertEqual(result["error"], error)
            self.assertEqual(result["connected"], status < 500)

    def test_watcher_get_routes_preserve_upstream_status(self) -> None:
        unavailable = {"ok": False, "error": "orchestrator_unavailable"}
        profile_handler = _CapturedHandler("/api/watcher-rules/movies")
        legacy_handler = _CapturedHandler("/api/watcher-rules")
        with patch.object(
            server,
            "_watcher_rules_payload",
            return_value=(503, unavailable),
        ):
            status, profile = server._watcher_rules_profile_payload("movies")
            server.Handler.do_GET(legacy_handler)

        self.assertEqual(status, 503)
        self.assertEqual(profile["error"], "orchestrator_unavailable")
        self.assertFalse(profile["connected"])
        self.assertEqual(legacy_handler.response, (503, unavailable))

        expected_profile = dict(profile)
        with patch.object(
            server,
            "_watcher_rules_profile_payload",
            return_value=(503, expected_profile),
        ):
            server.Handler.do_GET(profile_handler)
        self.assertEqual(profile_handler.response, (503, expected_profile))

    def test_legacy_watcher_route_also_preserves_upstream_status(self) -> None:
        conflict = {"ok": False, "error": "revision_conflict"}
        handler = _CapturedHandler(
            "/api/watcher-rules",
            {"rules": {"ignored_suffixes": []}},
        )
        with patch.object(
            server,
            "_save_watcher_rules",
            return_value=(409, conflict),
        ):
            server.Handler.do_POST(handler)

        self.assertEqual(handler.response, (409, conflict))

    def test_tv_profile_is_honestly_disconnected_without_upstream(self) -> None:
        with patch.object(server, "_watcher_rules_payload") as read, patch.object(
            server,
            "_save_watcher_rules",
        ) as save:
            get_status, get_result = server._watcher_rules_profile_payload("tv")
            post_status, post_result = server._save_watcher_rules_profile(
                "tv",
                {"rules": {"ignored_suffixes": [".part"]}},
            )

        read.assert_not_called()
        save.assert_not_called()
        self.assertEqual(get_status, 200)
        self.assertFalse(get_result["connected"])
        self.assertFalse(get_result["editable"])
        self.assertEqual(post_status, 503)
        self.assertEqual(post_result["error"], "series_watcher_not_connected")

    def test_profile_routes_keep_legacy_and_tv_blocked(self) -> None:
        get_handler = _CapturedHandler("/api/watcher-rules/movies")
        expected = {"ok": True, "profile": "movies"}
        with patch.object(
            server,
            "_watcher_rules_profile_payload",
            return_value=(200, expected),
        ) as profile_get:
            server.Handler.do_GET(get_handler)
        profile_get.assert_called_once_with("movies")
        self.assertEqual(get_handler.response, (200, expected))

        post_handler = _CapturedHandler("/api/watcher-rules/tv", {"rules": {}})
        blocked = {
            "ok": False,
            "error": "series_watcher_not_connected",
        }
        with patch.object(
            server,
            "_save_watcher_rules_profile",
            return_value=(503, blocked),
        ) as profile_save:
            server.Handler.do_POST(post_handler)
        profile_save.assert_called_once_with("tv", {"rules": {}})
        self.assertEqual(post_handler.response, (503, blocked))

    def test_new_rules_posts_require_same_origin_and_json_content_type(self) -> None:
        paths = (
            "/api/movie-rules",
            "/api/trailer-rules",
            "/api/series-rules",
            "/api/watcher-rules/movies",
            "/api/watcher-rules/tv",
        )
        cases = (
            (
                {
                    "Content-Type": "application/json",
                    "Host": "arr.local",
                    "Origin": "http://evil.local",
                },
                403,
                "cross_origin_request",
            ),
            (
                {"Content-Type": "text/plain", "Host": "arr.local"},
                415,
                "unsupported_media_type",
            ),
        )
        for path in paths:
            for headers, expected_status, expected_error in cases:
                with self.subTest(path=path, expected_status=expected_status):
                    handler = _CapturedHandler(path, {"rules": {}})
                    handler.headers.update(headers)
                    with patch.object(
                        server,
                        "_save_media_rules_profile",
                    ) as media_save, patch.object(
                        server,
                        "_save_watcher_rules_profile",
                    ) as watcher_save:
                        server.Handler.do_POST(handler)

                    media_save.assert_not_called()
                    watcher_save.assert_not_called()
                    self.assertEqual(handler.response[0], expected_status)
                    self.assertEqual(handler.response[1]["error"], expected_error)

    def test_new_rules_posts_require_a_valid_json_object(self) -> None:
        for path in (
            "/api/movie-rules",
            "/api/trailer-rules",
            "/api/watcher-rules/movies",
            "/api/watcher-rules/tv",
        ):
            for raw in (b"{not-json", b"[]", b'"text"', b""):
                with self.subTest(path=path, raw=raw):
                    handler = object.__new__(server.Handler)
                    handler.path = path
                    handler.headers = {
                        "Content-Length": str(len(raw)),
                        "Content-Type": "application/json",
                        "Host": "arr.local",
                        "Origin": "http://arr.local",
                    }
                    handler.rfile = io.BytesIO(raw)
                    captured = []
                    handler._json = lambda status, payload: captured.append(
                        (status, payload)
                    )
                    with patch.object(
                        server,
                        "_save_media_rules_profile",
                    ) as media_save, patch.object(
                        server,
                        "_save_watcher_rules_profile",
                    ) as watcher_save:
                        server.Handler.do_POST(handler)

                    media_save.assert_not_called()
                    watcher_save.assert_not_called()
                    self.assertEqual(captured[0][0], 400)
                    self.assertEqual(captured[0][1]["error"], "invalid_json")

    def test_save_pins_profile_source_before_waiting_for_response(self) -> None:
        panel_js = (
            Path(server.__file__).resolve().parent
            / "web"
            / "static"
            / "js"
            / "panel.js"
        ).read_text(encoding="utf-8")

        self.assertIn("async function saveRuleSource(view, source)", panel_js)
        self.assertIn("const documentState = state.documents[source];", panel_js)
        self.assertIn("state.documents[source] = savedState;", panel_js)
        self.assertIn("if (isRuleSourceActive(view, source))", panel_js)

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
