import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from media_panel import server, source_context_proxy
from media_panel.source_context_proxy import (
    MAX_REQUEST_BODY_BYTES,
    SourceContextProxy,
)


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
    def __init__(self, payload=None, token="secret-token") -> None:
        self.path = "/api/source-context/events"
        self.payload = payload or {"schema_version": 1}
        self.response = None
        self.headers = {
            "Content-Length": "2",
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        }

    def _json(self, status, payload) -> None:
        self.response = (status, payload)

    def _read_payload(self, **_kwargs):
        return self.payload

    def _content_length(self):
        return int(self.headers["Content-Length"])


class SourceContextProxyTests(unittest.TestCase):
    def test_proxy_forwards_to_internal_endpoint_with_bearer(self) -> None:
        proxy = SourceContextProxy("http://arr-orchestrator:8787/", "secret-token")
        payload = {"schema_version": 1, "source": "buscador-pro"}
        with patch.object(
            source_context_proxy.urllib.request,
            "urlopen",
            return_value=_Response(201, {"ok": True, "action": "created"}),
        ) as upstream:
            status, result = proxy.post_event(payload)

        self.assertEqual((status, result["action"]), (201, "created"))
        request = upstream.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://arr-orchestrator:8787/internal/source-context/events",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(json.loads(request.data.decode("utf-8")), payload)

    def test_proxy_transport_error_is_safe(self) -> None:
        proxy = SourceContextProxy("http://arr-orchestrator:8787", "secret-token")
        with patch.object(
            source_context_proxy.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("secret-token in transport"),
        ):
            status, payload = proxy.post_event({"schema_version": 1})

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "orchestrator_unavailable")
        self.assertNotIn("secret-token in transport", payload["message"])


class SourceContextPanelHandlerTests(unittest.TestCase):
    def test_public_endpoint_requires_configured_valid_bearer(self) -> None:
        for proxy, supplied, expected_status, expected_error in (
            (SourceContextProxy("http://orch", ""), "anything", 503, "source_context_disabled"),
            (SourceContextProxy("http://orch", "secret-token"), "wrong", 401, "unauthorized"),
            (SourceContextProxy("http://orch", "secret-token"), "", 401, "unauthorized"),
        ):
            with self.subTest(expected_error=expected_error), patch.object(
                server, "SOURCE_CONTEXT_PROXY", proxy
            ), patch.object(proxy, "post_event") as upstream:
                handler = _CapturedHandler(token=supplied)
                if not supplied:
                    handler.headers.pop("Authorization")
                server.Handler.do_POST(handler)
                upstream.assert_not_called()
                self.assertEqual(handler.response[0], expected_status)
                self.assertEqual(handler.response[1]["error"], expected_error)

    def test_public_endpoint_forwards_payload_and_exact_status(self) -> None:
        proxy = SourceContextProxy("http://orch", "secret-token")
        handler = _CapturedHandler(payload={"schema_version": 1})
        expected = {"ok": True, "action": "duplicate"}
        with patch.object(
            server, "SOURCE_CONTEXT_PROXY", proxy
        ), patch.object(proxy, "post_event", return_value=(200, expected)) as upstream:
            server.Handler.do_POST(handler)

        upstream.assert_called_once_with({"schema_version": 1})
        self.assertEqual(handler.response, (200, expected))

    def test_public_endpoint_rejects_non_json_and_source_specific_size_limit(self) -> None:
        proxy = SourceContextProxy("http://orch", "secret-token")
        for headers, expected_status, expected_error in (
            ({"Content-Type": "text/plain"}, 415, "unsupported_media_type"),
            (
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(MAX_REQUEST_BODY_BYTES + 1),
                },
                413,
                "payload_too_large",
            ),
        ):
            with self.subTest(expected_error=expected_error), patch.object(
                server, "SOURCE_CONTEXT_PROXY", proxy
            ), patch.object(proxy, "post_event") as upstream:
                handler = _CapturedHandler()
                handler.headers.update(headers)
                server.Handler.do_POST(handler)
                upstream.assert_not_called()
                self.assertEqual(handler.response[0], expected_status)
                self.assertEqual(handler.response[1]["error"], expected_error)

    def test_public_endpoint_rejects_invalid_json_without_forwarding(self) -> None:
        proxy = SourceContextProxy("http://orch", "secret-token")
        raw = b"{bad-json"
        handler = object.__new__(server.Handler)
        handler.path = "/api/source-context/events"
        handler.headers = {
            "Content-Length": str(len(raw)),
            "Content-Type": "application/json",
            "Authorization": "Bearer secret-token",
        }
        handler.rfile = io.BytesIO(raw)
        captured = []
        handler._json = lambda status, payload: captured.append((status, payload))
        with patch.object(
            server, "SOURCE_CONTEXT_PROXY", proxy
        ), patch.object(proxy, "post_event") as upstream:
            server.Handler.do_POST(handler)

        upstream.assert_not_called()
        self.assertEqual(captured[0][0], 400)
        self.assertEqual(captured[0][1]["error"], "invalid_json")


if __name__ == "__main__":
    unittest.main()
