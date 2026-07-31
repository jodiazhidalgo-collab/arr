import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

from media_panel import server


class _CapturedReportHandler:
    def __init__(self, path: str) -> None:
        self.path = path
        self.response = None

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.response = (status, body, content_type)

    def _json(self, status: int, payload: object) -> None:
        self.response = (status, payload, "application/json; charset=utf-8")


def _report_request(profile: str | None, relative: str) -> _CapturedReportHandler:
    query = {"file": relative}
    if profile is not None:
        query["profile"] = profile
    handler = _CapturedReportHandler(f"/api/report?{urllib.parse.urlencode(query)}")
    server.Handler.do_GET(handler)
    return handler


class ReportAccessTests(unittest.TestCase):
    def test_series_opens_its_technical_report_and_sanitizes_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movies"
            series_root = base / "series"
            relative = "job-series/series_result.json"
            movie_report = movie_root / relative
            series_report = series_root / relative
            movie_report.parent.mkdir(parents=True)
            series_report.parent.mkdir(parents=True)
            movie_report.write_text('{"owner":"MOVIE_ONLY"}', encoding="utf-8")
            series_report.write_text(
                json.dumps(
                    {
                        "owner": "SERIES_ONLY",
                        "source": "/data/downloads/torrents/complete/taller/Show",
                        "final": "/data/media/tv/Show",
                        "journal": "/config/series-worker/job-series/journal.json",
                        "callback_url": "http://arr-orchestrator:8787/callback?token=query-secret",
                        "token": "top-secret",
                        "api_key": "another-secret",
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(server, "REPORT_ROOT", movie_root), patch.object(
                server,
                "SERIES_REPORT_ROOT",
                series_root,
            ):
                handler = _report_request("series", relative)

        self.assertEqual(handler.response[0], 200)
        body = handler.response[1].decode("utf-8")
        payload = json.loads(body)
        self.assertEqual(payload["owner"], "SERIES_ONLY")
        self.assertEqual(
            payload["source"],
            "<DATA_DOWNLOADS>/torrents/complete/taller/Show",
        )
        self.assertEqual(payload["final"], "<DATA_MEDIA>/tv/Show")
        self.assertEqual(
            payload["journal"],
            "<CONFIG>/series-worker/job-series/journal.json",
        )
        self.assertEqual(payload["callback_url"], "<REDACTED_URL>")
        self.assertEqual(payload["token"], "<REDACTED>")
        self.assertEqual(payload["api_key"], "<REDACTED>")
        self.assertNotIn("MOVIE_ONLY", body)
        self.assertNotIn("/data/", body)
        self.assertNotIn("/config/", body)
        self.assertNotIn("http://", body)
        self.assertNotIn("top-secret", body)
        self.assertNotIn("another-secret", body)

    def test_movies_profile_and_legacy_request_keep_the_movie_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movies"
            series_root = base / "series"
            movie_root.mkdir()
            series_root.mkdir()
            (movie_root / "legacy.txt").write_text("MOVIE", encoding="utf-8")
            (series_root / "legacy.txt").write_text("SERIES", encoding="utf-8")

            with patch.object(server, "REPORT_ROOT", movie_root), patch.object(
                server,
                "SERIES_REPORT_ROOT",
                series_root,
            ):
                legacy = _report_request(None, "legacy.txt")
                explicit = _report_request("movies", "legacy.txt")

        self.assertEqual(legacy.response[0], 200)
        self.assertEqual(explicit.response[0], 200)
        self.assertEqual(legacy.response[1], b"MOVIE")
        self.assertEqual(explicit.response[1], b"MOVIE")

    def test_series_rejects_cross_root_traversal_and_non_technical_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movies"
            series_root = base / "series"
            movie_only = movie_root / "job-movie" / "request.json"
            movie_only.parent.mkdir(parents=True)
            movie_only.write_text("MOVIE", encoding="utf-8")
            series_job = series_root / "job-series"
            series_job.mkdir(parents=True)
            (series_job / "debug.txt").write_text("debug", encoding="utf-8")
            (series_job / ".series_result.json.tmp").write_text(
                "temporary",
                encoding="utf-8",
            )
            (series_root / "series_result.json").write_text("root", encoding="utf-8")
            outside = base / "outside.txt"
            outside.write_text("outside", encoding="utf-8")

            with patch.object(server, "REPORT_ROOT", movie_root), patch.object(
                server,
                "SERIES_REPORT_ROOT",
                series_root,
            ):
                requests = (
                    _report_request("series", "job-movie/request.json"),
                    _report_request("series", "../outside.txt"),
                    _report_request("series", str(outside)),
                    _report_request("series", "job-series/debug.txt"),
                    _report_request("series", "job-series/.series_result.json.tmp"),
                    _report_request("series", "series_result.json"),
                )

        self.assertTrue(all(handler.response[0] == 404 for handler in requests))

    def test_report_rejects_an_unknown_profile(self) -> None:
        handler = _report_request("tv", "anything.json")

        self.assertEqual(handler.response[0], 400)
        self.assertEqual(handler.response[1]["error"], "invalid_profile")

    def test_report_rejects_a_symlinked_technical_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "series"
            report = root / "job-series" / "journal.json"
            report.parent.mkdir(parents=True)
            report.write_text("{}", encoding="utf-8")
            real_is_symlink = Path.is_symlink

            def reported_symlink(path: Path) -> bool:
                return path == report or real_is_symlink(path)

            with patch.object(server, "SERIES_REPORT_ROOT", root), patch.object(
                Path,
                "is_symlink",
                reported_symlink,
            ):
                handler = _report_request("series", "job-series/journal.json")

        self.assertEqual(handler.response[0], 404)


if __name__ == "__main__":
    unittest.main()
