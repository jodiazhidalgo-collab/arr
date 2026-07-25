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
        self.headers = {"Content-Length": "0"}

    def _json(self, status, payload) -> None:
        self.response = (status, payload)

    def _read_payload(self):
        return self.payload

    def _content_length(self):
        return int(self.headers["Content-Length"])


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
            identity_proxy.urllib.request, "urlopen", side_effect=conflict
        ):
            status, payload = self.proxy.save_rules(
                {"rules": {}, "expected_revision": 4}
            )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "revision_conflict")
        self.assertEqual(payload["current_revision"], 5)

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


class IdentityPanelHandlerTests(unittest.TestCase):
    def test_get_returns_exact_upstream_status(self) -> None:
        handler = _CapturedHandler("/api/identity-rules")
        expected = {"ok": False, "error": "orchestrator_unavailable"}
        with patch.object(
            server.IDENTITY_PROXY, "get_rules", return_value=(502, expected)
        ):
            server.Handler.do_GET(handler)

        self.assertEqual(handler.response, (502, expected))

    def test_all_identity_post_actions_forward_payload_and_status(self) -> None:
        actions = {
            "/api/identity-rules": "save_rules",
            "/api/identity-rules/reset": "reset_rules",
            "/api/identity-rules/cache/clear": "clear_cache",
            "/api/identity-rules/test-parser": "test_parser",
            "/api/identity-rules/test-resolver": "test_resolver",
        }
        payload = {"name": "Blade.Runner.1982.1080p", "category": "movies"}
        for path, method_name in actions.items():
            with self.subTest(path=path):
                handler = _CapturedHandler(path, payload)
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


if __name__ == "__main__":
    unittest.main()
