import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from arr_orchestrator.media_worker import (
    MediaWorkerClient,
    MediaWorkerError,
    MediaWorkerJobActive,
    MediaWorkerTransportError,
)


def response(status_code: int, payload: object = None, json_error: Exception = None) -> Mock:
    result = Mock(status_code=status_code)
    if json_error is not None:
        result.json.side_effect = json_error
    else:
        result.json.return_value = payload
    return result


class MediaWorkerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MediaWorkerClient(
            "http://media-worker:8790",
            "http://arr-orchestrator:8787",
            timeout_seconds=321,
        )

    @patch("arr_orchestrator.media_worker.requests.get")
    @patch("arr_orchestrator.media_worker.requests.post")
    def test_process_movie_makes_one_post_without_status_get(self, post: Mock, get: Mock) -> None:
        post.return_value = response(200, {"status": "done", "final": "/media/movie"})

        result = self.client.process_movie(
            "job-1",
            Path("/data/source"),
            Path("/data/final"),
            Path("/data/review"),
            Path("/data/reports"),
        )

        self.assertEqual(result["status"], "done")
        post.assert_called_once()
        get.assert_not_called()
        self.assertEqual(post.call_args.kwargs["timeout"], 321)

    @patch("arr_orchestrator.media_worker.requests.post")
    def test_active_409_raises_typed_error_and_keeps_result(self, post: Mock) -> None:
        payload = {
            "status": "active",
            "error_code": "media_job_active",
            "retryable": True,
            "job_id": "job-1",
        }
        post.return_value = response(409, payload)

        with self.assertRaises(MediaWorkerJobActive) as caught:
            self.client.process_movie(
                "job-1",
                Path("/data/source"),
                Path("/data/final"),
                Path("/data/review"),
                Path("/data/reports"),
            )

        error = caught.exception
        self.assertEqual(error.endpoint, "/process-movie")
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.error_code, "media_job_active")
        self.assertIs(error.result, payload)
        self.assertTrue(error.retryable)
        post.assert_called_once()

    @patch("arr_orchestrator.media_worker.requests.post")
    def test_server_500_raises_typed_error_and_keeps_json(self, post: Mock) -> None:
        payload = {
            "status": "error",
            "error_code": "media_source_missing",
            "error": "No existe la carpeta de media",
            "job_id": "job-1",
        }
        post.return_value = response(500, payload)

        with self.assertRaises(MediaWorkerError) as caught:
            self.client.process_trailer(
                "job-1",
                Path("/data/source"),
                Path("/data/movies"),
                Path("/data/review"),
                Path("/data/reports"),
            )

        error = caught.exception
        self.assertNotIsInstance(error, MediaWorkerJobActive)
        self.assertEqual(error.endpoint, "/process-trailer")
        self.assertEqual(error.status_code, 500)
        self.assertEqual(error.error_code, "media_source_missing")
        self.assertIs(error.result, payload)
        self.assertFalse(error.retryable)
        self.assertEqual(str(error), "No existe la carpeta de media")

    @patch("arr_orchestrator.media_worker.requests.post")
    def test_timeout_and_connection_errors_are_typed_without_retry(self, post: Mock) -> None:
        failures = (
            (requests.Timeout("agotado"), "media_worker_timeout"),
            (requests.ConnectionError("sin conexión"), "media_worker_transport_error"),
        )
        for failure, error_code in failures:
            with self.subTest(error_code=error_code):
                post.reset_mock()
                post.side_effect = failure
                with self.assertRaises(MediaWorkerTransportError) as caught:
                    self.client.process_movie(
                        "job-1",
                        Path("/data/source"),
                        Path("/data/final"),
                        Path("/data/review"),
                        Path("/data/reports"),
                    )
                self.assertEqual(caught.exception.error_code, error_code)
                self.assertIsNone(caught.exception.status_code)
                self.assertTrue(caught.exception.retryable)
                post.assert_called_once()

    @patch("arr_orchestrator.media_worker.requests.get")
    def test_job_status_returns_active_terminal_and_not_found(self, get: Mock) -> None:
        active = {"status": "active", "job_id": "job-1"}
        terminal = {
            "status": "terminal",
            "job_id": "job-1",
            "result": {"status": "done"},
        }
        not_found = {
            "status": "not_found",
            "error_code": "media_job_not_found",
        }
        get.side_effect = (
            response(200, active),
            response(200, terminal),
            response(404, not_found),
            response(200, active),
        )

        self.assertIs(self.client.job_status("job-1", "movie"), active)
        self.assertIs(self.client.job_status("job-1", "trailer"), terminal)
        self.assertIs(self.client.job_status("job-1", "movie"), not_found)
        self.assertIs(self.client.job_status("job-1", "bluray"), active)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(get.call_args_list[0].kwargs["params"], {"kind": "movie"})
        self.assertEqual(get.call_args_list[1].kwargs["params"], {"kind": "trailer"})
        self.assertEqual(get.call_args_list[3].kwargs["params"], {"kind": "bluray"})

    @patch("arr_orchestrator.media_worker.requests.post")
    def test_invalid_json_raises_transport_error(self, post: Mock) -> None:
        post.return_value = response(200, json_error=ValueError("invalid json"))

        with self.assertRaises(MediaWorkerTransportError) as caught:
            self.client.process_movie(
                "job-1",
                Path("/data/source"),
                Path("/data/final"),
                Path("/data/review"),
                Path("/data/reports"),
            )

        self.assertEqual(caught.exception.error_code, "media_worker_invalid_json")
        self.assertEqual(caught.exception.status_code, 200)
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
