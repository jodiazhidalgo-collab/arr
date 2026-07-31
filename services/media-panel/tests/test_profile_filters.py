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
    def test_real_untagged_movie_and_series_structures_are_classified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            movie = root / "Blade Runner 2049 (2017)"
            _write_reason(movie, {"job_id": MOVIE_JOB, "phase": "filebot"})
            (movie / "Blade Runner 2049 (2017).mkv").write_bytes(b"")

            series = root / "La Agencia"
            _write_reason(series, {"job_id": SERIES_JOB, "phase": "filebot"})
            season = series / "Season 02"
            season.mkdir()
            (season / "La Agencia - S02E03.mkv").write_bytes(b"")

            with patch.object(server, "REVIEW_DIR", root), patch.object(
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

    def test_untagged_ambiguous_folder_uses_job_source_meta(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "Dark"
            _write_reason(folder, {"job_id": SERIES_JOB, "phase": "identity"})
            jobs_payload = {
                "ok": True,
                "jobs": [
                    {
                        "job_id": SERIES_JOB,
                        "source_meta_json": json.dumps({"category": "tv"}),
                    }
                ],
            }
            with patch.object(server, "REVIEW_DIR", root), patch.object(
                server,
                "_jobs_payload",
                return_value=jobs_payload,
            ) as jobs:
                shows = server._review_payload(profile="series")
                movies = server._review_payload(profile="movies")

        self.assertEqual(jobs.call_count, 2)
        self.assertEqual([item["name"] for item in shows["items"]], ["Dark"])
        self.assertEqual(movies["items"], [])
        self.assertEqual(shows["items"][0]["profile"], "series")

    def test_unclassified_review_item_is_not_hidden_by_profile_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "Sin datos suficientes"
            _write_reason(folder, {"phase": "manual"})
            with patch.object(server, "REVIEW_DIR", root):
                movies = server._review_payload(profile="movies")
                shows = server._review_payload(profile="series")

        self.assertEqual([item["name"] for item in movies["items"]], [folder.name])
        self.assertEqual([item["name"] for item in shows["items"]], [folder.name])
        self.assertEqual(movies["items"][0]["classification"], "unclassified")
        self.assertIsNone(movies["items"][0]["profile"])


class ReportProfileFilterTests(unittest.TestCase):
    def test_untagged_reports_use_job_lookup_structure_and_worker_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            movie_report = root / MOVIE_JOB / "media_result.json"
            movie_report.parent.mkdir()
            movie_report.write_text(
                json.dumps({"job_id": MOVIE_JOB, "status": "done"}),
                encoding="utf-8",
            )
            series_report = root / SERIES_JOB / "media_result.json"
            series_report.parent.mkdir()
            series_report.write_text(
                json.dumps({"job_id": SERIES_JOB, "status": "done"}),
                encoding="utf-8",
            )
            structure_report = root / "series" / "Season 01" / "informe.txt"
            structure_report.parent.mkdir(parents=True)
            structure_report.write_text("sin etiqueta", encoding="utf-8")
            legacy_report = root / "legacy" / "media_verify.json"
            legacy_report.parent.mkdir()
            legacy_report.write_text("{}", encoding="utf-8")

            jobs_payload = {
                "ok": True,
                "jobs": [
                    {"job_id": MOVIE_JOB, "category": "movies"},
                    {
                        "job_id": SERIES_JOB,
                        "source_meta": {"media_type": "tv"},
                    },
                ],
            }
            with patch.object(server, "REPORT_ROOT", root), patch.object(
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
                f"{SERIES_JOB}/media_result.json",
                "series/Season 01/informe.txt",
            },
        )
        self.assertTrue(all(item["profile"] == "movies" for item in movies["files"]))
        self.assertTrue(all(item["profile"] == "series" for item in shows["files"]))
        self.assertFalse(shows["connected"])
        self.assertEqual(shows["message"], "Motor de series no conectado")


if __name__ == "__main__":
    unittest.main()
