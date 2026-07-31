import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_panel import server


MOVIE_JOB = "11111111-1111-4111-8111-111111111111"
SERIES_JOB = "22222222-2222-4222-8222-222222222222"


def _write_reason(folder: Path, payload: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "reason.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


class ReviewProfileFilterTests(unittest.TestCase):
    def test_real_movie_and_series_roots_are_strictly_separated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movie-review"
            series_root = base / "series-review"
            movie = movie_root / "Blade Runner 2049 (2017)"
            _write_reason(movie, {"job_id": MOVIE_JOB, "phase": "filebot"})
            (movie / "Blade Runner 2049 (2017).mkv").write_bytes(b"")

            series = series_root / "La Agencia"
            _write_reason(series, {"job_id": SERIES_JOB, "phase": "filebot"})
            season = series / "Season 02"
            season.mkdir()
            (season / "La Agencia - S02E03.mkv").write_bytes(b"")

            with patch.object(server, "REVIEW_DIR", movie_root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                series_root,
            ), patch.object(
                server,
                "_jobs_payload",
            ) as jobs:
                movies = server._review_payload(profile="movies")
                shows = server._review_payload(profile="series")

        jobs.assert_not_called()
        self.assertEqual([item["name"] for item in movies["items"]], [movie.name])
        self.assertEqual([item["name"] for item in shows["items"]], [series.name])
        self.assertEqual(movies["items"][0]["classification"], "movies")
        self.assertEqual(shows["items"][0]["classification"], "series")
        self.assertEqual(
            shows["items"][0]["path"],
            f"{server.SERIES_REVIEW_ALIAS}/La Agencia",
        )
        self.assertTrue(shows["connected"])

    def test_series_review_skips_hidden_tmp_and_symlink_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "series-review"
            visible = root / "Visible Show"
            hidden = root / ".Partial Show"
            staging = root / "Visible Show.123.tmp"
            partial_staging = root / "Another Show.tmp.partial"
            linked = root / "Linked Show"
            for folder in (visible, hidden, staging, partial_staging, linked):
                _write_reason(folder, {"job_id": SERIES_JOB, "phase": "review"})

            real_is_symlink = Path.is_symlink

            def reported_symlink(path: Path) -> bool:
                return path == linked or real_is_symlink(path)

            with patch.object(server, "SERIES_REVIEW_DIR", root), patch.object(
                Path,
                "is_symlink",
                reported_symlink,
            ), patch.object(
                server,
                "_read_json",
                wraps=server._read_json,
            ) as read_json:
                shows = server._review_payload(profile="series")

        self.assertEqual([item["name"] for item in shows["items"]], [visible.name])
        self.assertEqual(
            [call.args[0].parent.name for call in read_json.call_args_list],
            [visible.name],
        )

    def test_series_root_ownership_does_not_query_movie_job_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movie-review"
            series_root = base / "series-review"
            movie_root.mkdir()
            folder = series_root / "Dark"
            _write_reason(folder, {"job_id": SERIES_JOB, "phase": "identity"})
            with patch.object(server, "REVIEW_DIR", movie_root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                series_root,
            ), patch.object(
                server,
                "_jobs_payload",
            ) as jobs:
                shows = server._review_payload(profile="series")
                movies = server._review_payload(profile="movies")

        jobs.assert_not_called()
        self.assertEqual([item["name"] for item in shows["items"]], ["Dark"])
        self.assertEqual(movies["items"], [])
        self.assertEqual(shows["items"][0]["profile"], "series")

    def test_unclassified_items_stay_inside_their_owner_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movie-review"
            series_root = base / "series-review"
            movie_folder = movie_root / "Sin datos suficientes"
            series_folder = series_root / "Otra sin datos"
            _write_reason(movie_folder, {"phase": "manual"})
            _write_reason(series_folder, {"phase": "manual"})
            with patch.object(server, "REVIEW_DIR", movie_root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                series_root,
            ):
                movies = server._review_payload(profile="movies")
                shows = server._review_payload(profile="series")

        self.assertEqual([item["name"] for item in movies["items"]], [movie_folder.name])
        self.assertEqual([item["name"] for item in shows["items"]], [series_folder.name])
        self.assertEqual(movies["items"][0]["classification"], "unclassified")
        self.assertIsNone(movies["items"][0]["profile"])
        self.assertEqual(shows["items"][0]["classification"], "series")


class ReportProfileFilterTests(unittest.TestCase):
    def test_report_roots_are_separate_and_series_paths_are_aliased(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movie-reports"
            series_root = base / "series-reports"
            movie_report = movie_root / MOVIE_JOB / "media_result.json"
            movie_report.parent.mkdir(parents=True)
            movie_report.write_text(
                json.dumps({"job_id": MOVIE_JOB, "status": "done"}),
                encoding="utf-8",
            )
            series_report = series_root / SERIES_JOB / "series_result.json"
            series_report.parent.mkdir(parents=True)
            series_report.write_text(
                json.dumps({"job_id": SERIES_JOB, "status": "done"}),
                encoding="utf-8",
            )
            structure_report = series_root / "series" / "Season 01" / "informe.txt"
            structure_report.parent.mkdir(parents=True)
            structure_report.write_text("sin etiqueta", encoding="utf-8")
            legacy_report = movie_root / "legacy" / "media_verify.json"
            legacy_report.parent.mkdir()
            legacy_report.write_text("{}", encoding="utf-8")

            jobs_payload = {
                "ok": True,
                "jobs": [
                    {"job_id": MOVIE_JOB, "category": "movies"},
                ],
            }
            with patch.object(server, "REPORT_ROOT", movie_root), patch.object(
                server,
                "SERIES_REPORT_ROOT",
                series_root,
            ), patch.object(
                server,
                "_jobs_payload",
                return_value=jobs_payload,
            ):
                movies = server._reports_payload(profile="movies")
                shows = server._reports_payload(profile="series")

        movie_paths = {item["relative"].replace("\\", "/") for item in movies["files"]}
        series_paths = {item["relative"].replace("\\", "/") for item in shows["files"]}
        self.assertEqual(
            movie_paths,
            {
                f"{MOVIE_JOB}/media_result.json",
                "legacy/media_verify.json",
            },
        )
        self.assertEqual(
            series_paths,
            {
                f"{SERIES_JOB}/series_result.json",
            },
        )
        self.assertTrue(all(item["profile"] == "movies" for item in movies["files"]))
        self.assertTrue(all(item["profile"] == "series" for item in shows["files"]))
        self.assertTrue(shows["connected"])
        self.assertEqual(shows["report_root"], server.SERIES_REPORT_ALIAS)
        self.assertTrue(
            all(
                item["path"].startswith(server.SERIES_REPORT_ALIAS + "/")
                for item in shows["files"]
            )
        )
        self.assertNotIn(str(series_root), json.dumps(shows))

    def test_missing_series_roots_report_disconnected_without_movie_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movie-reports"
            movie_root.mkdir()
            (movie_root / "movie.json").write_text("{}", encoding="utf-8")
            missing_series = base / "missing-series"
            with patch.object(server, "REPORT_ROOT", movie_root), patch.object(
                server,
                "SERIES_REPORT_ROOT",
                missing_series,
            ):
                shows = server._reports_payload(profile="series")

        self.assertEqual(shows["files"], [])
        self.assertFalse(shows["connected"])
        self.assertEqual(shows["report_root"], server.SERIES_REPORT_ALIAS)
        self.assertEqual(shows["message"], "Motor de series no conectado")


if __name__ == "__main__":
    unittest.main()
