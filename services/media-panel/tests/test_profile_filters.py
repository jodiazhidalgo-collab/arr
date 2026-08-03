import json
from pathlib import Path
import tempfile
import unittest
import zipfile
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


def _write_codex_zip(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("job.json", json.dumps(payload))


class JobAndCodexProfileFilterTests(unittest.TestCase):
    def test_jobs_are_filtered_by_real_category(self) -> None:
        jobs = [
            {"job_id": MOVIE_JOB, "category": "movies"},
            {"job_id": SERIES_JOB, "category": "tv"},
            {"job_id": "trailer", "category": "trailers_automatizacion"},
            {
                "job_id": "manual-series",
                "category": "manual",
                "source_meta_json": json.dumps({"media_type": "tv"}),
            },
        ]
        with patch.object(server, "_upstream_json", return_value=jobs):
            movies = server._jobs_payload(profile="movies")
            shows = server._jobs_payload(profile="series")
            legacy = server._jobs_payload()

        self.assertEqual(
            [job["job_id"] for job in movies["jobs"]],
            [MOVIE_JOB, "trailer"],
        )
        self.assertEqual(
            [job["job_id"] for job in shows["jobs"]],
            [SERIES_JOB, "manual-series"],
        )
        self.assertEqual(len(legacy["jobs"]), 4)

    def test_codex_zips_are_filtered_by_job_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_codex_zip(
                root / "movies" / "movie.zip",
                {"name": "Movie", "category": "movies"},
            )
            _write_codex_zip(
                root / "tv" / "series.zip",
                {"name": "Series", "category": "tv"},
            )
            _write_codex_zip(
                root / "repetidas_vs_error" / "series-review.zip",
                {"name": "Series review", "category": "tv"},
            )
            with patch.object(server, "CODEX_DIAG_ROOT", root):
                movies = server._codex_diagnostics_payload(profile="movies")
                shows = server._codex_diagnostics_payload(profile="series")

        self.assertEqual([item["name"] for item in movies["files"]], ["movie.zip"])
        self.assertEqual(
            {item["name"] for item in shows["files"]},
            {"series.zip", "series-review.zip"},
        )
        self.assertTrue(all(item["profile"] == "movies" for item in movies["files"]))
        self.assertTrue(all(item["profile"] == "series" for item in shows["files"]))


class ReviewProfileFilterTests(unittest.TestCase):
    def test_job_contexts_stop_immediately_when_jobs_endpoint_is_down(self) -> None:
        with patch.object(
            server,
            "_jobs_payload",
            return_value={"ok": False, "jobs": [], "error": "down"},
        ), patch.object(server, "_upstream_json") as upstream:
            contexts = server._job_contexts({MOVIE_JOB, SERIES_JOB})

        self.assertEqual(contexts, {})
        upstream.assert_not_called()

    def test_job_contexts_bound_slow_detail_fallbacks(self) -> None:
        job_ids = {
            f"0000000{number}-0000-4000-8000-00000000000{number}"
            for number in range(1, 7)
        }
        with patch.object(
            server,
            "_jobs_payload",
            return_value={"ok": True, "jobs": []},
        ), patch.object(
            server,
            "_upstream_json",
            side_effect=lambda url, timeout: {
                "job_id": url.rsplit("/", 1)[-1],
                "category": "tv",
            },
        ) as upstream:
            contexts = server._job_contexts(job_ids)

        self.assertEqual(len(contexts), server.MAX_REVIEW_JOB_DETAIL_LOOKUPS)
        self.assertEqual(upstream.call_count, server.MAX_REVIEW_JOB_DETAIL_LOOKUPS)
        self.assertTrue(
            all(
                call.kwargs["timeout"] == server.REVIEW_JOB_DETAIL_TIMEOUT_SEC
                for call in upstream.call_args_list
            )
        )

    def test_real_movie_and_series_roots_are_strictly_separated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movie-review"
            series_root = base / "series-review"
            movie = movie_root / "Blade Runner 2049 (2017)"
            _write_reason(
                movie,
                {"job_id": MOVIE_JOB, "phase": "filebot", "category": "movies"},
            )
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

    def test_shared_root_keeps_series_and_movies_in_their_own_web_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repetidas_vs_error"
            movie = root / "Blade Runner 2049 (2017)"
            series = root / "El príncipe de Bel-Air - S01E08"
            unknown = root / "Antigua sin metadatos"
            _write_reason(movie, {"job_id": "movie", "profile": "movies"})
            _write_reason(
                series,
                {
                    "schema": "series-review-v1",
                    "job_id": "series",
                    "profile": "series",
                    "category": "tv",
                },
            )
            (series / "El príncipe de Bel-Air - S01E08.mkv").write_bytes(b"")
            _write_reason(unknown, {"job_id": "legacy"})

            with patch.object(server, "REVIEW_DIR", root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                root,
            ), patch.object(server, "_jobs_payload") as jobs:
                movies = server._review_payload(profile="movies")
                shows = server._review_payload(profile="series")
                legacy = server._review_payload()

        jobs.assert_not_called()
        self.assertEqual(
            {item["name"] for item in movies["items"]},
            {movie.name, unknown.name},
        )
        self.assertEqual([item["name"] for item in shows["items"]], [series.name])
        self.assertEqual(
            {item["name"] for item in legacy["items"]},
            {movie.name, series.name, unknown.name},
        )
        self.assertEqual(shows["review_dir"], server.SERIES_REVIEW_ALIAS)
        self.assertEqual(movies["review_dir"], server.SERIES_REVIEW_ALIAS)
        self.assertTrue(
            all(
                item["path"].startswith(server.SERIES_REVIEW_ALIAS + "/")
                for item in movies["items"]
            )
        )
        self.assertEqual(
            {item["name"]: item["profile"] for item in movies["items"]},
            {movie.name: "movies", unknown.name: None},
        )

    def test_declared_reason_file_is_preferred_and_exposes_v2_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repetidas_vs_error"
            folder = root / "Serie con video invalido"
            _write_reason(
                folder,
                {
                    "schema": "series-review-v2",
                    "profile": "series",
                    "category": "tv",
                    "job_id": SERIES_JOB,
                    "reason_file": "Video no valido.txt",
                    "reason_code": "video_invalid",
                    "reason_kind": "video",
                },
            )
            (folder / "Audio no valido.txt").write_text(
                "Audio no valido\ntexto que no debe elegirse\n",
                encoding="utf-8",
            )
            (folder / "Video no valido.txt").write_text(
                "Video no valido\nDebe haber exactamente un video.\n",
                encoding="utf-8",
            )
            with patch.object(server, "REVIEW_DIR", root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                root,
            ):
                shows = server._review_payload(profile="series")

        self.assertEqual(len(shows["items"]), 1)
        item = shows["items"][0]
        self.assertEqual(item["reason_file"], "Video no valido.txt")
        self.assertIn("exactamente un video", item["reason_text"])
        self.assertNotIn("no debe elegirse", item["reason_text"])
        self.assertEqual(item["reason_code"], "video_invalid")
        self.assertEqual(item["reason_kind"], "video")

    def test_unsafe_or_missing_declared_reason_file_uses_legacy_txt_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movie-review"
            series_root = base / "series-review"
            series_root.mkdir()
            unsafe = movie_root / "Unsafe"
            _write_reason(
                unsafe,
                {
                    "profile": "movies",
                    "reason_file": "../fuera.txt",
                    "reason": "identity_suspicious",
                },
            )
            (base / "fuera.txt").write_text("NO LEER", encoding="utf-8")
            (unsafe / "Revision manual.txt").write_text(
                "Revision manual\nIdentidad ambigua.\n",
                encoding="utf-8",
            )
            missing = movie_root / "Missing"
            _write_reason(
                missing,
                {
                    "profile": "movies",
                    "reason_file": "No existe.txt",
                    "reason": "destination_exists_before_processing",
                },
            )
            (missing / "Pelicula repetida.txt").write_text(
                "Pelicula repetida\nYa existe destino final.\n",
                encoding="utf-8",
            )
            with patch.object(server, "REVIEW_DIR", movie_root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                series_root,
            ):
                movies = server._review_payload(profile="movies")

        items = {item["name"]: item for item in movies["items"]}
        self.assertEqual(items["Unsafe"]["reason_file"], "Revision manual.txt")
        self.assertNotIn("NO LEER", items["Unsafe"]["reason_text"])
        self.assertEqual(items["Unsafe"]["reason_code"], "identity_suspicious")
        self.assertEqual(items["Unsafe"]["reason_kind"], "manual")
        self.assertEqual(items["Missing"]["reason_file"], "Pelicula repetida.txt")
        self.assertEqual(items["Missing"]["reason_kind"], "duplicate")

    def test_legacy_series_marker_is_typed_from_real_reason_not_its_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repetidas_vs_error"
            audio = root / "Serie - S01E01"
            process = root / "Serie - S01E02"
            internal_duplicate = root / "Serie - S01E03"
            casefold_collision = root / "Serie - S01E04"
            library_duplicate = root / "Serie - S01E05"
            mixed_manifest = root / "Serie - S01E06"
            cases = (
                (audio, "procesamiento_fallido:No hay audio español válido."),
                (
                    process,
                    "procesamiento_fallido:Falló Audio Master - S01E02.mkv: "
                    "ffprobe no devolvió pistas.",
                ),
                (
                    internal_duplicate,
                    "episodio_duplicado:S01E03:Serie.S01E03.mkv:Serie.S01E03.1080p.mkv",
                ),
                (
                    casefold_collision,
                    "colision_casefold:Serie.S01E04.mkv:serie.s01e04.mkv",
                ),
                (
                    library_duplicate,
                    "colision_existente:Serie/Season 01/Serie.S01E05.mkv",
                ),
            )
            for folder, raw_reason in cases:
                _write_reason(
                    folder,
                    {
                        "schema": "series-review-v1",
                        "profile": "series",
                        "category": "tv",
                        "job_id": SERIES_JOB,
                        "reasons": [raw_reason],
                    },
                )
                (folder / "Serie repetida.txt").write_text(
                    f"Serie repetida\n{raw_reason}\n",
                    encoding="utf-8",
                )
            mixed_reasons = [
                "manifest_no_apto",
                "colision_existente:Serie/Season 01/Serie.S01E06.mkv",
            ]
            _write_reason(
                mixed_manifest,
                {
                    "schema": "series-review-v1",
                    "profile": "series",
                    "category": "tv",
                    "job_id": SERIES_JOB,
                    "reasons": mixed_reasons,
                },
            )
            (mixed_manifest / "Serie repetida.txt").write_text(
                "Serie repetida\n" + "\n".join(mixed_reasons) + "\n",
                encoding="utf-8",
            )
            with patch.object(server, "REVIEW_DIR", root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                root,
            ):
                shows = server._review_payload(profile="series")

        items = {item["name"]: item for item in shows["items"]}
        self.assertEqual(items[audio.name]["reason_code"], "procesamiento_fallido")
        self.assertEqual(items[audio.name]["reason_kind"], "audio")
        self.assertEqual(items[process.name]["reason_code"], "procesamiento_fallido")
        self.assertEqual(items[process.name]["reason_kind"], "process")
        self.assertEqual(items[internal_duplicate.name]["reason_kind"], "manual")
        self.assertEqual(items[casefold_collision.name]["reason_kind"], "manual")
        self.assertEqual(items[library_duplicate.name]["reason_kind"], "duplicate")
        self.assertEqual(items[mixed_manifest.name]["reason_kind"], "manual")

    def test_legacy_processing_review_wrapper_is_manual_unless_detail_is_typed(self) -> None:
        cases = (
            (
                "procesamiento_requiere_revision: detalle que requiere decisión humana",
                "manual",
            ),
            (
                "procesamiento_requiere_revision: No hay audio español válido.",
                "audio",
            ),
            (
                "procesamiento_requiere_revision: Se esperaban 1 pistas de vídeo y hay 0.",
                "video",
            ),
            (
                "procesamiento_requiere_revision: Codec de subtítulo desconocido.",
                "subtitle",
            ),
            (
                "procesamiento_requiere_revision: Ha fallado el OCR del subtítulo.",
                "ocr",
            ),
            (
                "procesamiento_requiere_revision: Falló la extracción del pack.",
                "extraction",
            ),
            (
                "procesamiento_requiere_revision: FileBot no pudo clasificar el pack.",
                "filebot",
            ),
            (
                "procesamiento_requiere_revision: Episodio ya existente en la biblioteca.",
                "duplicate",
            ),
        )

        for raw_reason, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                reason_code, reason_kind = server._review_reason_metadata(
                    {"reasons": [raw_reason]},
                    "Serie repetida.txt",
                    f"Serie repetida\n{raw_reason}\n",
                )
                self.assertEqual(reason_code, "procesamiento_requiere_revision")
                self.assertEqual(reason_kind, expected_kind)

    def test_shared_root_job_metadata_wins_over_a_misleading_folder_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repetidas_vs_error"
            folder = root / "Parece película (2026)"
            _write_reason(folder, {"job_id": SERIES_JOB})
            jobs_payload = {
                "ok": True,
                "jobs": [{"job_id": SERIES_JOB, "category": "tv"}],
            }
            with patch.object(server, "REVIEW_DIR", root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                root,
            ), patch.object(server, "_jobs_payload", return_value=jobs_payload):
                movies = server._review_payload(profile="movies")
                shows = server._review_payload(profile="series")

        self.assertEqual(movies["items"], [])
        self.assertEqual([item["name"] for item in shows["items"]], [folder.name])

    def test_shared_root_legacy_series_marker_survives_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repetidas_vs_error"
            folder = root / "Serie antigua"
            folder.mkdir(parents=True)
            (folder / "reason.json").write_text("{", encoding="utf-8")
            (folder / "Serie repetida.txt").write_text("Serie repetida", encoding="utf-8")
            (folder / "Capitulo - S01E02.mkv").write_bytes(b"")
            with patch.object(server, "REVIEW_DIR", root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                root,
            ):
                movies = server._review_payload(profile="movies")
                shows = server._review_payload(profile="series")

        self.assertEqual(movies["items"], [])
        self.assertEqual([item["name"] for item in shows["items"]], [folder.name])

    def test_shared_root_movie_title_with_series_word_stays_in_movies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repetidas_vs_error"
            folder = root / "A Series of Unfortunate Events (2017)"
            _write_reason(folder, {"job_id": "legacy"})
            (folder / "A Series of Unfortunate Events (2017).mkv").write_bytes(b"")
            with patch.object(server, "REVIEW_DIR", root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                root,
            ):
                movies = server._review_payload(profile="movies")
                shows = server._review_payload(profile="series")

        self.assertEqual([item["name"] for item in movies["items"]], [folder.name])
        self.assertEqual(shows["items"], [])

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
