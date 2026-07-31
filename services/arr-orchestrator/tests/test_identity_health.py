import http.client
import json
import unittest
import urllib.error
import urllib.request

from arr_orchestrator.health import MAX_REQUEST_BODY_BYTES, start_health_server


def _post(url: str, payload: dict):
    return _post_raw(url, json.dumps(payload).encode("utf-8"))


def _post_raw(url: str, data: bytes):
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


class IdentityHealthTests(unittest.TestCase):
    def test_removed_filebot_settings_endpoint_returns_404(self) -> None:
        server = start_health_server(
            0,
            lambda: {"status": "ok"},
            lambda: [],
        )
        try:
            port = server.server_address[1]
            url = f"http://127.0.0.1:{port}/settings/filebot"
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(url, timeout=5)
            self.assertEqual(raised.exception.code, 404)
            self.assertEqual(
                json.loads(raised.exception.read().decode("utf-8")),
                {"error": "not_found"},
            )
            self.assertEqual(_post(url, {})[0], 404)
        finally:
            server.shutdown()
            server.server_close()

    def test_identity_endpoints_preserve_contract_and_statuses(self) -> None:
        calls = []

        def action(name, response):
            def handler(payload):
                calls.append((name, payload))
                return dict(response)

            return handler

        document = {
            "ok": True,
            "revision": 7,
            "rules": {"schema_version": 1, "parser": {}, "resolver": {}},
            "schema": {"parser": {}, "resolver": {}},
        }
        server = start_health_server(
            0,
            lambda: {"status": "ok"},
            lambda: [],
            identity_rules_provider=lambda: document,
            identity_rules_updater=action(
                "save",
                {
                    "ok": False,
                    "error": "revision_conflict",
                    "current_revision": 8,
                },
            ),
            identity_rules_resetter=action(
                "reset", {"ok": True, "saved": True, "revision": 9}
            ),
            identity_cache_clearer=action(
                "cache", {"ok": True, "deleted": 4}
            ),
            identity_parser_tester=action(
                "parser", {"ok": False, "error": "invalid_rules"}
            ),
            identity_resolver_tester=action(
                "resolver", {"ok": True, "status": "ACCEPTED"}
            ),
        )
        try:
            port = server.server_address[1]
            base = f"http://127.0.0.1:{port}/settings/identity"
            with urllib.request.urlopen(base, timeout=5) as response:
                fetched = json.loads(response.read().decode("utf-8"))
            self.assertEqual(fetched, document)

            self.assertEqual(_post(base, {"expected_revision": 7})[0], 409)
            self.assertEqual(
                _post(f"{base}/reset", {"expected_revision": 8})[0], 200
            )
            self.assertEqual(_post(f"{base}/cache/clear", {})[0], 200)
            self.assertEqual(
                _post(f"{base}/test-parser", {"name": "Alien"})[0], 400
            )
            self.assertEqual(
                _post(f"{base}/test-resolver", {"name": "Alien"})[0], 200
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(
            [name for name, _payload in calls],
            ["save", "reset", "cache", "parser", "resolver"],
        )
        self.assertEqual(calls[1][1], {"expected_revision": 8})

    def test_large_identity_payload_is_complete_and_excess_returns_413(self) -> None:
        seen = []

        def parser_tester(payload):
            seen.append(len(str(payload.get("blob") or "")))
            return {"ok": True, "seen": seen[-1]}

        server = start_health_server(
            0,
            lambda: {"status": "ok"},
            lambda: [],
            identity_parser_tester=parser_tester,
        )
        connection = None
        try:
            port = server.server_address[1]
            url = f"http://127.0.0.1:{port}/settings/identity/test-parser"
            status, payload = _post(url, {"blob": "x" * 300_000})
            self.assertEqual(status, 200)
            self.assertEqual(payload["seen"], 300_000)

            prefix = b'{"blob":"'
            suffix = b'"}'
            exact = prefix + (
                b"x" * (MAX_REQUEST_BODY_BYTES - len(prefix) - len(suffix))
            ) + suffix
            status, payload = _post_raw(url, exact)
            self.assertEqual(status, 200)
            self.assertEqual(
                payload["seen"], MAX_REQUEST_BODY_BYTES - len(prefix) - len(suffix)
            )

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.putrequest("POST", "/settings/identity/test-parser")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(MAX_REQUEST_BODY_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            rejected = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 413)
            self.assertEqual(rejected["error"], "payload_too_large")
            self.assertEqual(
                seen,
                [300_000, MAX_REQUEST_BODY_BYTES - len(prefix) - len(suffix)],
            )
        finally:
            if connection is not None:
                connection.close()
            server.shutdown()
            server.server_close()

    def test_profiled_identity_endpoints_and_legacy_alias_dispatch(self) -> None:
        calls = []

        def provider(profile="common"):
            calls.append(("get", profile))
            return {"ok": True, "profile": profile, "revision": 3}

        def action(name):
            def handler(payload, profile="common"):
                calls.append((name, profile, payload))
                return {"ok": True, "profile": profile}

            return handler

        server = start_health_server(
            0,
            lambda: {"status": "ok"},
            lambda: [],
            identity_rules_provider=provider,
            identity_rules_updater=action("save"),
            identity_rules_resetter=action("reset"),
            identity_cache_clearer=action("cache"),
            identity_parser_tester=action("parser"),
            identity_resolver_tester=action("resolver"),
        )
        try:
            port = server.server_address[1]
            base = f"http://127.0.0.1:{port}/settings/identity"
            with urllib.request.urlopen(f"{base}/movies", timeout=5) as response:
                fetched = json.loads(response.read().decode("utf-8"))
            self.assertEqual(fetched["profile"], "movies")
            self.assertEqual(_post(f"{base}/tv", {"expected_revision": 3})[0], 200)
            self.assertEqual(
                _post(f"{base}/tv/reset", {"expected_revision": 3})[0], 200
            )
            self.assertEqual(_post(f"{base}/movies/cache/clear", {})[0], 200)
            self.assertEqual(
                _post(f"{base}/common/test-parser", {"name": "Alien"})[0],
                200,
            )
            self.assertEqual(
                _post(f"{base}/movies/test-resolver", {"name": "Alien"})[0],
                200,
            )
            self.assertEqual(_post(base, {"expected_revision": 3})[0], 200)
            self.assertEqual(_post(f"{base}/unknown", {})[0], 404)
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(calls[0], ("get", "movies"))
        self.assertEqual(
            [(call[0], call[1]) for call in calls[1:]],
            [
                ("save", "tv"),
                ("reset", "tv"),
                ("cache", "movies"),
                ("parser", "common"),
                ("resolver", "movies"),
                ("save", "common"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
