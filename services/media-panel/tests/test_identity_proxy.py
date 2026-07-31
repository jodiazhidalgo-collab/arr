import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from media_panel import identity_proxy, server
from media_panel.identity_proxy import IdentityProxy


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._body


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


IDENTITY_POST_ACTIONS = {
    "/api/identity-rules": "save_rules",
    "/api/identity-rules/reset": "reset_rules",
    "/api/identity-rules/cache/clear": "clear_cache",
    "/api/identity-rules/test-parser": "test_parser",
    "/api/identity-rules/test-resolver": "test_resolver",
}


class IdentityProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proxy = IdentityProxy("http://arr-orchestrator:8787/")

    def test_get_rules_uses_identity_settings_endpoint(self) -> None:
        expected = {"ok": True, "revision": 3, "rules": {}}
        with patch.object(
            identity_proxy.urllib.request,
            "urlopen",
            return_value=_Response(200, expected),
        ) as upstream:
            result = self.proxy.get_rules()

        self.assertEqual(result, (200, expected))
        request = upstream.call_args.args[0]
        self.assertEqual(request.full_url, "http://arr-orchestrator:8787/settings/identity")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(upstream.call_args.kwargs["timeout"], 10)

    def test_save_forwards_complete_draft_and_revision(self) -> None:
        draft = {
            "rules": {"schema_version": 1, "parser": {}, "resolver": {}},
            "expected_revision": 8,
        }
        expected = {"ok": True, "revision": 9, "rules": draft["rules"]}
        with patch.object(
            identity_proxy.urllib.request,
            "urlopen",
            return_value=_Response(200, expected),
        ) as upstream:
            result = self.proxy.save_rules(draft)

        self.assertEqual(result, (200, expected))
        request = upstream.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data.decode("utf-8")), draft)
        self.assertEqual(upstream.call_args.kwargs["timeout"], 25)

    def test_profile_endpoints_keep_each_identity_document_isolated(self) -> None:
        payload = {"rules": {"schema_version": 1}, "expected_revision": 2}
        cases = (
            ("common", "save_rules", "", "/settings/identity/common"),
            ("movies", "reset_rules", "reset", "/settings/identity/movies/reset"),
            ("tv", "clear_cache", "cache", "/settings/identity/tv/cache/clear"),
            ("movies", "test_parser", "parser", "/settings/identity/movies/test-parser"),
            ("tv", "test_resolver", "resolver", "/settings/identity/tv/test-resolver"),
        )
        for profile, method_name, _label, expected_path in cases:
            with self.subTest(profile=profile, method=method_name), patch.object(
                identity_proxy.urllib.request,
                "urlopen",
                return_value=_Response(200, {"ok": True, "profile": profile}),
            ) as upstream:
                method = getattr(self.proxy, method_name)
                result = method(payload, profile)

            self.assertEqual(result[1]["profile"], profile)
            request = upstream.call_args.args[0]
            self.assertEqual(request.full_url, f"http://arr-orchestrator:8787{expected_path}")
            self.assertEqual(request.get_method(), "POST")

    def test_profile_get_uses_scoped_upstream_path(self) -> None:
        with patch.object(
            identity_proxy.urllib.request,
            "urlopen",
            return_value=_Response(200, {"ok": True, "profile": "tv"}),
        ) as upstream:
            result = self.proxy.get_rules("tv")

        self.assertEqual(result, (200, {"ok": True, "profile": "tv"}))
        self.assertEqual(
            upstream.call_args.args[0].full_url,
            "http://arr-orchestrator:8787/settings/identity/tv",
        )

    def test_unknown_profile_is_rejected_before_network(self) -> None:
        with patch.object(identity_proxy.urllib.request, "urlopen") as upstream:
            with self.assertRaises(ValueError):
                self.proxy.get_rules("other")
        upstream.assert_not_called()

    def test_http_conflict_preserves_status_and_body(self) -> None:
        body = b'{"ok":false,"error":"revision_conflict","current_revision":5}'
        conflict = urllib.error.HTTPError(
            "http://arr-orchestrator:8787/settings/identity",
            409,
            "Conflict",
            None,
            io.BytesIO(body),
        )
        with patch.object(
            conflict, "close", wraps=conflict.close
        ) as close_error, patch.object(
            identity_proxy.urllib.request, "urlopen", side_effect=conflict
        ):
            status, payload = self.proxy.save_rules(
                {"rules": {}, "expected_revision": 4}
            )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "revision_conflict")
        self.assertEqual(payload["current_revision"], 5)
        close_error.assert_called_once_with()

    def test_transport_error_is_safe_and_does_not_leak_url(self) -> None:
        with patch.object(
            identity_proxy.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("secret upstream details"),
        ):
            status, payload = self.proxy.test_resolver({"name": "Alien"})

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "orchestrator_unavailable")
        self.assertNotIn("secret upstream details", payload["message"])

    def test_resolver_timeout_follows_draft_budget_with_margin_and_cap(self) -> None:
        cases = (
            ({"resolver": {"http": {"total_budget_ms": 5_000}}}, 10.0),
            ({"resolver": {"http": {"total_budget_ms": 120_000}}}, 125.0),
            ({"resolver": {"http": {"total_budget_ms": 300_000}}}, 305.0),
            ({"resolver": {"http": {"total_budget_ms": 999_999}}}, 305.0),
            ({"resolver": {"http": {"total_budget_ms": "invalid"}}}, 305.0),
            ({"resolver": {"http": {}}}, 305.0),
            ({}, 305.0),
            (None, 305.0),
        )
        for rules, expected_timeout in cases:
            with self.subTest(rules=rules), patch.object(
                identity_proxy.urllib.request,
                "urlopen",
                return_value=_Response(200, {"ok": True}),
            ) as upstream:
                payload: dict[str, object] = {"name": "Alien"}
                if rules is not None:
                    payload["rules"] = rules
                self.proxy.test_resolver(payload)
                self.assertEqual(
                    upstream.call_args.kwargs["timeout"], expected_timeout
                )


class IdentityPanelHandlerTests(unittest.TestCase):
    def test_get_returns_exact_upstream_status(self) -> None:
        handler = _CapturedHandler("/api/identity-rules")
        expected = {"ok": False, "error": "orchestrator_unavailable"}
        with patch.object(
            server.IDENTITY_PROXY, "get_rules", return_value=(502, expected)
        ):
            server.Handler.do_GET(handler)

        self.assertEqual(handler.response, (502, expected))

    def test_profile_get_forwards_profile_and_exact_status(self) -> None:
        for profile in ("common", "movies", "tv"):
            with self.subTest(profile=profile):
                handler = _CapturedHandler(f"/api/identity-rules/{profile}")
                expected = {"ok": True, "profile": profile, "revision": 1}
                with patch.object(
                    server.IDENTITY_PROXY,
                    "get_rules",
                    return_value=(200, expected),
                ) as get_rules:
                    server.Handler.do_GET(handler)

                get_rules.assert_called_once_with(profile)
                self.assertEqual(handler.response, (200, expected))

    def test_profile_save_and_reset_forward_profile(self) -> None:
        payload = {"expected_revision": 1}
        for suffix, method_name in (("", "save_rules"), ("/reset", "reset_rules")):
            with self.subTest(suffix=suffix):
                handler = _CapturedHandler(f"/api/identity-rules/movies{suffix}", payload)
                expected = {"ok": True, "profile": "movies", "revision": 2}
                with patch.object(
                    server.IDENTITY_PROXY,
                    method_name,
                    return_value=(200, expected),
                ) as action:
                    server.Handler.do_POST(handler)

                action.assert_called_once_with(payload, "movies")
                self.assertEqual(handler.response, (200, expected))

    def test_profile_cache_and_test_actions_forward_profile(self) -> None:
        payload = {"name": "Dark.S01E01", "category": "tv"}
        actions = (
            ("/cache/clear", "clear_cache"),
            ("/test-parser", "test_parser"),
            ("/test-resolver", "test_resolver"),
        )
        for suffix, method_name in actions:
            with self.subTest(suffix=suffix):
                handler = _CapturedHandler(f"/api/identity-rules/tv{suffix}", payload)
                expected = {"ok": True, "profile": "tv"}
                with patch.object(
                    server.IDENTITY_PROXY,
                    method_name,
                    return_value=(200, expected),
                ) as action:
                    server.Handler.do_POST(handler)

                action.assert_called_once_with(payload, "tv")
                self.assertEqual(handler.response, (200, expected))

    def test_all_identity_post_actions_forward_payload_and_status(self) -> None:
        payload = {"name": "Blade.Runner.1982.1080p", "category": "movies"}
        for path, method_name in IDENTITY_POST_ACTIONS.items():
            with self.subTest(path=path):
                handler = _CapturedHandler(path, payload)
                handler.headers.update(
                    {
                        "Host": "arr.local",
                        "Origin": "http://arr.local",
                        "Sec-Fetch-Site": "same-origin",
                    }
                )
                expected = {"ok": False, "error": "invalid_rules"}
                with patch.object(
                    server.IDENTITY_PROXY,
                    method_name,
                    return_value=(400, expected),
                ) as action:
                    server.Handler.do_POST(handler)
                action.assert_called_once_with(payload)
                self.assertEqual(handler.response, (400, expected))

    def test_oversized_identity_payload_returns_413_without_forwarding(self) -> None:
        handler = object.__new__(server.Handler)
        handler.path = "/api/identity-rules"
        handler.headers = {
            "Content-Length": str(server.MAX_REQUEST_BODY_BYTES + 1)
        }
        handler.rfile = io.BytesIO()
        captured = []
        handler._json = lambda status, payload: captured.append((status, payload))

        with patch.object(server.IDENTITY_PROXY, "save_rules") as action:
            server.Handler.do_POST(handler)

        action.assert_not_called()
        self.assertEqual(captured[0][0], 413)
        self.assertEqual(captured[0][1]["error"], "payload_too_large")

    def test_exact_identity_payload_limit_is_read_and_forwarded_complete(self) -> None:
        prefix = b'{"blob":"'
        suffix = b'"}'
        raw = prefix + (
            b"x" * (server.MAX_REQUEST_BODY_BYTES - len(prefix) - len(suffix))
        ) + suffix
        handler = object.__new__(server.Handler)
        handler.path = "/api/identity-rules/test-parser"
        handler.headers = {"Content-Length": str(len(raw))}
        handler.headers["Content-Type"] = "application/json"
        handler.rfile = io.BytesIO(raw)
        captured = []
        handler._json = lambda status, payload: captured.append((status, payload))

        with patch.object(
            server.IDENTITY_PROXY,
            "test_parser",
            return_value=(200, {"ok": True}),
        ) as action:
            server.Handler.do_POST(handler)

        forwarded = action.call_args.args[0]
        self.assertEqual(len(forwarded["blob"]), len(raw) - len(prefix) - len(suffix))
        self.assertEqual(captured, [(200, {"ok": True})])

    def test_invalid_content_length_returns_400_without_reading_or_forwarding(self) -> None:
        for raw_length in ("not-a-number", "-1"):
            with self.subTest(content_length=raw_length):
                handler = object.__new__(server.Handler)
                handler.path = "/api/identity-rules"
                handler.headers = {"Content-Length": raw_length}
                handler.rfile = io.BytesIO(b'{"rules":{}}')
                captured = []
                handler._json = lambda status, payload: captured.append(
                    (status, payload)
                )

                with patch.object(server.IDENTITY_PROXY, "save_rules") as action:
                    server.Handler.do_POST(handler)

                action.assert_not_called()
                self.assertEqual(captured[0][0], 400)
                self.assertEqual(captured[0][1]["error"], "invalid_request")

    def test_all_identity_posts_reject_non_json_and_cross_origin_requests(self) -> None:
        cases = (
            (
                {"Content-Type": "text/plain", "Host": "arr.local"},
                415,
                "unsupported_media_type",
            ),
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
                {
                    "Content-Type": "application/json",
                    "Host": "arr.local",
                    "Sec-Fetch-Site": "cross-site",
                },
                403,
                "cross_origin_request",
            ),
        )
        for path, method_name in IDENTITY_POST_ACTIONS.items():
            for headers, expected_status, expected_error in cases:
                with self.subTest(path=path, headers=headers):
                    handler = _CapturedHandler(path)
                    handler.headers.update(headers)
                    with patch.object(server.IDENTITY_PROXY, method_name) as action:
                        server.Handler.do_POST(handler)
                    action.assert_not_called()
                    self.assertEqual(handler.response[0], expected_status)
                    self.assertEqual(handler.response[1]["error"], expected_error)

    def test_all_identity_posts_accept_same_origin_and_api_json_clients(self) -> None:
        payload = {"confirmed": True}
        expected = {"ok": True}
        for path, method_name in IDENTITY_POST_ACTIONS.items():
            for client_headers in (
                {
                    "Host": "arr.local",
                    "Origin": "http://arr.local",
                    "Sec-Fetch-Site": "same-origin",
                },
                {},
            ):
                with self.subTest(path=path, client_headers=client_headers):
                    handler = _CapturedHandler(path, payload)
                    handler.headers.update(client_headers)
                    with patch.object(
                        server.IDENTITY_PROXY,
                        method_name,
                        return_value=(200, expected),
                    ) as action:
                        server.Handler.do_POST(handler)

                    action.assert_called_once_with(payload)
                    self.assertEqual(handler.response, (200, expected))

    def test_all_identity_posts_require_a_valid_json_object(self) -> None:
        for path, method_name in IDENTITY_POST_ACTIONS.items():
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

                    with patch.object(server.IDENTITY_PROXY, method_name) as action:
                        server.Handler.do_POST(handler)

                    action.assert_not_called()
                    self.assertEqual(captured[0][0], 400)
                    self.assertEqual(captured[0][1]["error"], "invalid_json")


if __name__ == "__main__":
    unittest.main()
