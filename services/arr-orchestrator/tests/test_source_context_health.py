import http.client
import json
import unittest
import urllib.error
import urllib.request

from arr_orchestrator.health import start_health_server
from arr_orchestrator.source_context.contract import MAX_REQUEST_BODY_BYTES


def _post_raw(
    url: str,
    data: bytes,
    *,
    token: str = "",
    content_type: str = "application/json",
):
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode("utf-8"))
        finally:
            error.close()


class SourceContextHealthTests(unittest.TestCase):
    def _server(self, handler, token="secret-token"):
        return start_health_server(
            0,
            lambda: {"status": "ok"},
            lambda: [],
            source_context_event_handler=handler,
            source_context_token=token,
        )

    def test_internal_endpoint_requires_bearer_and_forwards_valid_json(self) -> None:
        calls = []

        def handler(payload):
            calls.append(payload)
            return 201, {"ok": True, "action": "created"}

        server = self._server(handler)
        try:
            url = (
                f"http://127.0.0.1:{server.server_address[1]}"
                "/internal/source-context/events"
            )
            self.assertEqual(_post_raw(url, b"{}", token="wrong")[0], 401)
            self.assertEqual(_post_raw(url, b"{}")[0], 401)
            status, payload = _post_raw(
                url,
                json.dumps({"schema_version": 1}).encode("utf-8"),
                token="secret-token",
            )
            self.assertEqual(status, 201)
            self.assertEqual(payload["action"], "created")
            self.assertEqual(calls, [{"schema_version": 1}])
        finally:
            server.shutdown()
            server.server_close()

    def test_missing_server_token_disables_endpoint(self) -> None:
        server = self._server(lambda payload: (200, payload), token="")
        try:
            url = (
                f"http://127.0.0.1:{server.server_address[1]}"
                "/internal/source-context/events"
            )
            status, payload = _post_raw(url, b"{}", token="anything")
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"], "source_context_disabled")
        finally:
            server.shutdown()
            server.server_close()

    def test_internal_endpoint_rejects_media_type_invalid_json_and_large_body(self) -> None:
        calls = []
        server = self._server(lambda payload: (calls.append(payload) or (200, {"ok": True})))
        try:
            url = (
                f"http://127.0.0.1:{server.server_address[1]}"
                "/internal/source-context/events"
            )
            status, payload = _post_raw(
                url, b"{}", token="secret-token", content_type="text/plain"
            )
            self.assertEqual((status, payload["error"]), (415, "unsupported_media_type"))

            status, payload = _post_raw(
                url, b"{bad-json", token="secret-token"
            )
            self.assertEqual((status, payload["error"]), (400, "invalid_json"))

            status, payload = _post_raw(
                url, b"x" * (MAX_REQUEST_BODY_BYTES + 1), token="secret-token"
            )
            self.assertEqual((status, payload["error"]), (413, "payload_too_large"))
            self.assertEqual(calls, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_handler_failure_returns_generic_error_without_details(self) -> None:
        def handler(_payload):
            raise RuntimeError("token-super-secreto")

        server = self._server(handler)
        try:
            url = (
                f"http://127.0.0.1:{server.server_address[1]}"
                "/internal/source-context/events"
            )
            status, payload = _post_raw(url, b"{}", token="secret-token")
            self.assertEqual(status, 500)
            self.assertEqual(payload["error"], "source_context_failed")
            self.assertNotIn("token-super-secreto", json.dumps(payload))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
