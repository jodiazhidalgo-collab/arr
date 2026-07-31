import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from arr_orchestrator.series_worker import (
    SeriesWorkerBadRequest,
    SeriesWorkerBusy,
    SeriesWorkerClient,
    SeriesWorkerConflict,
    SeriesWorkerError,
    SeriesWorkerTransportError,
    SeriesWorkerUnavailable,
)


def response(status_code: int, payload: object = None, json_error: Exception = None) -> Mock:
    result = Mock(status_code=status_code)
    if json_error is not None:
        result.json.side_effect = json_error
    else:
        result.json.return_value = payload
    return result


def success(status: str, job_id: str = "job-1", **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "status": status,
        "job_id": job_id,
        "kind": "series",
    }
    payload.update(extra)
    if status == "terminal" and "result" not in payload:
        payload["result"] = {
            "status": "done",
            "job_id": job_id,
            "kind": "series",
            "published": ["S01E01.mkv"],
        }
    return payload


class SeriesWorkerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SeriesWorkerClient(
            "http://series-worker:8791/",
            "http://arr-orchestrator:8787/",
            timeout_seconds=321,
            status_timeout_seconds=7,
        )
        self.paths = (
            Path("/data/downloads/torrents/complete/taller/job-1"),
            Path("/data/downloads/torrents/complete/taller/job-1/series_filebot_output"),
            Path("/data/media/tv"),
            Path("/data/media/repetidas_vs_error_series"),
            Path("/config/series-worker"),
        )

    def test_default_post_budget_is_short_because_acceptance_is_asynchronous(self) -> None:
        client = SeriesWorkerClient("http://series-worker:8791")
        preview = client.preview_process_series("job-1", *self.paths)
        self.assertEqual(preview["timeout_sec"], 30)

    def process(self, job_id: str = "job-1") -> dict[str, object]:
        return self.client.process_series(job_id, *self.paths)

    @patch("arr_orchestrator.series_worker.requests.get")
    def test_version_checks_health_with_short_timeout_and_returns_safe_status(
        self, get: Mock
    ) -> None:
        get.return_value = response(
            200,
            {
                "ok": True,
                "status": "ok",
                "service": "series-worker",
                "checks": {
                    "rules": {"ok": True, "fingerprint": "a" * 64},
                    "tools": {"ok": True, "missing": []},
                    "atomicity": {"ok": True, "status": "preflight_on_submit"},
                },
                "errors": [],
            },
        )

        self.assertEqual(self.client.version(), "ok")
        get.assert_called_once_with(
            "http://series-worker:8791/health",
            timeout=7,
        )

    @patch("arr_orchestrator.series_worker.requests.get")
    def test_health_503_is_typed_and_does_not_expose_details(self, get: Mock) -> None:
        get.return_value = response(
            503,
            {
                "ok": False,
                "status": "unavailable",
                "service": "series-worker",
                "message": "token=hidden /data/media/tv https://private.local/health",
                "checks": {"tools": {"ok": False}},
            },
        )

        with self.assertRaises(SeriesWorkerUnavailable) as caught:
            self.client.version()

        error = caught.exception
        self.assertEqual(error.endpoint, "/health")
        self.assertEqual(error.status_code, 503)
        self.assertNotIn("hidden", str(error))
        self.assertNotIn("private.local", str(error))
        self.assertNotIn("/data/media", str(error))

    @patch("arr_orchestrator.series_worker.requests.get")
    def test_health_rejects_invalid_json_and_incompatible_success(self, get: Mock) -> None:
        get.side_effect = (
            response(200, json_error=ValueError("invalid")),
            response(
                200,
                {
                    "ok": True,
                    "status": "ok",
                    "service": "media-worker",
                    "checks": {},
                },
            ),
        )

        with self.assertRaises(SeriesWorkerTransportError) as invalid_json:
            self.client.version()
        self.assertEqual(invalid_json.exception.error_code, "series_worker_invalid_json")
        with self.assertRaises(SeriesWorkerTransportError) as invalid_contract:
            self.client.version()
        self.assertEqual(
            invalid_contract.exception.error_code,
            "series_worker_invalid_response",
        )

    @patch("arr_orchestrator.series_worker.requests.get")
    @patch("arr_orchestrator.series_worker.requests.post")
    def test_post_uses_exact_seven_key_payload_and_no_status_get(
        self, post: Mock, get: Mock
    ) -> None:
        post.return_value = response(202, success("accepted"))

        result = self.process()

        self.assertEqual(result["status"], "accepted")
        post.assert_called_once_with(
            "http://series-worker:8791/process-series",
            json={
                "job_id": "job-1",
                "job_root": str(self.paths[0]),
                "source_root": str(self.paths[1]),
                "final_root": str(self.paths[2]),
                "review_root": str(self.paths[3]),
                "reports_root": str(self.paths[4]),
                "callback_url": "http://arr-orchestrator:8787/jobs/job-1/events",
            },
            timeout=321,
        )
        self.assertEqual(len(post.call_args.kwargs["json"]), 7)
        get.assert_not_called()

    @patch("arr_orchestrator.series_worker.requests.post")
    def test_post_accepts_active_and_terminal_contracts(self, post: Mock) -> None:
        terminal = success("terminal")
        post.side_effect = (
            response(202, success("active")),
            response(200, terminal),
        )

        self.assertEqual(self.process()["status"], "active")
        self.assertIs(self.process(), terminal)

    @patch("arr_orchestrator.series_worker.requests.post")
    def test_terminal_accepts_only_the_three_complete_worker_outcomes(self, post: Mock) -> None:
        terminals = []
        for outcome in ("done", "review", "failed"):
            terminal = success("terminal")
            terminal["result"] = {**terminal["result"], "status": outcome}
            terminals.append(terminal)
        post.side_effect = [response(200, terminal) for terminal in terminals]

        self.assertEqual(
            [self.process()["result"]["status"] for _ in terminals],
            ["done", "review", "failed"],
        )

    @patch("arr_orchestrator.series_worker.requests.get")
    def test_get_uses_encoded_job_id_kind_series_and_short_timeout(self, get: Mock) -> None:
        get.return_value = response(202, success("active", "job one"))

        result = self.client.job_status("job one")

        self.assertEqual(result["status"], "active")
        get.assert_called_once_with(
            "http://series-worker:8791/jobs/job%20one/status",
            params={"kind": "series"},
            timeout=7,
        )

    @patch("arr_orchestrator.series_worker.requests.get")
    def test_get_accepts_active_recoverable_terminal_and_not_found(self, get: Mock) -> None:
        active = success("active")
        recoverable = success("recoverable", journal_state="PROCESSING", retryable=True)
        terminal = success("terminal")
        not_found = {
            "ok": False,
            "status": "not_found",
            "error": "series_job_not_found",
            "retryable": False,
            "job_id": "job-1",
            "kind": "series",
        }
        get.side_effect = (
            response(202, active),
            response(202, recoverable),
            response(200, terminal),
            response(404, not_found),
        )

        self.assertIs(self.client.job_status("job-1"), active)
        self.assertIs(self.client.job_status("job-1"), recoverable)
        self.assertIs(self.client.job_status("job-1"), terminal)
        self.assertIs(self.client.job_status("job-1"), not_found)

    @patch("arr_orchestrator.series_worker.requests.get")
    def test_get_maps_400_and_503_to_the_same_typed_contract(self, get: Mock) -> None:
        get.side_effect = (
            response(
                400,
                {"ok": False, "error": "invalid_request", "message": "kind inválido"},
            ),
            response(
                503,
                {
                    "ok": False,
                    "error": "series_worker_unavailable",
                    "message": "journal no disponible",
                },
            ),
        )

        with self.assertRaises(SeriesWorkerBadRequest):
            self.client.job_status("job-1")
        with self.assertRaises(SeriesWorkerUnavailable):
            self.client.job_status("job-1")

    @patch("arr_orchestrator.series_worker.requests.get")
    def test_get_rejects_accepted_wrong_job_wrong_kind_and_wrong_http_pairing(
        self, get: Mock
    ) -> None:
        invalid = (
            response(202, success("accepted")),
            response(202, success("active", "another-job")),
            response(202, {**success("active"), "kind": "movie"}),
            response(200, success("active")),
        )
        for mocked in invalid:
            with self.subTest(payload=mocked.json.return_value):
                get.return_value = mocked
                with self.assertRaises(SeriesWorkerTransportError) as caught:
                    self.client.job_status("job-1")
                self.assertEqual(caught.exception.error_code, "series_worker_invalid_response")

    @patch("arr_orchestrator.series_worker.requests.post")
    def test_busy_409_is_typed_and_always_retryable(self, post: Mock) -> None:
        payload = {
            "ok": False,
            "error": "series_worker_busy",
            "message": "Motor ocupado",
            "retryable": False,
        }
        post.return_value = response(409, payload)

        with self.assertRaises(SeriesWorkerBusy) as caught:
            self.process()

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.error_code, "series_worker_busy")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.result, payload)

    @patch("arr_orchestrator.series_worker.requests.post")
    def test_job_conflict_409_is_typed_and_never_retryable(self, post: Mock) -> None:
        post.return_value = response(
            409,
            {
                "ok": False,
                "error": "job_conflict",
                "message": "Payload distinto",
                "retryable": True,
            },
        )

        with self.assertRaises(SeriesWorkerConflict) as caught:
            self.process()

        self.assertEqual(caught.exception.error_code, "job_conflict")
        self.assertFalse(caught.exception.retryable)

    @patch("arr_orchestrator.series_worker.requests.post")
    def test_400_and_503_have_distinct_error_types(self, post: Mock) -> None:
        cases = (
            (400, "invalid_request", SeriesWorkerBadRequest, False),
            (503, "series_worker_unavailable", SeriesWorkerUnavailable, False),
        )
        for status_code, code, expected_type, retryable in cases:
            with self.subTest(code=code):
                post.return_value = response(
                    status_code,
                    {"ok": False, "error": code, "message": code, "retryable": retryable},
                )
                with self.assertRaises(expected_type) as caught:
                    self.process()
                self.assertEqual(caught.exception.status_code, status_code)
                self.assertEqual(caught.exception.error_code, code)
                self.assertEqual(caught.exception.retryable, retryable)

    @patch("arr_orchestrator.series_worker.requests.get")
    @patch("arr_orchestrator.series_worker.requests.post")
    def test_post_timeout_returns_active_status_without_second_post(
        self, post: Mock, get: Mock
    ) -> None:
        post.side_effect = requests.Timeout("token=secret /data/media/tv")
        active = success("active")
        get.return_value = response(202, active)

        result = self.process()

        self.assertIs(result, active)
        post.assert_called_once()
        get.assert_called_once()

    @patch("arr_orchestrator.series_worker.requests.get")
    @patch("arr_orchestrator.series_worker.requests.post")
    def test_post_timeout_returns_recoverable_or_terminal_status(
        self, post: Mock, get: Mock
    ) -> None:
        post.side_effect = requests.Timeout("agotado")
        recoverable = success("recoverable", journal_state="PROCESSING")
        terminal = success("terminal")
        get.side_effect = (response(202, recoverable), response(200, terminal))

        self.assertIs(self.process(), recoverable)
        self.assertIs(self.process(), terminal)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(get.call_count, 2)

    @patch("arr_orchestrator.series_worker.requests.get")
    @patch("arr_orchestrator.series_worker.requests.post")
    def test_post_timeout_not_found_is_typed_retryable_and_never_reposts(
        self, post: Mock, get: Mock
    ) -> None:
        post.side_effect = requests.Timeout("agotado")
        get.return_value = response(
            404,
            {
                "ok": False,
                "status": "not_found",
                "error": "series_job_not_found",
                "retryable": False,
                "job_id": "job-1",
                "kind": "series",
            },
        )

        with self.assertRaises(SeriesWorkerTransportError) as caught:
            self.process()

        self.assertEqual(caught.exception.error_code, "series_worker_timeout_not_found")
        self.assertTrue(caught.exception.retryable)
        post.assert_called_once()
        get.assert_called_once()

    @patch("arr_orchestrator.series_worker.requests.get")
    @patch("arr_orchestrator.series_worker.requests.post")
    def test_post_timeout_with_failed_status_check_keeps_safe_typed_error(
        self, post: Mock, get: Mock
    ) -> None:
        post.side_effect = requests.Timeout("token=hidden /data/media/private.mkv")
        get.side_effect = requests.ConnectionError("password=hidden /config/secret")

        with self.assertRaises(SeriesWorkerTransportError) as caught:
            self.process()

        error = caught.exception
        self.assertEqual(error.error_code, "series_worker_timeout_status_unknown")
        self.assertEqual(
            error.result,
            {"status_check_error": "series_worker_transport_error"},
        )
        self.assertNotIn("hidden", str(error))
        self.assertNotIn("/data/", str(error))

    @patch("arr_orchestrator.series_worker.requests.get")
    @patch("arr_orchestrator.series_worker.requests.post")
    def test_non_timeout_transport_error_does_not_poll_or_retry(
        self, post: Mock, get: Mock
    ) -> None:
        post.side_effect = requests.ConnectionError("token=hidden")

        with self.assertRaises(SeriesWorkerTransportError) as caught:
            self.process()

        self.assertEqual(caught.exception.error_code, "series_worker_transport_error")
        self.assertTrue(caught.exception.retryable)
        post.assert_called_once()
        get.assert_not_called()

    @patch("arr_orchestrator.series_worker.requests.post")
    def test_error_message_and_result_are_sanitized(self, post: Mock) -> None:
        post.return_value = response(
            503,
            {
                "ok": False,
                "error": "series_worker_unavailable",
                "message": (
                    "token=hidden /data/media/tv/secret.mkv "
                    "/home/user/private.mkv https://private.local/file"
                ),
                "authorization": "Bearer hidden",
                "source_path": "/data/downloads/private.mkv",
                "other_path": "/home/user/private.mkv",
            },
        )

        with self.assertRaises(SeriesWorkerUnavailable) as caught:
            self.process()

        error = caught.exception
        self.assertNotIn("hidden", str(error))
        self.assertNotIn("private.local", str(error))
        self.assertNotIn("/home/user", str(error))
        self.assertIn("<DATA_MEDIA>", str(error))
        self.assertEqual(error.result["authorization"], "<REDACTED>")
        self.assertEqual(error.result["source_path"], "<DATA_DOWNLOADS>/private.mkv")
        self.assertEqual(error.result["other_path"], "<PATH_REDACTED>")

    @patch("arr_orchestrator.series_worker.requests.post")
    def test_invalid_json_and_non_object_json_are_transport_errors(self, post: Mock) -> None:
        cases = (
            response(200, json_error=ValueError("invalid")),
            response(200, ["not", "an", "object"]),
        )
        for mocked in cases:
            with self.subTest(payload=mocked):
                post.return_value = mocked
                with self.assertRaises(SeriesWorkerTransportError) as caught:
                    self.process()
                self.assertEqual(caught.exception.error_code, "series_worker_invalid_json")

    @patch("arr_orchestrator.series_worker.requests.post")
    def test_rejects_wrong_http_status_status_job_kind_or_terminal_shape(self, post: Mock) -> None:
        terminal = success("terminal")
        foreign_result = {
            **terminal,
            "result": {**terminal["result"], "job_id": "foreign-job"},
        }
        wrong_kind_result = {
            **terminal,
            "result": {**terminal["result"], "kind": "movie"},
        }
        wrong_status_result = {
            **terminal,
            "result": {**terminal["result"], "status": "partial"},
        }
        invalid = (
            response(200, success("accepted")),
            response(202, success("terminal")),
            response(202, success("accepted", "another-job")),
            response(202, {**success("accepted"), "kind": "movie"}),
            response(200, {**success("terminal"), "result": "done"}),
            response(200, foreign_result),
            response(200, wrong_kind_result),
            response(200, wrong_status_result),
        )
        for mocked in invalid:
            with self.subTest(payload=mocked.json.return_value):
                post.return_value = mocked
                with self.assertRaises(SeriesWorkerTransportError) as caught:
                    self.process()
                self.assertEqual(caught.exception.error_code, "series_worker_invalid_response")

    def test_preview_has_same_payload_and_does_not_make_network_calls(self) -> None:
        preview = self.client.preview_process_series("job-1", *self.paths)

        self.assertEqual(preview["service"], "series-worker")
        self.assertEqual(preview["endpoint"], "/process-series")
        self.assertEqual(preview["timeout_sec"], 321)
        self.assertEqual(len(preview["payload"]), 7)
        self.assertEqual(preview["payload"]["source_root"], str(self.paths[1]))


if __name__ == "__main__":
    unittest.main()
