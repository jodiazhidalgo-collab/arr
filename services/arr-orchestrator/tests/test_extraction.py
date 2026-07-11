import json
import os
import shutil
import tempfile
import unittest
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

from arr_orchestrator.arr_blackbox import ArrBlackbox
from arr_orchestrator.codex_diagnostics import create_codex_diagnostic
from arr_orchestrator.config import Config
from arr_orchestrator.db import Database
from arr_orchestrator.engine import Engine
from arr_orchestrator.filesystem import (
    EXTRACTION_SPACE_RESERVE_BYTES,
    EXTRACTION_LAYER_MARKER,
    MAX_ARCHIVE_CANDIDATES_PER_LAYER,
    ExtractionError,
    archive_candidates,
    extract_archives,
    media_files,
    move_extraction_failure_to_review,
    prepare_filebot_input,
)


def _command_paths(command: list[str]) -> tuple[Path, Path]:
    if command[0] == "unrar":
        return Path(command[-2]), Path(command[-1])
    output = next(value[2:] for value in command if value.startswith("-o"))
    return Path(command[-1]), Path(output)


def _completed(command: list[str], returncode: int = 0, output: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(command, returncode, output, "" if returncode == 0 else output)


class ArchiveCandidateTests(unittest.TestCase):
    def _candidates(self, names: list[str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"archive")
            return [path.name for path in archive_candidates(root)]

    def test_simple_rar_is_selected(self) -> None:
        self.assertEqual(self._candidates(["movie.rar"]), ["movie.rar"])

    def test_multipart_first_volume_variants_are_selected(self) -> None:
        for name in ("movie.part1.rar", "movie.part01.rar", "movie.part001.rar"):
            with self.subTest(name=name):
                self.assertEqual(self._candidates([name]), [name])

    def test_later_multipart_volumes_are_never_selected(self) -> None:
        self.assertEqual(
            self._candidates(["movie.part02.rar", "movie.part03.rar", "movie.part09.rar"]),
            [],
        )

    def test_two_independent_multipart_sets_only_select_their_first_volumes(self) -> None:
        names = [
            "alpha.part01.rar",
            "alpha.part02.rar",
            "beta.part001.rar",
            "beta.part002.rar",
        ]
        self.assertEqual(
            self._candidates(names),
            ["alpha.part01.rar", "beta.part001.rar"],
        )

    def test_old_style_rar_set_only_selects_rar_entry(self) -> None:
        self.assertEqual(
            self._candidates(["movie.rar", "movie.r00", "movie.r01"]),
            ["movie.rar"],
        )

    def test_zip_7z_and_split_001_are_selected(self) -> None:
        self.assertEqual(
            self._candidates(["a.zip", "b.7z", "c.001"]),
            ["a.zip", "b.7z", "c.001"],
        )

    def test_input_without_archives_has_no_candidates(self) -> None:
        self.assertEqual(self._candidates(["movie.mkv", "readme.txt"]), [])


class LayeredExtractionTests(unittest.TestCase):
    def _job(self, root: Path) -> tuple[Path, Path]:
        job_root = root / "taller" / "job-extract"
        original = job_root / "original"
        original.mkdir(parents=True)
        return job_root, original

    def test_direct_media_returns_original_without_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "movie.mkv").write_bytes(b"movie")
            with patch("arr_orchestrator.filesystem.subprocess.run") as run:
                result = extract_archives(job_root)
            self.assertEqual(result, original)
            run.assert_not_called()

    def test_outer_rar_to_multipart_to_media_uses_two_atomic_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "outer.rar").write_bytes(b"outer")

            def fake_run(command, **_kwargs):
                archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                if archive.name == "outer.rar":
                    (output / "inner.part01.rar").write_bytes(b"part1")
                    (output / "inner.part02.rar").write_bytes(b"part2")
                else:
                    (output / "movie.mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run) as run:
                result = extract_archives(job_root)

            self.assertEqual(result, job_root / "extracted" / "layer_02")
            self.assertEqual(run.call_count, 2)
            for layer in (1, 2):
                layer_root = job_root / "extracted" / f"layer_{layer:02d}"
                self.assertTrue((layer_root / EXTRACTION_LAYER_MARKER).is_file())
                self.assertFalse(layer_root.with_name(layer_root.name + ".tmp").exists())

    def test_three_layers_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "outer.rar").write_bytes(b"outer")

            outputs = {
                "outer.rar": "middle.zip",
                "middle.zip": "inner.7z",
                "inner.7z": "movie.mkv",
            }

            def fake_run(command, **_kwargs):
                archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / outputs[archive.name]).write_bytes(b"content")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run):
                result = extract_archives(job_root)
            self.assertEqual(result, job_root / "extracted" / "layer_03")
            self.assertEqual(len(media_files(result)), 1)

    def test_fourth_layer_is_blocked_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "outer.rar").write_bytes(b"outer")
            outputs = {"outer.rar": "a.zip", "a.zip": "b.7z", "b.7z": "c.rar"}

            def fake_run(command, **_kwargs):
                archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / outputs[archive.name]).write_bytes(b"content")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run):
                with self.assertRaises(ExtractionError) as raised:
                    extract_archives(job_root)
            self.assertEqual(raised.exception.code, "extract_depth_limit")
            self.assertFalse((job_root / "extracted" / "layer_04").exists())

    def test_multiple_independent_archives_use_separate_deterministic_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "alpha.rar").write_bytes(b"alpha")
            (original / "beta.zip").write_bytes(b"beta")

            def fake_run(command, **_kwargs):
                archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / f"{archive.stem}.mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run):
                result = extract_archives(job_root)
            videos = media_files(result)
            self.assertEqual(len(videos), 2)
            self.assertNotEqual(videos[0].parent, videos[1].parent)
            self.assertTrue(all("__" in video.parent.name for video in videos))

    def test_incomplete_tmp_is_removed_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "movie.rar").write_bytes(b"archive")
            stale = job_root / "extracted" / "layer_01.tmp" / "stale.bin"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale")

            def fake_run(command, **_kwargs):
                _archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / "movie.mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run):
                result = extract_archives(job_root)
            self.assertFalse(stale.exists())
            self.assertEqual(len(media_files(result)), 1)

    def test_valid_marker_reuses_layer_without_duplicate_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "movie.rar").write_bytes(b"archive")

            def fake_run(command, **_kwargs):
                _archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / "movie.mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run) as run:
                first = extract_archives(job_root)
                second = extract_archives(job_root)
            self.assertEqual(first, second)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(media_files(second)), 1)

    def test_changed_input_invalidates_layer_and_later_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            archive = original / "movie.rar"
            archive.write_bytes(b"v1")

            def fake_run(command, **_kwargs):
                _archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / "movie.mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run) as run:
                extract_archives(job_root)
                archive.write_bytes(b"version-two")
                result = extract_archives(job_root)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(len(media_files(result)), 1)

    def test_archive_that_produces_no_media_or_nested_archive_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "empty.zip").write_bytes(b"archive")

            def fake_run(command, **_kwargs):
                _archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / "readme.txt").write_text("empty", encoding="utf-8")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run):
                with self.assertRaises(ExtractionError) as raised:
                    extract_archives(job_root)
            self.assertEqual(raised.exception.code, "extract_no_media")

    def test_too_many_archive_candidates_are_rejected_before_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            for index in range(MAX_ARCHIVE_CANDIDATES_PER_LAYER + 1):
                (original / f"archive-{index:02d}.zip").write_bytes(b"zip")
            with patch("arr_orchestrator.filesystem.subprocess.run") as run:
                with self.assertRaises(ExtractionError) as raised:
                    extract_archives(job_root)
            self.assertEqual(raised.exception.code, "extract_too_many_candidates")
            self.assertEqual(
                raised.exception.details["candidate_count"],
                MAX_ARCHIVE_CANDIDATES_PER_LAYER + 1,
            )
            run.assert_not_called()

    def test_insufficient_space_uses_all_multipart_volumes_in_required_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "movie.part01.rar").write_bytes(b"1234")
            (original / "movie.part02.rar").write_bytes(b"5678")
            disk_usage = type("DiskUsage", (), {"free": EXTRACTION_SPACE_RESERVE_BYTES})()
            events: list[dict[str, object]] = []
            with patch("arr_orchestrator.filesystem.shutil.disk_usage", return_value=disk_usage):
                with self.assertRaises(ExtractionError) as raised:
                    extract_archives(
                        job_root,
                        event_callback=lambda kind, message, data: events.append(
                            {"type": kind, "message": message, "structured": data}
                        ),
                    )
            self.assertEqual(raised.exception.code, "extract_no_space")
            self.assertEqual(raised.exception.details["input_size_bytes"], 8)
            self.assertGreater(
                raised.exception.details["required_space_bytes"],
                raised.exception.details["available_space_bytes"],
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "command")
            self.assertEqual(
                events[0]["structured"]["required_space_bytes"],
                raised.exception.details["required_space_bytes"],
            )

    def test_total_timeout_budget_is_checked_before_launching_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "movie.rar").write_bytes(b"archive")
            with patch("arr_orchestrator.filesystem.subprocess.run") as run:
                with self.assertRaises(ExtractionError) as raised:
                    extract_archives(job_root, total_timeout_seconds=0)
            self.assertEqual(raised.exception.code, "extract_timeout")
            self.assertEqual(raised.exception.details["timeout_scope"], "total")
            run.assert_not_called()

    def test_command_timeout_is_classified_and_stdin_is_never_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "movie.rar").write_bytes(b"archive")

            def timeout_run(command, **kwargs):
                self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
                self.assertIn("-p-", command)
                self.assertIn("-y", command)
                raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="partial")

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=timeout_run):
                with self.assertRaises(ExtractionError) as raised:
                    extract_archives(job_root, timeout_seconds=5, total_timeout_seconds=10)
            self.assertEqual(raised.exception.code, "extract_timeout")
            self.assertEqual(raised.exception.details["timeout_scope"], "command")
            self.assertEqual(raised.exception.details["output_tail"], "partial")

    def test_remaining_total_budget_caps_per_command_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "movie.zip").write_bytes(b"archive")
            observed: dict[str, object] = {}

            def fake_run(command, **kwargs):
                observed.update(kwargs)
                _archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / "movie.mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run):
                extract_archives(job_root, timeout_seconds=7200, total_timeout_seconds=5)
            self.assertGreater(float(observed["timeout"]), 0)
            self.assertLessEqual(float(observed["timeout"]), 5)
            self.assertIs(observed["stdin"], subprocess.DEVNULL)

    def test_7z_command_disables_password_prompt_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "movie.7z").write_bytes(b"archive")
            observed: list[str] = []

            def fake_run(command, **_kwargs):
                observed.extend(command)
                _archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / "movie.mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run):
                extract_archives(job_root)
            self.assertIn("-p-", observed)

    def test_tool_output_is_classified_into_specific_error_codes(self) -> None:
        cases = {
            "Enter password for encrypted file": "extract_password_required",
            "Cannot find volume movie.part02.rar": "extract_volume_missing",
            "CRC failed in movie.mkv": "extract_archive_corrupt",
            "unknown tool failure": "extract_tool_failed",
        }
        for output_text, expected_code in cases.items():
            with self.subTest(expected_code=expected_code):
                with tempfile.TemporaryDirectory() as temporary:
                    job_root, original = self._job(Path(temporary))
                    (original / "movie.rar").write_bytes(b"archive")
                    with patch(
                        "arr_orchestrator.filesystem.subprocess.run",
                        return_value=_completed(["unrar"], returncode=2, output=output_text),
                    ):
                        with self.assertRaises(ExtractionError) as raised:
                            extract_archives(job_root)
                self.assertEqual(raised.exception.code, expected_code)
                for key in (
                    "extract_layer",
                    "archive",
                    "tool",
                    "return_code",
                    "classified_reason",
                    "output_tail",
                    "input_root",
                    "output_root",
                    "duration_sec",
                ):
                    self.assertIn(key, raised.exception.details)

    def test_missing_extraction_tool_is_classified_without_masking_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "movie.7z").write_bytes(b"archive")
            with patch(
                "arr_orchestrator.filesystem.subprocess.run",
                side_effect=FileNotFoundError("7z not found"),
            ):
                with self.assertRaises(ExtractionError) as raised:
                    extract_archives(job_root)
            self.assertEqual(raised.exception.code, "extract_tool_failed")
            self.assertEqual(raised.exception.details["tool"], "7z")
            self.assertIsNone(raised.exception.details["return_code"])

    def test_nested_layer_two_is_prepared_for_filebot_like_extracted_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "outer.rar").write_bytes(b"outer")

            def fake_run(command, **_kwargs):
                archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                if archive.name == "outer.rar":
                    (output / "inner.part01.rar").write_bytes(b"part1")
                    (output / "inner.part02.rar").write_bytes(b"part2")
                else:
                    (output / "The Batman (2022).mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run):
                layer_two = extract_archives(job_root)
            prepared = prepare_filebot_input(layer_two, job_root, "The Batman (2022)")

            self.assertEqual(layer_two.name, "layer_02")
            self.assertIn("filebot_input", prepared.parts)
            self.assertNotEqual(prepared, layer_two)
            self.assertTrue((prepared / "The Batman (2022).mkv").is_file())
            self.assertFalse((prepared / EXTRACTION_LAYER_MARKER).exists())

    def test_layer_events_reach_database_live_trace_and_codex_report(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            env = {
                "ARR_MODE": "active",
                "ARR_CONFIG_DIR": str(root / "config"),
                "ARR_DATA_ROOT": str(root / "data"),
                "ARR_WORKSHOP_ROOT": str(root / "workshop"),
                "ARR_REVIEW_DIR": str(root / "review"),
                "CODEX_DIAG_ROOT": str(root / "diagnosticos_codex"),
                "ARR_DIAGNOSTICS_ROOT": str(root / "diagnostics" / "arr"),
                "TMDB_API_TOKEN": "",
            }
            with patch.dict(os.environ, env, clear=False):
                config = Config.from_env()
            config.ensure_directories()
            blackbox = ArrBlackbox(config.diagnostics_root)
            database = Database(config.db_path, event_recorder=blackbox.record_event)
            database.initialize()
            self.addCleanup(database.close)
            job_root = config.workshop_root / "job-extract-events"
            original = job_root / "original"
            original.mkdir(parents=True)
            (original / "movie.zip").write_bytes(b"archive")
            job = database.create_job(
                "fs:movies:extract-events",
                "fs",
                "movies",
                "Movie Events (2026).zip",
                state="ready_extract",
                stage_path=str(job_root),
                source_path=str(original),
            )
            engine = Engine(config, database)

            def fake_run(command, **_kwargs):
                _archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / "Movie Events (2026).mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run):
                engine._run_extract(job)

            detail = database.job_detail(job["job_id"])
            extract_events = [event for event in detail["timeline"] if event["phase"] == "extract"]
            command_event = next(event for event in extract_events if event["event_type"] == "command")
            layer_event = next(
                event
                for event in extract_events
                if event["event_type"] == "finished" and "Capa de extracción" in event["message"]
            )
            command_payload = command_event["structured"]
            layer_payload = layer_event["structured"]
            for key in (
                "extract_layer",
                "scan_root",
                "selected_archives",
                "tool",
                "command_preview",
                "cwd",
                "timeout_sec",
                "total_budget_remaining_sec",
                "available_space_bytes",
                "required_space_bytes",
            ):
                self.assertIn(key, command_payload)
            for key in (
                "extract_layer",
                "duration_sec",
                "return_code",
                "produced_files",
                "produced_media",
                "new_archives",
                "output_root",
                "decision",
            ):
                self.assertIn(key, layer_payload)
            self.assertIn("<JOB_ROOT>", json.dumps(command_payload))
            self.assertNotIn(str(job_root), json.dumps(command_payload))

            trace_matches = list((config.diagnostics_root / "jobs").glob(f"*/*{job['job_id']}"))
            self.assertEqual(len(trace_matches), 1)
            trace_dir = trace_matches[0]
            events_text = (trace_dir / "events.jsonl").read_text(encoding="utf-8")
            timeline_text = (trace_dir / "timeline.md").read_text(encoding="utf-8")
            summary = json.loads((trace_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertIn("Capa de extracción 1 preparada", events_text)
            self.assertIn("Capa de extracción 1 terminada", timeline_text)
            self.assertGreaterEqual(summary["event_count"], len(extract_events))

            report = create_codex_diagnostic(
                database,
                job["job_id"],
                config.codex_diag_root,
                {"orchestrator": {"status": "ok"}},
                force=True,
                diagnostics_root=config.diagnostics_root,
            )
            self.assertTrue(report["ok"])
            with zipfile.ZipFile(report["path"]) as archive:
                report_events = archive.read("traza_viva/events.jsonl").decode("utf-8")
            self.assertIn("Capa de extracción 1 preparada", report_events)
            database.close()

    def test_extraction_failure_review_preserves_original_and_extracted_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_root, original = self._job(root)
            (original / "outer.rar").write_bytes(b"outer")
            partial = job_root / "extracted" / "layer_01.tmp" / "inner.part01.rar"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"partial")

            review, preservation = move_extraction_failure_to_review(
                job_root,
                root / "review",
                "Movie Failure",
            )

            self.assertTrue((review / "original" / "outer.rar").is_file())
            self.assertTrue(
                (review / "extracted" / "layer_01.tmp" / "inner.part01.rar").is_file()
            )
            self.assertTrue(preservation["preserved_original"])
            self.assertTrue(preservation["preserved_extracted"])
            self.assertEqual(preservation["preserved_total_bytes"], len(b"outerpartial"))
            self.assertEqual(preservation["preservation_errors"], [])
            self.assertFalse(job_root.exists())

    def test_review_prioritizes_original_and_reports_extracted_move_failure_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_root, original = self._job(root)
            (original / "outer.rar").write_bytes(b"outer")
            extracted = job_root / "extracted"
            extracted.mkdir(parents=True)
            (extracted / "partial.rar").write_bytes(b"partial")
            real_move = shutil.move

            def controlled_move(source, destination):
                if Path(source).name == "extracted":
                    raise OSError(28, "No space left on device", str(source))
                return real_move(source, destination)

            with patch("arr_orchestrator.filesystem.shutil.move", side_effect=controlled_move):
                review, preservation = move_extraction_failure_to_review(
                    job_root,
                    root / "review",
                    "Movie Failure",
                )

            self.assertTrue((review / "original" / "outer.rar").is_file())
            self.assertFalse((review / "extracted").exists())
            self.assertTrue((job_root / "extracted" / "partial.rar").is_file())
            self.assertTrue(preservation["preserved_original"])
            self.assertFalse(preservation["preserved_extracted"])
            self.assertTrue(preservation["residual_job_root"])
            error_text = " ".join(preservation["preservation_errors"])
            self.assertIn("No space left on device", error_text)
            self.assertNotIn(str(job_root), error_text)

    def test_engine_extraction_error_writes_reason_and_preserves_both_trees(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            env = {
                "ARR_MODE": "active",
                "ARR_CONFIG_DIR": str(root / "config"),
                "ARR_DATA_ROOT": str(root / "data"),
                "ARR_WORKSHOP_ROOT": str(root / "workshop"),
                "ARR_REVIEW_DIR": str(root / "review"),
                "CODEX_DIAG_ROOT": str(root / "diagnosticos_codex"),
                "ARR_DIAGNOSTICS_ROOT": str(root / "diagnostics" / "arr"),
                "TMDB_API_TOKEN": "",
            }
            with patch.dict(os.environ, env, clear=False):
                config = Config.from_env()
            config.ensure_directories()
            database = Database(config.db_path)
            database.initialize()
            self.addCleanup(database.close)
            job_root = config.workshop_root / "job-extract-failure"
            original = job_root / "original"
            original.mkdir(parents=True)
            (original / "outer.rar").write_bytes(b"outer")
            job = database.create_job(
                "fs:movies:extract-failure",
                "fs",
                "movies",
                "Movie Failure.rar",
                state="ready_extract",
                stage_path=str(job_root),
                source_path=str(original),
            )
            engine = Engine(config, database)

            def failed_run(command, **_kwargs):
                _archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / "inner.part01.rar").write_bytes(b"partial")
                return _completed(command, returncode=2, output="Cannot find volume inner.part02.rar")

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=failed_run):
                engine._run_extract(job)

            updated = database.get_job(job["job_id"])
            review = Path(updated["stage_path"])
            reason = json.loads((review / "reason.json").read_text(encoding="utf-8"))
            self.assertEqual(updated["state"], "error_terminal")
            self.assertEqual(updated["last_error_code"], "extract_volume_missing")
            self.assertTrue((review / "original" / "outer.rar").is_file())
            self.assertTrue(
                (review / "extracted" / "layer_01.tmp" / "inner.part01.rar").is_file()
            )
            self.assertTrue((review / "Error de extraccion.txt").is_file())
            self.assertTrue(reason["preserved_original"])
            self.assertTrue(reason["preserved_extracted"])
            self.assertGreater(reason["preserved_total_bytes"], 0)
            self.assertEqual(reason["error_code"], "extract_volume_missing")
            self.assertFalse(job_root.exists())
            database.close()

    def test_simple_rar_zip_and_7z_extract_without_regression(self) -> None:
        for archive_name in ("movie.rar", "movie.zip", "movie.7z"):
            with self.subTest(archive_name=archive_name):
                with tempfile.TemporaryDirectory() as temporary:
                    job_root, original = self._job(Path(temporary))
                    (original / archive_name).write_bytes(b"archive")

                    def fake_run(command, **_kwargs):
                        _archive, output = _command_paths(command)
                        output.mkdir(parents=True, exist_ok=True)
                        (output / "movie.mkv").write_bytes(b"movie")
                        return _completed(command)

                    with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run):
                        result = extract_archives(job_root)
                    self.assertEqual(result.name, "layer_01")
                    self.assertEqual(len(media_files(result)), 1)

    def test_two_multipart_sets_extract_independently_without_mixing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            for name in (
                "alpha.part01.rar",
                "alpha.part02.rar",
                "beta.part001.rar",
                "beta.part002.rar",
            ):
                (original / name).write_bytes(name.encode("ascii"))

            def fake_run(command, **_kwargs):
                archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                prefix = archive.name.split(".part", 1)[0]
                (output / f"{prefix}.mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run) as run:
                result = extract_archives(job_root)
            videos = sorted(media_files(result), key=lambda path: path.name)
            self.assertEqual([video.name for video in videos], ["alpha.mkv", "beta.mkv"])
            self.assertNotEqual(videos[0].parent, videos[1].parent)
            self.assertEqual(run.call_count, 2)

    def test_valid_completed_layer_is_reused_even_when_free_space_is_now_low(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root, original = self._job(Path(temporary))
            (original / "movie.rar").write_bytes(b"archive")

            def fake_run(command, **_kwargs):
                _archive, output = _command_paths(command)
                output.mkdir(parents=True, exist_ok=True)
                (output / "movie.mkv").write_bytes(b"movie")
                return _completed(command)

            with patch("arr_orchestrator.filesystem.subprocess.run", side_effect=fake_run) as run:
                first = extract_archives(job_root)
                disk_usage = type("DiskUsage", (), {"free": 0})()
                with patch("arr_orchestrator.filesystem.shutil.disk_usage", return_value=disk_usage):
                    second = extract_archives(job_root)
            self.assertEqual(first, second)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
