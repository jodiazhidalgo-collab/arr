import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from arr_orchestrator.config import Config
from arr_orchestrator.db import Database
from arr_orchestrator.engine import Engine, WORKER_ACTIVE_MAX_SECONDS
from arr_orchestrator.filebot import MOVE_PATTERN, is_duplicate_output
from arr_orchestrator.filesystem import (
    extraction_command_previews,
    matching_root,
    manifest,
    media_files,
    media_worker_source,
    move_job_to,
    move_job_to_review_clean,
    move_tv_job_to_review,
    move_trailer_package_into_job,
    prepare_filebot_input,
    top_level_item,
    trailer_package_manifest,
    trailer_ready_source,
    write_reason,
)
from arr_orchestrator.media_worker import (
    MediaWorkerClient,
    MediaWorkerError,
    MediaWorkerTransportError,
)
from arr_orchestrator.torrent import torrent_info
from arr_orchestrator.name_resolver import ResolvedIdentity, ResolverUnavailable
from arr_orchestrator.name_resolver import ResolverAmbiguous


def test_config(root: Path) -> Config:
    data = root / "data"
    complete = data / "downloads" / "torrents" / "complete"
    return Config(
        mode="active",
        config_dir=root / "config",
        data_root=data,
        watch_inbox=data / "torrents" / "watch" / "inbox",
        processed_root=data / "torrents" / "watch" / "processed",
        watch_error=data / "torrents" / "watch" / "error",
        event_dir=data / "torrents" / "events" / "inbox" / "qbt",
        complete_root=complete,
        workshop_root=complete / "taller",
        movies_output=complete / "movies_automatizacion",
        movies_final=data / "media" / "movies",
        tv_output=data / "media" / "tv",
        trailers_inbox=complete / "trailers_automatizacion",
        review_dir=data / "media" / "repetidas_vs_error",
        media_worker_url="http://media-worker:8790",
        callback_url="http://arr-orchestrator:8787",
        media_reports_root=root / "config" / "media-worker",
        codex_diag_root=root / "diagnosticos_codex",
        diagnostics_root=root / "diagnostics" / "arr",
        qbt_url="http://gluetun:8080",
        qbt_user="admin",
        qbt_password="",
        rdt_url="http://rdtclient:6500",
        rdt_user="admin",
        rdt_password="",
        stable_seconds=1,
        reconcile_seconds=30,
        fallback_seconds=5400,
        health_port=8787,
        filebot_bin="/opt/filebot/filebot",
        tmdb_api_token="",
        resolver_language="es-ES",
        resolver_region="ES",
        resolver_http_timeout_ms=2500,
        resolver_total_budget_ms=5000,
        resolver_retry_seconds=60,
    )


test_config.__test__ = False


class CoreTests(unittest.TestCase):
    def _run_tv_name_with_ambiguous_resolver(self, name: str, message: str):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        config = test_config(root)
        config.ensure_directories()
        database = Database(root / "test.db")
        database.initialize()
        engine = Engine(config, database)
        job_root = config.workshop_root / "job-tv-policy"
        original = job_root / "original"
        original.mkdir(parents=True)
        file_name = name if name.lower().endswith(".mkv") else f"{name}.mkv"
        source = original / file_name
        source.write_bytes(b"episode")
        job = database.create_job(
            "fs:tv:policy",
            "fs",
            "tv",
            file_name,
            state="ready_filebot",
            source_path=str(original),
            stage_path=str(job_root),
        )

        class AmbiguousResolver:
            enabled = True

            def resolve(self, _job, _input_root):
                raise ResolverAmbiguous(message, {"query": name})

            def output_matches(self, _identity, _names):
                return False

        class FakeFileBot:
            def __init__(self):
                self.calls = []

            def run(self, job_id, category, input_path, output_root):
                self.calls.append((job_id, category, input_path, output_root))
                source_file = media_files(input_path)[0]
                destination = output_root / "Serie" / "Season 01" / source_file.name
                destination.parent.mkdir(parents=True)
                destination.write_bytes(b"episode")
                source_file.unlink()
                return {
                    "exit_code": 0,
                    "moves": [{"source": str(source_file), "destination": str(destination)}],
                    "output_media": [str(destination)],
                    "duplicate": False,
                    "stdout_tail": "",
                }

        fake_filebot = FakeFileBot()
        engine.name_resolver = AmbiguousResolver()
        engine.filebot = fake_filebot

        engine._run_filebot(job)

        updated = database.get_job(job["job_id"])
        detail = database.job_detail(job["job_id"])
        calls = list(fake_filebot.calls)
        database.close()
        temporary.cleanup()
        return updated, detail, calls

    def _run_conflict_before_resolver(self, category: str, name: str):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        config = test_config(root)
        config.ensure_directories()
        database = Database(root / "test.db")
        database.initialize()
        engine = Engine(config, database)
        job_root = config.workshop_root / "job-conflict-policy"
        original = job_root / "original"
        original.mkdir(parents=True)
        file_name = name if name.lower().endswith(".mkv") else f"{name}.mkv"
        source = original / file_name
        source.write_bytes(b"media")
        job = database.create_job(
            f"fs:{category}:conflict-policy",
            "fs",
            category,
            file_name,
            state="ready_filebot",
            source_path=str(original),
            stage_path=str(job_root),
        )

        class ResolverMustNotRun:
            enabled = True

            def resolve(self, _job, _input_root):
                raise AssertionError("resolver must not run")

        class FileBotMustNotRun:
            def run(self, *_args, **_kwargs):
                raise AssertionError("filebot must not run")

        engine.name_resolver = ResolverMustNotRun()
        engine.filebot = FileBotMustNotRun()

        engine._run_filebot(job)

        updated = database.get_job(job["job_id"])
        detail = database.job_detail(job["job_id"])
        database.close()
        temporary.cleanup()
        return updated, detail

    def test_filebot_move_output_is_parsed(self) -> None:
        output = (
            "[MOVE] from [/input/Big Buck Bunny.mp4] "
            "to [/output/Big Buck Bunny (2008).mp4]"
        )
        self.assertEqual(
            MOVE_PATTERN.findall(output),
            [
                (
                    "/input/Big Buck Bunny.mp4",
                    "/output/Big Buck Bunny (2008).mp4",
                )
            ],
        )

    def test_filebot_skip_existing_is_duplicate(self) -> None:
        output = (
            "[SKIP] Skipped [/input/episode.mkv] because "
            "[/output/episode.mkv] already exists\n"
            "Processed 0 files\n"
            "Failure"
        )
        self.assertTrue(is_duplicate_output(output, []))

    def test_source_path_can_adopt_qbt_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "test.db")
            database.initialize()
            created = database.create_job(
                "fs:movies:Example",
                "fs",
                "movies",
                "Example",
                state="waiting_stable",
                source_path="/data/complete/movies/Example",
            )
            adopted = database.get_job_by_source_path("/data/complete/movies/Example")
            self.assertEqual(created["job_id"], adopted["job_id"])
            database.update_job(
                adopted["job_id"],
                infohash="abc123",
                qbt_hash="abc123",
                origin="qbt",
            )
            self.assertEqual(
                database.get_job_by_infohash("ABC123")["job_id"],
                created["job_id"],
            )
            database.transition(
                created["job_id"],
                "done",
                "test",
                "finished",
            )
            self.assertIsNone(database.get_active_job_by_infohash("abc123"))
            database.close()

    def test_reconcile_qbt_adopts_existing_fs_job_for_folder_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            item = config.complete_root / "movies" / "Wasabi"
            content = item / "Wasabi.avi"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")
            job = database.create_job(
                "fs:movies:wasabi",
                "fs",
                "movies",
                "Wasabi",
                state="waiting_stable",
                source_path=str(item),
            )

            class FakeQbt:
                def torrents(self, _torrent_filter):
                    return [
                        {
                            "hash": "abc123",
                            "category": "movies",
                            "name": "Wasabi",
                            "content_path": str(content),
                            "added_on": 123,
                        }
                    ]

            engine.qbt = FakeQbt()
            engine._reconcile_qbt()

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["qbt_hash"], "abc123")
            self.assertEqual(updated["infohash"], "abc123")
            self.assertEqual(updated["source_path"], str(item))
            self.assertEqual(len(database.latest_jobs()), 1)
            self.assertTrue(
                any(event["phase"] == "qbt" for event in database.job_detail(job["job_id"])["timeline"])
            )
            database.close()

    def test_qbt_event_uses_top_level_folder_as_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            item = config.complete_root / "movies" / "Wasabi"
            content = item / "Wasabi.avi"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")
            infohash = "a" * 40
            event_path = config.event_dir / "wasabi.event"
            event_path.write_text(f"hash={infohash}\n", encoding="utf-8")

            class FakeQbt:
                def torrent(self, _infohash):
                    return {
                        "hash": infohash,
                        "category": "movies",
                        "name": "Wasabi",
                        "content_path": str(content),
                        "progress": 1,
                        "completion_on": 123,
                        "added_on": 100,
                    }

            engine.qbt = FakeQbt()
            engine._handle_qbt_event(event_path)

            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["qbt_hash"], infohash)
            self.assertEqual(jobs[0]["source_path"], str(item))
            self.assertFalse(event_path.exists())
            database.close()

    def test_materialized_fs_job_adopts_qbt_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            item = config.complete_root / "movies" / "Wasabi"
            content = item / "Wasabi.avi"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")

            class FakeQbt:
                def torrents(self, _torrent_filter):
                    return [
                        {
                            "hash": "abc123",
                            "category": "movies",
                            "name": "Wasabi",
                            "content_path": str(content),
                            "added_on": 123,
                        }
                    ]

            engine.qbt = FakeQbt()
            engine._register_materialized("movies", item)

            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["origin"], "fs")
            self.assertEqual(jobs[0]["qbt_hash"], "abc123")
            self.assertEqual(jobs[0]["source_path"], str(item))
            database.close()

    def test_manifest_changes_with_file_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file_path = root / "movie.mkv"
            file_path.write_bytes(b"a")
            first, _ = manifest(root)
            file_path.write_bytes(b"ab")
            second, _ = manifest(root)
            self.assertNotEqual(first, second)

    def test_top_level_item(self) -> None:
        root = Path("/data/complete/movies")
        changed = root / "Movie" / "video.mkv"
        self.assertEqual(top_level_item(root, changed), root / "Movie")

    def test_complete_allowlist_ignores_workshop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete_root = root / "downloads" / "torrents" / "complete"
            roots = [
                complete_root / "movies",
                complete_root / "tv",
                complete_root / "manual",
                complete_root / "movies_automatizacion",
                complete_root / "trailers_automatizacion",
            ]
            movie = complete_root / "movies" / "Movie" / "video.mkv"
            movie.parent.mkdir(parents=True)
            movie.write_bytes(b"movie")
            self.assertEqual(matching_root(movie, roots), complete_root / "movies")

            workshop_file = complete_root / "taller" / "job-id" / "original" / "video.mkv"
            workshop_file.parent.mkdir(parents=True)
            workshop_file.write_bytes(b"movie")
            self.assertIsNone(matching_root(workshop_file, roots))

    def test_write_reason_creates_json_and_txt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "Movie"
            write_reason(
                destination,
                {"phase": "filebot", "reason": "destination_exists"},
                "Pelicula repetida.txt",
                ["FileBot indica que el destino ya existe."],
            )
            self.assertTrue((destination / "reason.json").exists())
            text = (destination / "Pelicula repetida.txt").read_text(encoding="utf-8")
            self.assertIn("Pelicula repetida", text)
            self.assertIn("destino ya existe", text)

    def test_move_job_to_uses_human_review_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review = root / "review"
            source = root / "job-id"
            source.mkdir()
            (source / "original.mkv").write_bytes(b"movie")
            (review / "El jinete pálido (1985)").mkdir(parents=True)

            destination = move_job_to(source, review, "El jinete pálido (1985)")

            self.assertEqual(destination.name, "El jinete pálido (1985) (1)")
            self.assertTrue((destination / "original.mkv").exists())

    def test_move_job_to_review_clean_flattens_original_movie_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review = root / "review"
            job_root = root / "taller" / "job-movie"
            source_dir = job_root / "original" / "Shelter In Place (2021) [2160p]"
            source_dir.mkdir(parents=True)
            movie = source_dir / "Shelter.In.Place.2021.2160p.mkv"
            movie.write_bytes(b"movie")

            destination = move_job_to_review_clean(
                job_root,
                review,
                "Shelter In Place (2021) [2160p]",
            )

            self.assertEqual(destination.name, "Shelter In Place (2021) [2160p]")
            self.assertFalse((destination / "original").exists())
            self.assertFalse((destination / "Shelter In Place (2021) [2160p]").exists())
            self.assertTrue((destination / movie.name).exists())
            self.assertFalse(job_root.exists())

    def test_move_job_to_review_clean_prefers_filebot_input_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review = root / "review"
            job_root = root / "taller" / "job-movie"
            original = job_root / "original"
            prepared = job_root / "filebot_input" / "Pelicula rara"
            original.mkdir(parents=True)
            prepared.mkdir(parents=True)
            (prepared / "Pelicula rara.mkv").write_bytes(b"movie")

            destination = move_job_to_review_clean(job_root, review, "Pelicula rara")

            self.assertFalse((destination / "original").exists())
            self.assertFalse((destination / "filebot_input").exists())
            self.assertTrue((destination / "Pelicula rara.mkv").exists())
            self.assertFalse(job_root.exists())

    def test_movie_duplicate_review_uses_clean_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = root / "taller" / "job-movie"
            source_dir = job_root / "original" / "Duplicada (2026)"
            source_dir.mkdir(parents=True)
            (source_dir / "Duplicada (2026).mkv").write_bytes(b"movie")

            destination = engine._move_duplicate_to_review(
                {"category": "movies", "name": "Duplicada (2026)"},
                job_root,
            )

            self.assertEqual(destination.name, "Duplicada (2026)")
            self.assertFalse((destination / "original").exists())
            self.assertTrue((destination / "Duplicada (2026).mkv").exists())
            database.close()

    def test_move_tv_job_to_review_uses_series_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review = root / "review"
            job_root = root / "taller" / "job-tv"
            source_dir = job_root / "original" / "Cuarto Milenio [HDTV 1080p][Cap.2139]"
            source_dir.mkdir(parents=True)
            source = source_dir / "Cuarto Milenio [HDTV 1080p][Cap.2139].mkv"
            source.write_bytes(b"episode")

            destination = move_tv_job_to_review(
                job_root,
                review,
                "Cuarto Milenio [HDTV 1080p][Cap.2139]",
            )

            self.assertEqual(destination.name, "Cuarto Milenio")
            self.assertFalse((destination / "original").exists())
            self.assertTrue((destination / "Season 21" / "Cuarto Milenio - S21E39.mkv").exists())
            self.assertFalse(job_root.exists())

    def test_move_tv_job_to_review_numbers_existing_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review = root / "review"
            (review / "Cuarto Milenio").mkdir(parents=True)
            job_root = root / "taller" / "job-tv"
            source_dir = job_root / "original" / "Cuarto Milenio [HDTV 1080p][Cap.2139]"
            source_dir.mkdir(parents=True)
            (source_dir / "Cuarto Milenio [HDTV 1080p][Cap.2139].mkv").write_bytes(b"episode")

            destination = move_tv_job_to_review(
                job_root,
                review,
                "Cuarto Milenio [HDTV 1080p][Cap.2139]",
            )

            self.assertEqual(destination.name, "Cuarto Milenio (1)")
            self.assertTrue((destination / "Season 21" / "Cuarto Milenio - S21E39.mkv").exists())

    def test_media_worker_source_ignores_technical_original_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / "original"
            movie = original / "Maridos en accion (2026)"
            technical = original / "_tecnico"
            movie.mkdir(parents=True)
            technical.mkdir()
            (technical / "nota.txt").write_text("x", encoding="utf-8")
            (movie / "Maridos en accion (2026).mkv").write_bytes(b"movie")

            self.assertEqual(media_worker_source(original), movie)

    def test_prepare_filebot_input_never_returns_original_root_for_loose_movie(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root = Path(temporary) / "taller" / "job-1"
            original = job_root / "original"
            original.mkdir(parents=True)
            movie = original / "Un padre en apuros 4Kwebrip2160.atomohd.li.mkv"
            movie.write_bytes(b"movie")

            prepared = prepare_filebot_input(original, job_root, movie.name)

            self.assertNotEqual(prepared, original)
            self.assertIn("filebot_input", prepared.parts)
            self.assertEqual(len(media_files(prepared)), 1)
            self.assertTrue((prepared / movie.name).exists())

    def test_prepare_filebot_input_keeps_normal_movie_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root = Path(temporary) / "taller" / "job-1"
            movie_dir = job_root / "original" / "El jinete palido (1985)"
            movie_dir.mkdir(parents=True)
            (movie_dir / "El jinete palido (1985).mkv").write_bytes(b"movie")

            prepared = prepare_filebot_input(job_root / "original", job_root, "El jinete palido (1985)")

            self.assertEqual(prepared, movie_dir)
            self.assertTrue((movie_dir / "El jinete palido (1985).mkv").exists())

    def test_prepare_filebot_input_wraps_extracted_archive_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root = Path(temporary) / "taller" / "job-1"
            extracted = job_root / "extracted"
            extracted.mkdir(parents=True)
            (extracted / "movie.mkv").write_bytes(b"movie")
            (extracted / "movie.srt").write_text("subtitle", encoding="utf-8")

            prepared = prepare_filebot_input(extracted, job_root, "Archive Movie 2026.zip")

            self.assertNotEqual(prepared, extracted)
            self.assertIn("filebot_input", prepared.parts)
            self.assertTrue((prepared / "movie.mkv").exists())
            self.assertTrue((prepared / "movie.srt").exists())

    def test_extraction_command_previews_archives_without_running_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root = Path(temporary) / "taller" / "job-extract"
            original = job_root / "original"
            original.mkdir(parents=True)
            archive = original / "movie.part1.rar"
            archive.write_bytes(b"fake archive")

            previews = extraction_command_previews(job_root)

            self.assertEqual(len(previews), 1)
            self.assertEqual(previews[0]["argv"][0], "unrar")
            self.assertEqual(previews[0]["archive"], str(archive))
            self.assertEqual(previews[0]["cwd"], str(job_root))
            self.assertEqual(previews[0]["timeout_sec"], 7200)

    def test_run_filebot_movies_uses_private_input_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-1"
            original = job_root / "original"
            original.mkdir(parents=True)
            source = original / "Un padre en apuros 4Kwebrip2160.atomohd.li.mkv"
            source.write_bytes(b"movie")
            job = database.create_job(
                "fs:movies:un-padre",
                "fs",
                "movies",
                source.name,
                state="ready_filebot",
                source_path=str(original),
                stage_path=str(job_root),
            )

            class FakeFileBot:
                calls = []

                def run(self, job_id, category, input_path, output_root):
                    self.calls.append((job_id, category, input_path, output_root))
                    source_file = media_files(input_path)[0]
                    destination = output_root / "Un padre en apuros (2026)" / "Un padre en apuros (2026).mkv"
                    destination.parent.mkdir(parents=True)
                    destination.write_bytes(b"movie")
                    source_file.unlink()
                    return {
                        "exit_code": 0,
                        "moves": [{"source": str(source_file), "destination": str(destination)}],
                        "output_media": [str(destination)],
                        "duplicate": False,
                        "stdout_tail": "",
                    }

            fake = FakeFileBot()
            engine.filebot = fake

            engine._run_filebot(job)

            _, category, input_path, output_root = fake.calls[0]
            updated = database.get_job(job["job_id"])
            detail = database.job_detail(job["job_id"])
            self.assertEqual(category, "movies")
            self.assertNotEqual(input_path, original)
            self.assertIn("filebot_input", input_path.parts)
            self.assertEqual(output_root, job_root / "filebot_output")
            self.assertEqual(updated["state"], "media_postprocess_ready")
            self.assertEqual(
                Path(updated["source_path"]),
                job_root / "filebot_output" / "Un padre en apuros (2026)",
            )
            self.assertNotIn("movies_automatizacion", updated["source_path"])
            command_events = [
                event for event in detail["timeline"]
                if event["phase"] == "filebot" and event["event_type"] == "command"
            ]
            self.assertEqual(len(command_events), 1)
            self.assertEqual(command_events[0]["structured"]["command_preview"]["mode"], "legacy_amc")
            self.assertIn("timeout_sec", command_events[0]["structured"])
            database.close()

    def test_media_worker_preview_exposes_endpoint_payload_and_timeout(self) -> None:
        client = MediaWorkerClient(
            "http://media-worker:8790",
            "http://arr-orchestrator:8787",
            timeout_seconds=123,
        )

        preview = client.preview_process_movie(
            "job-1",
            Path("/data/work/source"),
            Path("/data/media/movies"),
            Path("/data/media/repetidas_vs_error"),
            Path("/config/media-worker"),
        )

        self.assertEqual(preview["method"], "POST")
        self.assertEqual(preview["service"], "media-worker")
        self.assertEqual(preview["endpoint"], "/process-movie")
        self.assertEqual(preview["timeout_sec"], 123)
        self.assertEqual(preview["payload"]["job_id"], "job-1")
        self.assertEqual(preview["payload"]["callback_url"], "http://arr-orchestrator:8787/jobs/job-1/events")

    def test_run_media_postprocess_records_command_preview_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            source = root / "movie-ready"
            source.mkdir()
            job = database.create_job(
                "fs:movies:media-command",
                "fs",
                "movies",
                "Movie Ready",
                state="media_postprocess_ready",
                source_path=str(source),
                stage_path=str(config.workshop_root / "job-media"),
            )

            class FakeMediaWorker:
                def preview_process_movie(self, job_id, source_path, final_root, review_root, reports_root):
                    return {
                        "method": "POST",
                        "service": "media-worker",
                        "endpoint": "/process-movie",
                        "payload": {
                            "job_id": job_id,
                            "source_path": str(source_path),
                            "final_root": str(final_root),
                            "review_root": str(review_root),
                            "reports_root": str(reports_root),
                        },
                        "timeout_sec": 14400,
                    }

                def process_movie(self, *_args):
                    final_video = config.movies_final / "Movie Ready" / "Movie Ready.mkv"
                    final_video.parent.mkdir(parents=True)
                    final_video.write_bytes(b"movie")
                    return {
                        "status": "done",
                        "job_id": job["job_id"],
                        "final": {"final_video": str(final_video)},
                    }

            engine.media_worker = FakeMediaWorker()
            engine._run_media_postprocess(job)

            detail = database.job_detail(job["job_id"])
            command_events = [
                event for event in detail["timeline"]
                if event["phase"] == "media" and event["event_type"] == "command"
            ]
            self.assertEqual(len(command_events), 1)
            self.assertEqual(command_events[0]["structured"]["command_preview"]["endpoint"], "/process-movie")
            self.assertEqual(database.get_job(job["job_id"])["state"], "ready_cleanup")
            database.close()

    def test_worker_http_failure_finishes_movie_and_trailer_in_manual_review(self) -> None:
        for phase in ("media", "trailer"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = test_config(root)
                config.ensure_directories()
                database = Database(root / "test.db")
                database.initialize()
                engine = Engine(config, database)
                stage = config.workshop_root / f"job-{phase}"
                source = stage / "original"
                source.mkdir(parents=True)
                (source / "material.mkv").write_bytes(b"media")
                state = "media_postprocess_ready" if phase == "media" else "trailer_ready"
                job = database.create_job(
                    f"fs:{phase}:worker-http-error",
                    "fs",
                    "movies" if phase == "media" else "trailers_automatizacion",
                    f"Worker {phase}",
                    state=state,
                    source_path=str(source),
                    stage_path=str(stage),
                )

                class FailingWorker:
                    def process_movie(self, *_args):
                        raise MediaWorkerError(
                            "No existe la carpeta de media",
                            endpoint="/process-movie",
                            status_code=500,
                            error_code="media_source_missing",
                            result={"status": "error", "job_id": job["job_id"]},
                        )

                    def process_trailer(self, *_args):
                        raise MediaWorkerError(
                            "Fallo controlado de trailer",
                            endpoint="/process-trailer",
                            status_code=500,
                            error_code="trailer_worker_exception",
                            result={"status": "error", "job_id": job["job_id"]},
                        )

                engine.media_worker = FailingWorker()
                if phase == "media":
                    engine._run_media_postprocess(job)
                else:
                    engine._run_trailer(job)

                updated = database.get_job(job["job_id"])
                detail = database.job_detail(job["job_id"])
                self.assertEqual(updated["state"], "manual_review")
                self.assertNotIn(updated["state"], {"media_postprocess_running", "trailer_running"})
                self.assertTrue(source.exists())
                self.assertTrue(stage.exists())
                self.assertTrue(
                    any(
                        event["phase"] == phase and event["event_type"] == "error"
                        for event in detail["timeline"]
                    )
                )
                database.close()

    def test_worker_errors_are_sanitized_before_job_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            message = engine._safe_worker_error(
                f"{config.data_root}/private.mkv Authorization: Bearer hidden "
                "magnet:?xt=urn:btih:hidden "
                "download_url=https://private.local/file?token=hidden "
                "/home/user/private.mkv"
            )
            self.assertIn("<DATA>", message)
            self.assertNotIn("hidden", message)
            self.assertNotIn("magnet:?", message)
            self.assertNotIn("private.local", message)
            self.assertNotIn("/home/user", message)
            database.close()

    def test_transport_timeout_checks_active_job_without_second_post(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            stage = config.workshop_root / "job-active"
            source = stage / "original"
            source.mkdir(parents=True)
            job = database.create_job(
                "fs:movies:worker-active",
                "fs",
                "movies",
                "Worker Active",
                state="media_postprocess_ready",
                source_path=str(source),
                stage_path=str(stage),
            )

            class ActiveWorker:
                def __init__(self):
                    self.posts = 0
                    self.status_calls = 0

                def process_movie(self, *_args):
                    self.posts += 1
                    raise MediaWorkerTransportError(
                        "timeout",
                        endpoint="/process-movie",
                        status_code=None,
                        error_code="media_worker_timeout",
                        retryable=True,
                    )

                def job_status(self, *_args):
                    self.status_calls += 1
                    return {"status": "active", "job_id": job["job_id"]}

            worker = ActiveWorker()
            engine.media_worker = worker
            engine._run_media_postprocess(job)
            engine._process_job(database.get_job(job["job_id"]))

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "media_postprocess_running")
            self.assertEqual(updated["last_error_code"], "media_worker_active")
            self.assertEqual(worker.posts, 1)
            self.assertEqual(worker.status_calls, 1)
            database.close()

    def test_stale_active_worker_stops_safely_without_second_post(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            stage = config.workshop_root / "job-stale-active"
            source = stage / "original"
            source.mkdir(parents=True)
            (source / "material.mkv").write_bytes(b"media")
            job = database.create_job(
                "fs:movies:worker-stale-active",
                "fs",
                "movies",
                "Worker Stale Active",
                state="media_postprocess_running",
                source_path=str(source),
                stage_path=str(stage),
            )

            class StaleWorker:
                posts = 0

                def process_movie(self, *_args):
                    self.posts += 1
                    raise AssertionError("No debe repetirse el POST")

                def job_status(self, *_args):
                    return {
                        "status": "active",
                        "job_id": job["job_id"],
                        "started_at": time.time() - WORKER_ACTIVE_MAX_SECONDS - 1,
                    }

            worker = StaleWorker()
            engine.media_worker = worker
            engine._reconcile_running_worker(job, "media", recovery=True)

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "manual_review")
            self.assertEqual(updated["last_error_code"], "media_worker_active_timeout")
            self.assertEqual(worker.posts, 0)
            self.assertTrue((source / "material.mkv").exists())
            database.close()

    def test_bluray_timeout_active_preserves_workshop_without_second_post(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            stage = config.workshop_root / "job-bluray-active"
            source = stage / "original"
            source.mkdir(parents=True)
            (source / "disc.iso").write_bytes(b"bluray")
            job = database.create_job(
                "fs:movies:bluray-active",
                "fs",
                "movies",
                "Blu-ray Active",
                state="ready_filebot",
                source_path=str(source),
                stage_path=str(stage),
            )

            class ActiveBlurayWorker:
                def __init__(self):
                    self.posts = 0
                    self.status_calls = 0

                def normalize_bluray(self, *_args):
                    self.posts += 1
                    raise MediaWorkerTransportError(
                        "timeout",
                        endpoint="/normalize-bluray",
                        status_code=None,
                        error_code="media_worker_timeout",
                        retryable=True,
                    )

                def job_status(self, *_args):
                    self.status_calls += 1
                    if self.status_calls > 1:
                        normalized = source / "Movie.mkv"
                        normalized.write_bytes(b"movie")
                        return {
                            "status": "terminal",
                            "job_id": job["job_id"],
                            "result": {
                                "status": "normalized",
                                "job_id": job["job_id"],
                                "kind": "bluray",
                                "result_file": str(normalized),
                            },
                        }
                    return {
                        "status": "active",
                        "job_id": job["job_id"],
                        "started_at": time.time(),
                    }

            worker = ActiveBlurayWorker()
            engine.media_worker = worker
            result = engine._normalize_bluray_before_filebot(job, stage, source)
            self.assertIsNone(result)
            self.assertEqual(database.get_job(job["job_id"])["state"], "bluray_running")
            self.assertEqual(worker.posts, 1)
            self.assertEqual(worker.status_calls, 1)
            self.assertTrue((source / "disc.iso").exists())

            engine._worker_status_checked_at.pop(job["job_id"], None)
            engine._process_job(database.get_job(job["job_id"]))
            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "ready_filebot")
            self.assertEqual(Path(updated["source_path"]), source / "Movie.mkv")
            self.assertEqual(worker.posts, 1)
            self.assertEqual(worker.status_calls, 2)

            filebot_calls = []

            def run_filebot_once(current):
                filebot_calls.append(current["job_id"])
                database.transition(
                    current["job_id"],
                    "done",
                    "filebot",
                    "FileBot simulado terminado",
                )

            engine._run_filebot = run_filebot_once
            engine._process_job(database.get_job(job["job_id"]))
            engine._process_job(database.get_job(job["job_id"]))
            self.assertEqual(filebot_calls, [job["job_id"]])
            database.close()

    def test_bluray_status_unavailable_keeps_running_and_preserves_workshop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            stage = config.workshop_root / "job-bluray-unavailable"
            source = stage / "original"
            source.mkdir(parents=True)
            (source / "disc.iso").write_bytes(b"bluray")
            job = database.create_job(
                "fs:movies:bluray-unavailable",
                "fs",
                "movies",
                "Blu-ray Unavailable",
                state="bluray_running",
                source_path=str(source),
                stage_path=str(stage),
            )

            class UnavailableWorker:
                posts = 0

                def normalize_bluray(self, *_args):
                    self.posts += 1
                    raise AssertionError("No debe repetirse la normalización")

                def job_status(self, *_args):
                    raise MediaWorkerTransportError(
                        "sin conexión",
                        endpoint="/jobs/status",
                        status_code=None,
                        error_code="media_worker_transport_error",
                        retryable=True,
                    )

            worker = UnavailableWorker()
            engine.media_worker = worker
            engine._reconcile_bluray_running(job, recovery=True)

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "bluray_running")
            self.assertEqual(updated["last_error_code"], "bluray_worker_status_unavailable")
            self.assertEqual(worker.posts, 0)
            self.assertTrue((source / "disc.iso").exists())
            database.close()

    def test_bluray_unavailable_deadline_uses_immutable_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            stage = config.workshop_root / "job-bluray-deadline"
            source = stage / "original"
            source.mkdir(parents=True)
            (source / "disc.iso").write_bytes(b"bluray")
            job = database.create_job(
                "fs:movies:bluray-deadline",
                "fs",
                "movies",
                "Blu-ray Deadline",
                state="bluray_running",
                source_path=str(source),
                stage_path=str(stage),
            )

            class UnavailableWorker:
                def job_status(self, *_args):
                    raise MediaWorkerTransportError(
                        "sin conexión",
                        endpoint="/jobs/status",
                        status_code=None,
                        error_code="media_worker_transport_error",
                        retryable=True,
                    )

            engine.media_worker = UnavailableWorker()
            immutable_start = 1000.0
            engine._worker_started_at[job["job_id"]] = immutable_start
            with patch(
                "arr_orchestrator.engine.time.time",
                return_value=immutable_start + WORKER_ACTIVE_MAX_SECONDS - 1,
            ):
                engine._reconcile_bluray_running(job, recovery=True)
            self.assertEqual(database.get_job(job["job_id"])["state"], "bluray_running")

            with patch(
                "arr_orchestrator.engine.time.time",
                return_value=immutable_start + WORKER_ACTIVE_MAX_SECONDS + 1,
            ):
                engine._reconcile_bluray_running(
                    database.get_job(job["job_id"]),
                    recovery=True,
                )
            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "manual_review")
            self.assertEqual(updated["last_error_code"], "bluray_worker_active_timeout")
            self.assertTrue((source / "disc.iso").exists())
            database.close()

    def test_bluray_recovery_uses_terminal_result_without_second_post(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            stage = config.workshop_root / "job-bluray-terminal"
            source = stage / "original"
            source.mkdir(parents=True)
            normalized = source / "Movie.mkv"
            normalized.write_bytes(b"movie")
            job = database.create_job(
                "fs:movies:bluray-terminal",
                "fs",
                "movies",
                "Blu-ray Terminal",
                state="bluray_running",
                source_path=str(source),
                stage_path=str(stage),
                last_error_code="engine_exception",
            )
            reports = config.media_reports_root / job["job_id"]
            reports.mkdir(parents=True)
            (reports / "bluray_result.json").write_text(
                json.dumps(
                    {
                        "status": "normalized",
                        "job_id": job["job_id"],
                        "kind": "bluray",
                        "result_file": str(normalized),
                    }
                ),
                encoding="utf-8",
            )

            class WorkerMustNotRun:
                def normalize_bluray(self, *_args):
                    raise AssertionError("No debe repetirse la normalización")

                def job_status(self, *_args):
                    raise AssertionError("El resultado local es prioritario")

            engine.media_worker = WorkerMustNotRun()
            engine._recover_interrupted_jobs()

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "ready_filebot")
            self.assertIsNone(updated["last_error_code"])
            self.assertTrue(normalized.exists())
            database.close()

    def test_bluray_terminal_failure_moves_only_after_worker_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            stage = config.workshop_root / "job-bluray-failed"
            source = stage / "original"
            source.mkdir(parents=True)
            (source / "BDMV.txt").write_bytes(b"bluray")
            job = database.create_job(
                "fs:movies:bluray-failed",
                "fs",
                "movies",
                "Blu-ray Failed",
                state="bluray_running",
                source_path=str(source),
                stage_path=str(stage),
            )
            reports = config.media_reports_root / job["job_id"]
            reports.mkdir(parents=True)
            (reports / "bluray_result.json").write_text(
                json.dumps(
                    {
                        "status": "verification_failed",
                        "job_id": job["job_id"],
                        "kind": "bluray",
                        "reason": "fallo controlado",
                    }
                ),
                encoding="utf-8",
            )

            class WorkerMustNotRun:
                def job_status(self, *_args):
                    raise AssertionError("El resultado local es prioritario")

            engine.media_worker = WorkerMustNotRun()
            engine._recover_interrupted_jobs()

            updated = database.get_job(job["job_id"])
            review = Path(updated["stage_path"])
            self.assertEqual(updated["state"], "error_terminal")
            self.assertFalse(stage.exists())
            self.assertTrue((review / "BDMV.txt").exists())
            self.assertTrue((review / "Error de proceso.txt").exists())
            database.close()

    def test_recovery_uses_durable_movie_result_and_never_posts_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            stage = config.workshop_root / "job-durable"
            stage.mkdir(parents=True)
            final_video = config.movies_final / "Durable (2026)" / "Durable (2026).mkv"
            final_video.parent.mkdir(parents=True)
            final_video.write_bytes(b"movie")
            job = database.create_job(
                "fs:movies:worker-durable",
                "fs",
                "movies",
                "Durable (2026)",
                state="media_postprocess_running",
                source_path=str(stage / "original"),
                stage_path=str(stage),
                last_error_code="engine_exception",
                last_error_message="old error",
            )
            reports = config.media_reports_root / job["job_id"]
            reports.mkdir(parents=True)
            (reports / "media_result.json").write_text(
                json.dumps(
                    {
                        "status": "done",
                        "job_id": job["job_id"],
                        "final": {"final_video": str(final_video)},
                    }
                ),
                encoding="utf-8",
            )

            class WorkerMustNotRun:
                def process_movie(self, *_args):
                    raise AssertionError("Media Worker no debe repetirse")

                def job_status(self, *_args):
                    raise AssertionError("El resultado local es prioritario")

            engine.media_worker = WorkerMustNotRun()
            engine._recover_interrupted_jobs()
            first_event_count = len(database.job_detail(job["job_id"])["timeline"])
            engine._recover_interrupted_jobs()

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "ready_cleanup")
            self.assertIsNone(updated["last_error_code"])
            self.assertIsNone(updated["last_error_message"])
            self.assertEqual(len(database.job_detail(job["job_id"])["timeline"]), first_event_count)
            database.close()

    def test_invalid_done_from_file_or_status_never_cleans_workshop(self) -> None:
        for source_kind in ("file", "status"):
            with self.subTest(source_kind=source_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = test_config(root)
                config.ensure_directories()
                database = Database(root / "test.db")
                database.initialize()
                engine = Engine(config, database)
                stage = config.workshop_root / f"job-invalid-{source_kind}"
                source = stage / "original"
                source.mkdir(parents=True)
                (source / "only-copy.mkv").write_bytes(b"only material")
                job = database.create_job(
                    f"fs:movies:worker-invalid:{source_kind}",
                    "fs",
                    "movies",
                    "Invalid terminal",
                    state="media_postprocess_running",
                    source_path=str(source),
                    stage_path=str(stage),
                )
                invalid = {
                    "status": "done",
                    "job_id": job["job_id"],
                    "kind": "movie",
                    "final": {
                        "final_video": str(config.movies_final / "Missing" / "Missing.mkv")
                    },
                }
                if source_kind == "file":
                    reports = config.media_reports_root / job["job_id"]
                    reports.mkdir(parents=True)
                    (reports / "media_result.json").write_text(
                        json.dumps(invalid),
                        encoding="utf-8",
                    )

                class InvalidWorker:
                    def __init__(self):
                        self.status_calls = 0

                    def job_status(self, *_args):
                        self.status_calls += 1
                        return {
                            "status": "terminal",
                            "job_id": job["job_id"],
                            "result": invalid,
                        }

                worker = InvalidWorker()
                engine.media_worker = worker
                engine._recover_interrupted_jobs()

                updated = database.get_job(job["job_id"])
                self.assertEqual(updated["state"], "manual_review")
                self.assertEqual(updated["last_error_code"], "media_worker_invalid_terminal")
                self.assertTrue(stage.exists())
                self.assertTrue((source / "only-copy.mkv").exists())
                self.assertEqual(worker.status_calls, 0 if source_kind == "file" else 1)
                first_event_count = len(database.job_detail(job["job_id"])["timeline"])
                engine._reconcile_late_worker_results()
                self.assertEqual(
                    len(database.job_detail(job["job_id"])["timeline"]),
                    first_event_count,
                )
                database.close()

    def test_worker_terminal_paths_must_stay_inside_owned_roots(self) -> None:
        for status in ("done", "review"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = test_config(root)
                config.ensure_directories()
                database = Database(root / "test.db")
                database.initialize()
                engine = Engine(config, database)
                stage = config.workshop_root / f"job-outside-{status}"
                source = stage / "original"
                source.mkdir(parents=True)
                (source / "material.mkv").write_bytes(b"media")
                job = database.create_job(
                    f"fs:movies:worker-outside:{status}",
                    "fs",
                    "movies",
                    "Outside terminal",
                    state="media_postprocess_running",
                    source_path=str(source),
                    stage_path=str(stage),
                )
                outside = root / "outside"
                outside.mkdir()
                if status == "done":
                    delivered = outside / "movie.mkv"
                    delivered.write_bytes(b"movie")
                    result = {
                        "status": "done",
                        "job_id": job["job_id"],
                        "kind": "movie",
                        "final": {"final_video": str(delivered)},
                    }
                else:
                    result = {
                        "status": "review",
                        "job_id": job["job_id"],
                        "kind": "movie",
                        "review_path": str(outside),
                        "reason_file": "Error de proceso.txt",
                    }

                engine._apply_worker_result(job, "media", result, recovery=True)

                updated = database.get_job(job["job_id"])
                self.assertEqual(updated["state"], "manual_review")
                self.assertEqual(updated["last_error_code"], "media_worker_invalid_terminal")
                self.assertTrue((source / "material.mkv").exists())
                database.close()

    def test_recovery_without_worker_evidence_is_terminal_and_idempotent(self) -> None:
        for material_exists in (True, False):
            with self.subTest(material_exists=material_exists), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = test_config(root)
                config.ensure_directories()
                database = Database(root / "test.db")
                database.initialize()
                engine = Engine(config, database)
                stage = config.workshop_root / "job-inconclusive"
                source = stage / "original"
                if material_exists:
                    source.mkdir(parents=True)
                    (source / "movie.mkv").write_bytes(b"media")
                job = database.create_job(
                    f"fs:movies:worker-missing:{material_exists}",
                    "fs",
                    "movies",
                    "Worker Missing",
                    state="media_postprocess_running",
                    source_path=str(source),
                    stage_path=str(stage),
                )

                class MissingWorker:
                    def __init__(self):
                        self.posts = 0
                        self.status_calls = 0

                    def process_movie(self, *_args):
                        self.posts += 1
                        raise AssertionError("No debe repetirse el POST")

                    def job_status(self, *_args):
                        self.status_calls += 1
                        return {
                            "status": "not_found",
                            "error_code": "media_job_not_found",
                        }

                worker = MissingWorker()
                engine.media_worker = worker
                engine._recover_interrupted_jobs()
                first_event_count = len(database.job_detail(job["job_id"])["timeline"])
                engine._recover_interrupted_jobs()

                updated = database.get_job(job["job_id"])
                expected = "media_recovery_inconclusive" if material_exists else "media_recovery_source_missing"
                self.assertEqual(updated["state"], "manual_review")
                self.assertEqual(updated["last_error_code"], expected)
                self.assertEqual(worker.posts, 0)
                self.assertEqual(worker.status_calls, 1)
                self.assertEqual(len(database.job_detail(job["job_id"])["timeline"]), first_event_count)
                if material_exists:
                    self.assertTrue(source.exists())
                database.close()

    def test_recovery_reconciles_trailer_and_review_terminal_results(self) -> None:
        cases = ("trailer_done", "movie_review")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = test_config(root)
                config.ensure_directories()
                database = Database(root / "test.db")
                database.initialize()
                engine = Engine(config, database)
                stage = config.workshop_root / case
                source = stage / "original"
                source.mkdir(parents=True)
                phase = "trailer" if case == "trailer_done" else "media"
                state = "trailer_running" if phase == "trailer" else "media_postprocess_running"
                job = database.create_job(
                    f"fs:{case}:terminal",
                    "fs",
                    "trailers_automatizacion" if phase == "trailer" else "movies",
                    case,
                    state=state,
                    source_path=str(source),
                    stage_path=str(stage),
                )
                reports = config.media_reports_root / job["job_id"]
                reports.mkdir(parents=True)
                if phase == "trailer":
                    destination = config.movies_final / "Movie" / "trailer.mp4"
                    destination.parent.mkdir(parents=True)
                    destination.write_bytes(b"trailer")
                    result = {
                        "status": "done",
                        "job_id": job["job_id"],
                        "destination": str(destination),
                    }
                    filename = "trailer_result.json"
                else:
                    review = config.review_dir / "Movie Duplicate"
                    review.mkdir(parents=True)
                    result = {
                        "status": "review",
                        "job_id": job["job_id"],
                        "review_path": str(review),
                        "reason_file": str(review / "Pelicula repetida.txt"),
                    }
                    filename = "media_result.json"
                (reports / filename).write_text(json.dumps(result), encoding="utf-8")

                class WorkerMustNotRun:
                    def job_status(self, *_args):
                        raise AssertionError("No debe consultar con resultado durable")

                engine.media_worker = WorkerMustNotRun()
                engine._recover_interrupted_jobs()

                updated = database.get_job(job["job_id"])
                self.assertEqual(
                    updated["state"],
                    "ready_cleanup" if phase == "trailer" else "duplicate",
                )
                database.close()

    def test_foreign_result_cannot_close_job_and_late_result_can_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            stage = config.workshop_root / "job-foreign"
            source = stage / "original"
            source.mkdir(parents=True)
            job = database.create_job(
                "fs:movies:worker-foreign",
                "fs",
                "movies",
                "Foreign",
                state="media_postprocess_running",
                source_path=str(source),
                stage_path=str(stage),
            )
            reports = config.media_reports_root / job["job_id"]
            reports.mkdir(parents=True)
            final_video = config.movies_final / "Foreign" / "Foreign.mkv"
            final_video.parent.mkdir(parents=True)
            final_video.write_bytes(b"movie")
            result_path = reports / "media_result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "status": "done",
                        "job_id": "different-job",
                        "final": {"final_video": str(final_video)},
                    }
                ),
                encoding="utf-8",
            )

            class MissingWorker:
                def job_status(self, *_args):
                    return {"status": "not_found"}

            engine.media_worker = MissingWorker()
            engine._recover_interrupted_jobs()
            self.assertEqual(database.get_job(job["job_id"])["state"], "manual_review")
            first_event_count = len(database.job_detail(job["job_id"])["timeline"])
            engine._reconcile_late_worker_results()
            self.assertEqual(
                len(database.job_detail(job["job_id"])["timeline"]),
                first_event_count,
            )

            result_path.write_text(
                json.dumps(
                    {
                        "status": "done",
                        "job_id": job["job_id"],
                        "final": {"final_video": str(final_video)},
                    }
                ),
                encoding="utf-8",
            )
            engine._reconcile_late_worker_results()
            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "ready_cleanup")
            self.assertIsNone(updated["last_error_code"])
            database.close()

    def test_run_filebot_tv_keeps_tv_output_but_not_original_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-tv"
            original = job_root / "original"
            original.mkdir(parents=True)
            source = original / "Serie.S01E01.mkv"
            source.write_bytes(b"episode")
            job = database.create_job(
                "fs:tv:serie",
                "fs",
                "tv",
                source.name,
                state="ready_filebot",
                source_path=str(original),
                stage_path=str(job_root),
            )

            class FakeFileBot:
                calls = []

                def run(self, job_id, category, input_path, output_root):
                    self.calls.append((job_id, category, input_path, output_root))
                    source_file = media_files(input_path)[0]
                    destination = output_root / "Serie" / "Season 01" / "Serie - S01E01.mkv"
                    destination.parent.mkdir(parents=True)
                    destination.write_bytes(b"episode")
                    source_file.unlink()
                    return {
                        "exit_code": 0,
                        "moves": [{"source": str(source_file), "destination": str(destination)}],
                        "output_media": [str(destination)],
                        "duplicate": False,
                        "stdout_tail": "",
                    }

            fake = FakeFileBot()
            engine.filebot = fake

            engine._run_filebot(job)

            _, category, input_path, output_root = fake.calls[0]
            updated = database.get_job(job["job_id"])
            self.assertEqual(category, "tv")
            self.assertNotEqual(input_path, original)
            self.assertIn("filebot_input", input_path.parts)
            self.assertEqual(output_root, config.tv_output)
            self.assertEqual(updated["state"], "ready_cleanup")
            database.close()

    def test_guided_identity_is_passed_to_filebot_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-guided"
            original = job_root / "original"
            original.mkdir(parents=True)
            source = original / "Un padre en apuros 4Kwebrip2160.atomohd.li.mkv"
            source.write_bytes(b"movie")
            job = database.create_job(
                "fs:movies:guided",
                "fs",
                "movies",
                source.name,
                state="ready_filebot",
                source_path=str(original),
                stage_path=str(job_root),
            )
            identity = ResolvedIdentity(
                media_type="movie",
                tmdb_id=9279,
                title="Un padre en apuros",
                original_title="Jingle All the Way",
                year=1996,
                aliases=["Un padre en apuros", "Jingle All the Way"],
                score=100,
                margin=50,
                query="Un padre en apuros",
                guess={"title": "Un padre en apuros"},
                source="search",
            )

            class FakeResolver:
                enabled = True

                def resolve(self, _job, _input_root):
                    return identity

                def output_matches(self, _identity, names):
                    return names == ["Un padre en apuros (1996)"]

            class FakeFileBot:
                received_identity = None

                def run(self, _job_id, _category, input_path, output_root, resolved):
                    self.received_identity = resolved
                    source_file = media_files(input_path)[0]
                    destination = (
                        output_root
                        / "Un padre en apuros (1996)"
                        / "Un padre en apuros (1996).mkv"
                    )
                    destination.parent.mkdir(parents=True)
                    destination.write_bytes(b"movie")
                    source_file.unlink()
                    return {
                        "exit_code": 0,
                        "moves": [
                            {"source": str(source_file), "destination": str(destination)}
                        ],
                        "output_media": [str(destination)],
                        "duplicate": False,
                        "stdout_tail": "",
                    }

            fake_filebot = FakeFileBot()
            engine.name_resolver = FakeResolver()
            engine.filebot = fake_filebot

            engine._run_filebot(job)

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "media_postprocess_ready")
            self.assertEqual(fake_filebot.received_identity.tmdb_id, 9279)
            self.assertIn('"tmdb_id": 9279', updated["identity_json"])
            database.close()

    def test_wrong_filebot_identity_is_blocked_before_media_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-wrong"
            original = job_root / "original"
            original.mkdir(parents=True)
            source = original / "Un padre en apuros.mkv"
            source.write_bytes(b"movie")
            job = database.create_job(
                "fs:movies:wrong",
                "fs",
                "movies",
                source.name,
                state="ready_filebot",
                source_path=str(original),
                stage_path=str(job_root),
            )
            identity = ResolvedIdentity(
                media_type="movie",
                tmdb_id=9279,
                title="Un padre en apuros",
                original_title="Jingle All the Way",
                year=1996,
                aliases=["Un padre en apuros", "Jingle All the Way"],
                score=100,
                margin=50,
                query="Un padre en apuros",
                guess={},
                source="search",
            )

            class FakeResolver:
                enabled = True

                def resolve(self, _job, _input_root):
                    return identity

                def output_matches(self, _identity, _names):
                    return False

            class FakeFileBot:
                def run(self, _job_id, _category, input_path, output_root, _identity):
                    source_file = media_files(input_path)[0]
                    destination = (
                        output_root
                        / "El padre La venganza tiene un precio (2018)"
                        / "El padre La venganza tiene un precio (2018).mkv"
                    )
                    destination.parent.mkdir(parents=True)
                    destination.write_bytes(b"movie")
                    source_file.unlink()
                    return {
                        "exit_code": 0,
                        "moves": [
                            {"source": str(source_file), "destination": str(destination)}
                        ],
                        "output_media": [str(destination)],
                        "duplicate": False,
                        "stdout_tail": "",
                    }

            engine.name_resolver = FakeResolver()
            engine.filebot = FakeFileBot()

            engine._run_filebot(job)

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "manual_review")
            self.assertEqual(updated["last_error_code"], "filebot_identity_mismatch")
            self.assertNotEqual(updated["state"], "media_postprocess_ready")
            database.close()

    def test_tmdb_outage_waits_for_retry_instead_of_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-retry"
            original = job_root / "original"
            original.mkdir(parents=True)
            source = original / "Un padre en apuros.mkv"
            source.write_bytes(b"movie")
            job = database.create_job(
                "fs:movies:retry",
                "fs",
                "movies",
                source.name,
                state="ready_filebot",
                source_path=str(original),
                stage_path=str(job_root),
            )

            class OfflineResolver:
                enabled = True

                def resolve(self, _job, _input_root):
                    raise ResolverUnavailable("TMDb temporalmente caido")

            engine.name_resolver = OfflineResolver()

            engine._run_filebot(job)

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "identity_retry")
            self.assertEqual(updated["last_error_code"], "identity_unavailable")
            self.assertTrue(Path(updated["source_path"]).exists())
            database.close()

    def test_run_filebot_multiple_movie_outputs_goes_to_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-pack"
            original = job_root / "original"
            original.mkdir(parents=True)
            source = original / "Pack peliculas.mkv"
            source.write_bytes(b"movie")
            job = database.create_job(
                "fs:movies:pack",
                "fs",
                "movies",
                "Pack peliculas",
                state="ready_filebot",
                source_path=str(original),
                stage_path=str(job_root),
            )

            class FakeFileBot:
                def run(self, job_id, category, input_path, output_root):
                    media_files(input_path)[0].unlink()
                    first = output_root / "Pelicula A (2026)" / "Pelicula A (2026).mkv"
                    second = output_root / "Pelicula B (2026)" / "Pelicula B (2026).mkv"
                    first.parent.mkdir(parents=True)
                    second.parent.mkdir(parents=True)
                    first.write_bytes(b"a")
                    second.write_bytes(b"b")
                    return {
                        "exit_code": 0,
                        "moves": [
                            {"source": "a", "destination": str(first)},
                            {"source": "b", "destination": str(second)},
                        ],
                        "output_media": [str(first), str(second)],
                        "duplicate": False,
                        "stdout_tail": "",
                    }

            engine.filebot = FakeFileBot()

            engine._run_filebot(job)

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "manual_review")
            self.assertEqual(updated["last_error_code"], "multiple_movie_outputs")
            review_path = Path(updated["stage_path"])
            self.assertTrue((review_path / "reason.json").exists())
            self.assertTrue((review_path / "Revision manual.txt").exists())
            database.close()

    def test_trailer_package_waits_for_json_and_video(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary) / "trailers"
            inbox.mkdir()
            video = inbox / "ejecucion_inminente__abc.mkv"
            meta = inbox / "ejecucion_inminente__abc.json"
            video.write_bytes(b"trailer")

            self.assertIsNone(trailer_ready_source(video))
            self.assertIsNone(trailer_ready_source(meta))

            meta.write_text(
                '{"title":"Ejecucion inminente","year":"1999","video_file":"ejecucion_inminente__abc.mkv"}',
                encoding="utf-8",
            )

            self.assertEqual(trailer_ready_source(meta), meta)
            signature, entries = trailer_package_manifest(meta)
            self.assertNotEqual(signature, "missing")
            self.assertEqual({entry["path"] for entry in entries}, {video.name, meta.name})

    def test_move_trailer_package_into_job_moves_json_and_video_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inbox = root / "trailers"
            workshop = root / "taller"
            inbox.mkdir()
            video = inbox / "el_jinete_palido__abc.mkv"
            meta = inbox / "el_jinete_palido__abc.json"
            video.write_bytes(b"trailer")
            meta.write_text(
                '{"title":"El jinete palido","year":"1985","video_file":"el_jinete_palido__abc.mkv"}',
                encoding="utf-8",
            )

            job_root, package = move_trailer_package_into_job(meta, workshop, "job-1")

            self.assertEqual(job_root, workshop / "job-1")
            self.assertTrue((package / video.name).exists())
            self.assertTrue((package / meta.name).exists())
            self.assertFalse(video.exists())
            self.assertFalse(meta.exists())

    def test_delay_audio_temp_file_is_ignored_in_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            temp_file = (
                config.complete_root
                / "movies"
                / ".Pelicula.mkv.12345678.delay-audio-part"
            )
            temp_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file.write_bytes(b"partial")

            engine._handle_complete_path(temp_file)
            engine._reconcile_complete()

            self.assertEqual(database.latest_jobs(), [])
            database.close()

    def test_nested_delay_audio_temp_ignores_movies_folder_until_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)

            class FakeQbt:
                def torrents(self, _torrent_filter):
                    return []

            engine.qbt = FakeQbt()
            item = config.complete_root / "movies" / "Pelicula"
            video = item / "contenido" / "Pelicula.mkv"
            ignored = item / "temporales" / "audio" / "Pelicula.DELAY-AUDIO-PART"
            video.parent.mkdir(parents=True)
            ignored.parent.mkdir(parents=True)
            video.write_bytes(b"movie")
            ignored.write_bytes(b"partial")

            engine._handle_complete_path(ignored)
            engine._reconcile_complete()
            self.assertEqual(database.latest_jobs(), [])

            ignored.unlink()
            engine._handle_complete_path(ignored)
            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["source_path"], str(item))
            self.assertEqual(jobs[0]["state"], "waiting_stable")
            database.close()

    def test_watcher_rules_are_configurable_persistent_and_allow_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)

            result = engine.update_watcher_rules(
                {"rules": {"ignored_suffixes": [".CUSTOM", ".custom", "-final"]}}
            )
            self.assertTrue(result["saved"])
            self.assertEqual(result["rules"]["ignored_suffixes"], [".custom", "-final"])

            restarted = Engine(config, database)
            self.assertEqual(
                restarted.watcher_rules()["rules"]["ignored_suffixes"],
                [".custom", "-final"],
            )
            item = config.complete_root / "movies" / "Personalizada"
            blocked = item / "interior" / "archivo.CUSTOM"
            blocked.parent.mkdir(parents=True)
            blocked.write_bytes(b"partial")
            self.assertTrue(restarted._ignored_movies_item(item))

            emptied = restarted.update_watcher_rules({"rules": {"ignored_suffixes": []}})
            self.assertEqual(emptied["rules"]["ignored_suffixes"], [])
            empty_restarted = Engine(config, database)
            self.assertEqual(empty_restarted.watcher_rules()["rules"]["ignored_suffixes"], [])
            self.assertFalse(empty_restarted._ignored_movies_item(item))
            database.close()

    def test_watcher_rule_scope_is_only_complete_movies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)

            class FakeQbt:
                def torrents(self, _torrent_filter):
                    return []

            engine.qbt = FakeQbt()
            tv_item = config.complete_root / "tv" / "Serie"
            blocked = tv_item / "interior" / "episodio.delay-audio-part"
            blocked.parent.mkdir(parents=True)
            blocked.write_bytes(b"partial")

            engine._reconcile_complete()
            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["category"], "tv")
            self.assertEqual(jobs[0]["source_path"], str(tv_item))
            database.close()

    def test_late_nested_ignored_file_pauses_only_jobs_under_effective_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)

            old_item = config.complete_root / "movies" / "Anterior"
            old_item.mkdir(parents=True)
            old_job = database.create_job(
                "fs:movies:anterior",
                "fs",
                "movies",
                old_item.name,
                state="waiting_stable",
                source_path=str(old_item),
            )
            engine.update_watcher_rules({"rules": {"ignored_suffixes": [".bloqueo"]}})
            (old_item / "interior.bloqueo").write_bytes(b"partial")
            self.assertFalse(engine._ignored_movies_job(old_job, old_item))

            future_item = config.complete_root / "movies" / "Posterior"
            future_item.mkdir(parents=True)
            future_job = database.create_job(
                "fs:movies:posterior",
                "fs",
                "movies",
                future_item.name,
                state="waiting_stable",
                source_path=str(future_item),
            )
            ignored = future_item / "profundo" / "temporal.BLOQUEO"
            ignored.parent.mkdir(parents=True)
            ignored.write_bytes(b"partial")
            self.assertTrue(engine._ignored_movies_job(future_job, future_item))

            engine.update_watcher_rules({"rules": {"ignored_suffixes": [".otra"]}})
            self.assertTrue(engine._ignored_movies_job(future_job, future_item))
            restarted = Engine(config, database)
            self.assertTrue(restarted._ignored_movies_job(future_job, future_item))

            restarted._stable[str(future_job["job_id"])] = ("old", time.time() - 20)
            restarted._process_job(future_job)
            self.assertEqual(database.get_job(future_job["job_id"])["state"], "waiting_stable")
            self.assertNotIn(str(future_job["job_id"]), restarted._stable)

            ignored.unlink()
            restarted._process_job(database.get_job(future_job["job_id"]))
            self.assertIn(str(future_job["job_id"]), restarted._stable)
            database.close()

    def test_top_level_folder_name_keeps_previous_ignore_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            item = config.complete_root / "movies" / "Carpeta.delay-audio-part"
            item.mkdir(parents=True)
            (item / "Pelicula.mkv").write_bytes(b"movie")

            engine._reconcile_complete()

            self.assertEqual(database.latest_jobs(), [])
            database.close()

    def test_qbt_paths_ignore_nested_movie_rule_without_creating_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            item = config.complete_root / "movies" / "Qbt"
            content = item / "Qbt.mkv"
            ignored = item / "temp" / "Qbt.delay-audio-part"
            content.parent.mkdir(parents=True)
            ignored.parent.mkdir(parents=True)
            content.write_bytes(b"movie")
            ignored.write_bytes(b"partial")
            infohash = "b" * 40
            torrent = {
                "hash": infohash,
                "category": "movies",
                "name": "Qbt",
                "content_path": str(content),
                "progress": 1,
                "completion_on": 123,
                "added_on": 100,
            }

            class FakeQbt:
                def torrents(self, _torrent_filter):
                    return [torrent]

                def torrent(self, _infohash):
                    return torrent

            engine.qbt = FakeQbt()
            engine._reconcile_qbt()
            self.assertEqual(database.latest_jobs(), [])

            event_path = config.event_dir / "qbt.event"
            event_path.write_text(f"hash={infohash}\n", encoding="utf-8")
            engine._handle_qbt_event(event_path)
            self.assertEqual(database.latest_jobs(), [])
            self.assertFalse(event_path.exists())

            ignored.unlink()
            engine._reconcile_qbt()
            jobs = database.latest_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["source_path"], str(item))
            database.close()

    def test_terminal_codex_diagnostic_is_written_in_category_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job = database.create_job(
                "fs:movies:diagnostic",
                "fs",
                "movies",
                "Pelicula de prueba (2026).mkv",
                state="done",
                source_path="/data/media/movies/Pelicula de prueba (2026)",
            )
            database.add_event(
                job["job_id"],
                "cleanup",
                "finished",
                "Trabajo terminado",
            )

            engine._create_terminal_diagnostic(database.get_job(job["job_id"]))

            reports = list((config.codex_diag_root / "movies").glob("*_informe_codex.zip"))
            self.assertEqual(len(reports), 1)
            database.close()

    def test_review_codex_diagnostic_is_written_in_review_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job = database.create_job(
                "fs:tv:review-diagnostic",
                "fs",
                "tv",
                "La Agencia [Cap.201]",
                state="manual_review",
                source_path="/data/media/repetidas_vs_error/La Agencia [Cap.201]",
            )

            engine._create_terminal_diagnostic(database.get_job(job["job_id"]))

            reports = list((config.codex_diag_root / "repetidas_vs_error").glob("*_informe_codex.zip"))
            self.assertEqual(len(reports), 1)
            database.close()

    def test_category_uses_name_parser(self) -> None:
        self.assertEqual(
            Engine._category("", "La reina del flow S03 E53 (2026) NETFLIX.mkv"),
            "tv",
        )
        self.assertEqual(Engine._category("", "la reina del flow.3x41.1080.mkv"), "tv")
        self.assertEqual(
            Engine._category("", "Los Simpsons - Temporada 34 [Cap.3401]"), "tv"
        )
        self.assertEqual(Engine._category("", "Los Simpson T06"), "tv")
        self.assertEqual(
            Engine._category("", "Erase Una Vez En... Hollywood (2019).mkv"),
            "movies",
        )
        self.assertEqual(
            Engine._category(
                "",
                "Lynda - Scott Simpson - Compleat Course Collection ( Linux, Ubuntu, Shell, CLI..) [AhLaN]",
            ),
            "manual",
        )

    def test_tv_cap_101_continues_to_filebot_when_tmdb_returns_empty(self) -> None:
        updated, detail, calls = self._run_tv_name_with_ambiguous_resolver(
            "Satisfacion garantizada [HDTV 1080p][Cap.101]",
            "TMDb no devolvio candidatos",
        )

        self.assertEqual(updated["state"], "ready_cleanup")
        self.assertNotEqual(updated.get("last_error_code"), "identity_suspicious")
        self.assertEqual(calls[0][1], "tv")
        self.assertTrue(detail["decisions"])
        self.assertTrue(
            any(
                event["event_type"] == "warning"
                and "senal TV local" in event["message"]
                for event in detail["decisions"]
            )
        )

    def test_tv_cap_201_continues_to_filebot_when_identity_threshold_is_low(self) -> None:
        updated, detail, calls = self._run_tv_name_with_ambiguous_resolver(
            "La Agencia [4k 2160p][Cap.201]",
            "La identidad no supera el umbral de seguridad",
        )

        self.assertEqual(updated["state"], "ready_cleanup")
        self.assertNotEqual(updated.get("last_error_code"), "identity_suspicious")
        self.assertEqual(calls[0][1], "tv")
        self.assertTrue(detail["decisions"])

    def test_movies_category_with_cap_201_goes_to_manual_review_by_conflict(self) -> None:
        updated, detail = self._run_conflict_before_resolver(
            "movies",
            "La Agencia [4k 2160p][Cap.201]",
        )

        self.assertEqual(updated["state"], "manual_review")
        self.assertEqual(updated["last_error_code"], "category_conflict")
        self.assertTrue(detail["decisions"])

    def test_tv_category_with_clear_movie_year_goes_to_manual_review_by_conflict(self) -> None:
        updated, detail = self._run_conflict_before_resolver(
            "tv",
            "Erase Una Vez En... Hollywood (2019).mkv",
        )

        self.assertEqual(updated["state"], "manual_review")
        self.assertEqual(updated["last_error_code"], "category_conflict")
        self.assertTrue(detail["decisions"])

    def test_category_conflict_goes_to_manual_review_before_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-conflict"
            original = job_root / "original"
            original.mkdir(parents=True)
            source = original / "Los Simpsons - Temporada 34 [Cap.3401].mkv"
            source.write_bytes(b"episode")
            job = database.create_job(
                "fs:movies:conflict",
                "fs",
                "movies",
                "Los Simpsons - Temporada 34 [Cap.3401]",
                state="ready_filebot",
                source_path=str(original),
                stage_path=str(job_root),
            )

            class ResolverMustNotRun:
                enabled = True

                def resolve(self, _job, _input_root):
                    raise AssertionError("resolver must not run")

            class FileBotMustNotRun:
                def run(self, *_args, **_kwargs):
                    raise AssertionError("filebot must not run")

            engine.name_resolver = ResolverMustNotRun()
            engine.filebot = FileBotMustNotRun()

            engine._run_filebot(job)

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "manual_review")
            self.assertEqual(updated["last_error_code"], "category_conflict")
            review_path = Path(updated["stage_path"])
            self.assertTrue((review_path / "reason.json").exists())
            database.close()

    def test_run_extract_keeps_direct_mkv_flow_ready_for_filebot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-direct-mkv"
            original = job_root / "original"
            original.mkdir(parents=True)
            (original / "Movie Direct (2026).mkv").write_bytes(b"movie")
            job = database.create_job(
                "fs:movies:direct-mkv",
                "fs",
                "movies",
                "Movie Direct (2026).mkv",
                state="ready_extract",
                stage_path=str(job_root),
                source_path=str(original),
            )

            engine._run_extract(job)

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "ready_filebot")
            self.assertEqual(Path(updated["source_path"]), original)
            self.assertFalse((job_root / "extracted" / "layer_01").exists())
            database.close()

    def test_recover_interrupted_extracting_job_returns_to_ready_extract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-recovery-extract"
            original = job_root / "original"
            original.mkdir(parents=True)
            (original / "movie.rar").write_bytes(b"archive")
            job = database.create_job(
                "fs:movies:recover-extract",
                "fs",
                "movies",
                "Movie Recovery.rar",
                state="extracting",
                stage_path=str(job_root),
                source_path=str(original),
            )

            engine._recover_interrupted_jobs()

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["state"], "ready_extract")
            recovery_events = [
                event
                for event in database.job_detail(job["job_id"])["timeline"]
                if event["phase"] == "recovery"
            ]
            self.assertEqual(len(recovery_events), 1)
            database.close()

    def test_filebot_rules_are_snapshotted_per_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)

            before = json.loads(engine._new_job_source_meta_json())["filebot_rules"]
            changed = engine.filebot_rules()["rules"]
            changed["movies"]["filename_style"] = "title_year_quality"
            saved = engine.update_filebot_rules(
                {"rules": changed, "expected_revision": 0}
            )
            after = engine._active_filebot_rules_context()
            old_job_context = engine._filebot_rules_for_job(
                {"source_meta_json": json.dumps({"filebot_rules": before})}
            )

            self.assertTrue(saved["ok"])
            self.assertEqual(before["revision"], 0)
            self.assertEqual(old_job_context["revision"], 0)
            self.assertEqual(
                old_job_context["rules"]["movies"]["filename_style"], "title_year"
            )
            self.assertEqual(after["revision"], 1)
            self.assertEqual(
                after["rules"]["movies"]["filename_style"], "title_year_quality"
            )
            database.close()

    def test_movie_filebot_timeout_finishes_in_review_with_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-timeout-movie"
            original = job_root / "original"
            original.mkdir(parents=True)
            source = original / "Timeout Movie (2026).mkv"
            source.write_bytes(b"movie")
            job = database.create_job(
                "fs:movies:timeout",
                "fs",
                "movies",
                "Timeout Movie (2026)",
                state="ready_filebot",
                stage_path=str(job_root),
                source_path=str(original),
            )

            class TimeoutFileBot:
                def configure_rules(self, rules):
                    self.rules = rules

                def run(self, _job_id, _category, _input_path, output_root):
                    partial = output_root / "Timeout Movie (2026)" / "Timeout Movie (2026).mkv"
                    partial.parent.mkdir(parents=True, exist_ok=True)
                    partial.write_bytes(b"partial")
                    return {
                        "exit_code": 124,
                        "moves": [],
                        "output_media": [str(partial)],
                        "duplicate": False,
                        "timed_out": True,
                        "timeout_message": "FileBot agoto el timeout de prueba",
                        "stdout_tail": "timeout",
                    }

            engine.filebot = TimeoutFileBot()
            engine._run_filebot(job)

            updated = database.get_job(job["job_id"])
            review = Path(updated["stage_path"])
            self.assertEqual(updated["state"], "manual_review")
            self.assertEqual(updated["last_error_code"], "filebot_timeout")
            self.assertTrue((review / "reason.json").exists())
            self.assertTrue(any(path.name.endswith(".mkv") for path in review.rglob("*.mkv")))
            self.assertEqual(updated["last_error_message"], "FileBot agoto el timeout de prueba")
            database.close()

    def test_tv_filebot_timeout_quarantines_identity_matched_unlogged_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "test.db")
            database.initialize()
            engine = Engine(config, database)
            job_root = config.workshop_root / "job-timeout-tv"
            original = job_root / "original"
            original.mkdir(parents=True)
            source = original / "Timeout Show S01E10.mkv"
            source.write_bytes(b"episode")
            job = database.create_job(
                "fs:tv:timeout",
                "fs",
                "tv",
                "Timeout Show S01E10",
                state="ready_filebot",
                stage_path=str(job_root),
                source_path=str(original),
            )
            destination = (
                config.tv_output
                / "Timeout Show"
                / "Season 01"
                / "Timeout Show - S01E10.mkv"
            )
            unrelated_destination = (
                config.tv_output
                / "Timeout Show"
                / "Season 01"
                / "Timeout Show - S01E100.mkv"
            )
            resolved = ResolvedIdentity(
                media_type="tv",
                tmdb_id=999,
                title="Timeout Show",
                original_title="Timeout Show",
                year=2026,
                aliases=["Timeout Show"],
                score=100,
                margin=20,
                query="Timeout Show",
                guess={"title": "Timeout Show", "season": 1, "episode": 10},
                source="test",
                season=1,
                episodes=[10],
            )

            class TimeoutFileBot:
                def configure_rules(self, _rules):
                    return None

                def run(self, _job_id, _category, input_path, _output_root):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(b"moved")
                    unrelated_destination.write_bytes(b"other-job")
                    input_path.joinpath("Timeout Show S01E10.mkv").unlink(missing_ok=True)
                    return {
                        "exit_code": 124,
                        "moves": [],
                        "output_media": [
                            str(destination),
                            str(unrelated_destination),
                        ],
                        "duplicate": False,
                        "timed_out": True,
                        "timeout_message": "FileBot agoto el timeout de prueba",
                        "stdout_tail": "timeout",
                        "identity": resolved.to_dict(),
                    }

            engine.filebot = TimeoutFileBot()
            engine._run_filebot(job)

            updated = database.get_job(job["job_id"])
            review = Path(updated["stage_path"])
            self.assertEqual(updated["state"], "manual_review")
            self.assertFalse(destination.exists())
            self.assertTrue(unrelated_destination.exists())
            self.assertEqual(unrelated_destination.read_bytes(), b"other-job")
            recovered = list(review.rglob("Timeout Show - S01E10.mkv"))
            self.assertEqual(len(recovered), 1)
            self.assertEqual(list(review.rglob("Timeout Show - S01E100.mkv")), [])
            self.assertIn("filebot_rejected", recovered[0].parts)
            database.close()

    def test_real_torrent_fixture_when_available(self) -> None:
        fixture = Path(tempfile.gettempdir()) / "arr-test-big-buck-bunny.torrent"
        fixture.write_bytes(
            b"d4:infod6:lengthi1e4:name14:Big Buck Bunny"
            b"12:piece lengthi16384e6:pieces20:01234567890123456789ee"
        )
        infohash, name = torrent_info(fixture)
        self.assertEqual(infohash, "3dfceb0ea9b3a32ec304238cad0b63e704f692d6")
        self.assertEqual(name, "Big Buck Bunny")
if __name__ == "__main__":
    unittest.main()
