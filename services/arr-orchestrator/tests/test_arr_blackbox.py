import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from arr_orchestrator.arr_blackbox import ArrBlackbox
from arr_orchestrator.codex_diagnostics import create_codex_diagnostic
from arr_orchestrator.db import Database
from arr_orchestrator.diagnostic_sanitizer import sanitize_text


EXPECTED_READ_ORDER = [
    "summary.json",
    "timeline.md",
    "warnings.jsonl",
    "errors.jsonl",
    "events.jsonl",
    "human_follow.json",
    "related_files.json",
    "meta.json",
]


def _trace_dir(root: Path, job_id: str) -> Path:
    matches = list((root / "diagnostics" / "arr" / "jobs").glob(f"*/*{job_id}"))
    if len(matches) != 1:
        raise AssertionError(f"Se esperaba una traza para {job_id}, hay {len(matches)}")
    return matches[0]


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class ArrBlackboxTests(unittest.TestCase):
    def test_create_job_starts_trace_files_and_running_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blackbox = ArrBlackbox(root / "diagnostics" / "arr")
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()

            job = database.create_job(
                "fs:movies:blackbox-created",
                "fs",
                "movies",
                "Pelicula Blackbox Creada.mkv",
                state="waiting_stable",
            )

            job_dir = _trace_dir(root, job["job_id"])
            summary = json.loads((job_dir / "summary.json").read_text(encoding="utf-8"))
            meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))

            for name in (
                "meta.json",
                "summary.json",
                "events.jsonl",
                "warnings.jsonl",
                "errors.jsonl",
                "timeline.md",
                "human_follow.json",
                "related_files.json",
            ):
                self.assertTrue((job_dir / name).exists(), name)
            self.assertEqual(summary["event_count"], 1)
            self.assertEqual(summary["source"], "orchestrator.db:job_events")
            self.assertEqual(summary["read_order"], EXPECTED_READ_ORDER)
            self.assertEqual(meta["read_order"], EXPECTED_READ_ORDER)
            self.assertEqual(meta["kind"], "job")
            self.assertEqual(meta["trace_kind"], "orchestrator_job")
            self.assertEqual(meta["trace_id"], job["job_id"])
            self.assertEqual(summary["counts"]["events"], 1)
            self.assertEqual(summary["diagnostic_status"], "ok")
            self.assertEqual(summary["operation_status"], "running")
            self.assertEqual(summary["phases"][0]["label"], "Entrada")
            self.assertEqual(summary["state"], "waiting_stable")
            self.assertEqual(summary["lifecycle"], "running")
            self.assertEqual(summary["last_event"]["phase"], "received")
            self.assertEqual(summary["last_event"]["event_type"], "started")
            self.assertEqual(_jsonl(job_dir / "events.jsonl")[0]["job_id"], job["job_id"])
            self.assertEqual((job_dir / "warnings.jsonl").read_text(encoding="utf-8"), "")
            self.assertEqual((job_dir / "errors.jsonl").read_text(encoding="utf-8"), "")
            database.close()

    def test_config_snapshot_is_written_to_meta_and_summary(self) -> None:
        env = {
            "ARR_MODE": "active",
            "ARR_STABLE_SECONDS": "8",
            "ARR_WORKSHOP_ROOT": "/data/downloads/torrents/complete/taller",
            "ARR_REVIEW_DIR": "/data/media/repetidas_vs_error",
            "CODEX_DIAG_ROOT": "/diagnosticos_codex",
            "ARR_DIAGNOSTICS_ROOT": "/diagnostics/arr",
            "MEDIA_WORKER_URL": "http://media-worker:8790",
            "QBT_URL": "http://gluetun:8080",
            "QBT_PASSWORD": "qbt-secret",
            "TMDB_API_TOKEN": "tmdb-secret",
        }
        with patch.dict("os.environ", env, clear=False):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                blackbox = ArrBlackbox(root / "diagnostics" / "arr")
                database = Database(root / "test.db", event_recorder=blackbox.record_event)
                database.initialize()
                job = database.create_job(
                    "fs:movies:blackbox-config-snapshot",
                    "fs",
                    "movies",
                    "Pelicula Config Snapshot.mkv",
                )

                job_dir = _trace_dir(root, job["job_id"])
                meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
                summary = json.loads((job_dir / "summary.json").read_text(encoding="utf-8"))
                meta_text = json.dumps(meta, ensure_ascii=False)

                self.assertEqual(meta["config_snapshot"]["schema"], "arr-config-snapshot-v1")
                self.assertEqual(summary["config_snapshot"], meta["config_snapshot"])
                self.assertEqual(meta["config_snapshot"]["mode"], "active")
                self.assertEqual(meta["config_snapshot"]["timing"]["stable_seconds"], "8")
                self.assertEqual(
                    meta["config_snapshot"]["paths"]["workshop"],
                    "<DATA_DOWNLOADS>/torrents/complete/taller",
                )
                self.assertEqual(
                    meta["config_snapshot"]["paths"]["review"],
                    "<DATA_MEDIA>/repetidas_vs_error",
                )
                self.assertEqual(meta["config_snapshot"]["services"]["media_worker"], "http://media-worker:8790")
                self.assertTrue(meta["config_snapshot"]["credential_flags"]["qbt_login_secret_present"])
                self.assertTrue(meta["config_snapshot"]["credential_flags"]["tmdb_credential_present"])
                self.assertNotIn("qbt-secret", meta_text)
                self.assertNotIn("tmdb-secret", meta_text)
                database.close()

    def test_command_event_is_preserved_in_blackbox_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blackbox = ArrBlackbox(root / "diagnostics" / "arr")
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()
            job = database.create_job(
                "fs:movies:blackbox-command",
                "fs",
                "movies",
                "Pelicula Command.mkv",
            )

            database.add_event(
                job["job_id"],
                "filebot",
                "command",
                "Comando FileBot preparado",
                {
                    "command_preview": {
                        "argv": ["filebot", "-rename", "/data/downloads/demo"],
                        "cwd": "/data/downloads/demo",
                        "timeout_sec": 14400,
                    }
                },
            )

            job_dir = _trace_dir(root, job["job_id"])
            events = _jsonl(job_dir / "events.jsonl")
            summary = json.loads((job_dir / "summary.json").read_text(encoding="utf-8"))
            timeline = (job_dir / "timeline.md").read_text(encoding="utf-8")

            self.assertEqual(events[-1]["event_type"], "command")
            self.assertEqual(summary["last_event"]["event_type"], "command")
            self.assertIn("filebot/command", timeline)
            self.assertIn("<DATA_DOWNLOADS>/demo", json.dumps(events[-1], ensure_ascii=False))
            database.close()

    def test_add_event_grows_events_jsonl_and_updates_last_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blackbox = ArrBlackbox(root / "diagnostics" / "arr")
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()
            job = database.create_job(
                "fs:movies:blackbox-growth",
                "fs",
                "movies",
                "Pelicula Blackbox Growth.mkv",
            )
            job_dir = _trace_dir(root, job["job_id"])
            before = len(_jsonl(job_dir / "events.jsonl"))

            database.add_event(
                job["job_id"],
                "identity",
                "decision",
                "Identidad resuelta por cache",
                {"tmdb_id": 1234},
            )

            events = _jsonl(job_dir / "events.jsonl")
            summary = json.loads((job_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(len(events), before + 1)
            self.assertEqual(summary["event_count"], before + 1)
            self.assertEqual(summary["last_event"]["phase"], "identity")
            self.assertEqual(summary["last_event"]["event_type"], "decision")
            self.assertEqual(summary["last_event"]["message"], "Identidad resuelta por cache")
            database.close()

    def test_warning_and_error_are_split_to_job_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blackbox = ArrBlackbox(root / "diagnostics" / "arr")
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()
            job = database.create_job(
                "fs:movies:blackbox-warning-error",
                "fs",
                "movies",
                "Pelicula Blackbox Aviso Error.mkv",
            )

            database.add_event(job["job_id"], "resolver", "warning", "Identidad dudosa")
            database.add_event(job["job_id"], "filebot", "error", "FileBot fallo")

            job_dir = _trace_dir(root, job["job_id"])
            summary = json.loads((job_dir / "summary.json").read_text(encoding="utf-8"))
            warnings = _jsonl(job_dir / "warnings.jsonl")
            errors = _jsonl(job_dir / "errors.jsonl")
            self.assertEqual(summary["warnings"], 1)
            self.assertEqual(summary["errors"], 1)
            self.assertEqual(summary["counts"]["warnings"], 1)
            self.assertEqual(summary["counts"]["errors"], 1)
            self.assertEqual(summary["diagnostic_status"], "error")
            self.assertEqual(summary["last_error_code"], "FileBot fallo")
            self.assertTrue(any(item["phase"] == "filebot" for item in summary["phases"]))
            self.assertEqual(warnings[0]["event_type"], "warning")
            self.assertEqual(errors[0]["event_type"], "error")
            self.assertEqual(summary["last_event"]["event_type"], "error")
            database.close()

    def test_synthetic_job_walkthrough_writes_expected_phases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blackbox = ArrBlackbox(root / "diagnostics" / "arr")
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()
            job = database.create_job(
                "fs:movies:blackbox-synthetic",
                "fs",
                "movies",
                "Pelicula Blackbox Sintetica.mkv",
                state="waiting_stable",
            )

            database.transition(job["job_id"], "ready_stage", "stable_wait", "Paquete estable")
            database.transition(job["job_id"], "ready_extract", "staging", "Movido a taller")
            database.transition(job["job_id"], "ready_filebot", "extract", "Extraccion preparada")
            database.add_event(job["job_id"], "identity", "decision", "Identidad resuelta")
            database.transition(job["job_id"], "media_postprocess_ready", "filebot", "FileBot correcto")
            database.transition(job["job_id"], "ready_cleanup", "verify", "Salida verificada")
            database.transition(job["job_id"], "done", "cleanup", "Trabajo terminado correctamente")

            job_dir = _trace_dir(root, job["job_id"])
            timeline = (job_dir / "timeline.md").read_text(encoding="utf-8")
            summary = json.loads((job_dir / "summary.json").read_text(encoding="utf-8"))
            for phase in ("received", "stable_wait", "staging", "extract", "identity", "filebot", "verify", "cleanup"):
                self.assertIn(f"{phase}/", timeline)
            self.assertEqual(summary["state"], "done")
            self.assertEqual(summary["lifecycle"], "final")
            self.assertEqual(summary["last_event"]["phase"], "cleanup")
            database.close()

    def test_database_events_are_mirrored_to_blackbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blackbox = ArrBlackbox(root / "diagnostics" / "arr")
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()

            job = database.create_job(
                "fs:movies:blackbox",
                "fs",
                "movies",
                "Pelicula Blackbox (2026).mkv",
                state="waiting_stable",
            )
            database.add_event(
                job["job_id"],
                "resolver",
                "warning",
                "TMDb no devolvio candidato unico",
                {"query": "Pelicula Blackbox", "log_file": "/config/media-worker/logs/demo.log"},
            )
            database.transition(
                job["job_id"],
                "done",
                "cleanup",
                "Trabajo terminado correctamente",
            )

            job_dirs = list((root / "diagnostics" / "arr" / "jobs").glob("*/*"))
            self.assertEqual(len(job_dirs), 1)
            job_dir = job_dirs[0]
            events = _jsonl(job_dir / "events.jsonl")
            summary = json.loads((job_dir / "summary.json").read_text(encoding="utf-8"))
            timeline = (job_dir / "timeline.md").read_text(encoding="utf-8")
            human_follow = json.loads((job_dir / "human_follow.json").read_text(encoding="utf-8"))
            related = json.loads((job_dir / "related_files.json").read_text(encoding="utf-8"))

            self.assertEqual(len(events), 3)
            self.assertEqual(summary["event_count"], 3)
            self.assertEqual(summary["warnings"], 1)
            self.assertEqual(summary["errors"], 0)
            self.assertEqual(summary["state"], "done")
            self.assertEqual(summary["lifecycle"], "final")
            self.assertEqual(summary["last_event"]["phase"], "cleanup")
            self.assertIn("cleanup/finished", timeline)
            self.assertTrue((job_dir / "warnings.jsonl").exists())
            self.assertTrue((job_dir / "errors.jsonl").exists())
            self.assertIn("cleanup/finished", human_follow["lines"][-1])
            self.assertEqual(related["files"][0]["path"], "<CONFIG>/media-worker/logs/demo.log")
            database.close()

    def test_blackbox_failure_never_breaks_database_event_write(self) -> None:
        def broken_recorder(_event):
            raise RuntimeError("blackbox unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "test.db", event_recorder=broken_recorder)
            database.initialize()

            job = database.create_job(
                "fs:movies:blackbox-fails",
                "fs",
                "movies",
                "Pelicula con blackbox caida.mkv",
            )
            database.add_event(job["job_id"], "test", "finished", "Evento guardado")

            detail = database.job_detail(job["job_id"])
            self.assertEqual(len(detail["timeline"]), 2)
            database.close()

    def test_blackbox_sanitizes_secrets_paths_truncates_and_limits_related_files(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            blackbox = ArrBlackbox(root / "diagnostics" / "arr")
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()
            job = database.create_job(
                "fs:movies:blackbox-sanitize",
                "fs",
                "movies",
                "Pelicula Sanitize.mkv",
            )
            database.add_event(
                job["job_id"],
                "qbt",
                "decision",
                "Magnet magnet:?xt=urn:btih:" + "a" * 40,
                {
                    "token": "secret-token",
                    "password": "secret-pass",
                    "download_url": "https://example.test/private.torrent",
                    "report_path": "/host/arr/config/reports/demo.json?token=abc123",
                    "source_path": "/data/downloads/torrents/complete/demo",
                    "stage_path": "/app/data/stage/demo",
                    "log_file": "/config/logs/demo.log",
                    "trace_id": "download-test-trace",
                    "correlation_id": "corr-test",
                    "long": "x" * 2500,
                    "many": list(range(60)),
                    "nested": [
                        {"log_file": f"/config/logs/demo-{index}.log"}
                        for index in range(30)
                    ],
                },
            )

            job_dir = _trace_dir(root, job["job_id"])
            events_text = (job_dir / "events.jsonl").read_text(encoding="utf-8")
            related = json.loads((job_dir / "related_files.json").read_text(encoding="utf-8"))
            summary = json.loads((job_dir / "summary.json").read_text(encoding="utf-8"))

            self.assertNotIn("secret-token", events_text)
            self.assertNotIn("secret-pass", events_text)
            self.assertNotIn("abc123", events_text)
            self.assertNotIn("magnet:?xt=", events_text)
            self.assertNotIn("/data/downloads", events_text)
            self.assertNotIn("/config/logs", events_text)
            self.assertNotIn("/host/arr/config", events_text)
            self.assertNotIn("/app/data", events_text)
            self.assertIn("<REDACTED>", events_text)
            self.assertIn("<MAGNET_REDACTED>", events_text)
            self.assertIn("<DATA_DOWNLOADS>/torrents/complete/demo", events_text)
            self.assertIn("<CONFIG>/logs/demo.log", events_text)
            self.assertIn("<APP_DATA>/stage/demo", events_text)
            self.assertIn("RECORTADO", events_text)
            self.assertIn("TRUNCATED_LIST", events_text)
            self.assertLessEqual(len(related["files"]), 20)
            self.assertEqual(summary["last_phase_label"], "qBittorrent")
            self.assertEqual(summary["correlation"]["trace_id"], "download-test-trace")
            self.assertEqual(summary["correlation"]["correlation_id"], "corr-test")
            database.close()

    def test_codex_diagnostic_export_sanitizes_live_trace_and_human_files(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            diagnostics_root = root / "diagnostics" / "arr"
            blackbox = ArrBlackbox(diagnostics_root)
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()
            job = database.create_job(
                "fs:movies:blackbox-export-sanitize",
                "fs",
                "movies",
                "Pelicula Export Sanitize.mkv",
                state="done",
                source_path="/data/downloads/torrents/complete/export",
                source_meta_json=json.dumps(
                    {
                        "trace_id": "download-export",
                        "correlation_id": "corr-export",
                        "url": "https://example.test/path?token=secret-query",
                    }
                ),
            )
            database.add_event(
                job["job_id"],
                "media_verify",
                "finished",
                "Verificado con authorization: Bearer secret",
                {
                    "api_key": "tmdb-secret",
                    "download_url": "https://example.test/private.torrent",
                    "source_path": "/data/downloads/torrents/complete/export",
                    "report_path": "/config/reports/export.json",
                },
            )

            result = create_codex_diagnostic(
                database,
                job["job_id"],
                root / "diagnosticos_codex",
                {"orchestrator": {"status": "ok"}},
                force=True,
                diagnostics_root=diagnostics_root,
            )

            with zipfile.ZipFile(result["path"]) as archive:
                combined = "\n".join(
                    archive.read(name).decode("utf-8", errors="replace")
                    for name in archive.namelist()
                    if name.endswith((".txt", ".json", ".jsonl", ".md"))
                )
            self.assertNotIn("tmdb-secret", combined)
            self.assertNotIn("Bearer secret", combined)
            self.assertNotIn("secret-query", combined)
            self.assertNotIn("https://example.test/private.torrent", combined)
            self.assertNotIn("/data/downloads", combined)
            self.assertNotIn("/config/reports", combined)
            self.assertIn("<REDACTED>", combined)
            self.assertIn("<DATA_DOWNLOADS>/torrents/complete/export", combined)
            self.assertIn("<CONFIG>/reports/export.json", combined)
            self.assertIn("CORRELACION", combined)
            self.assertIn("download-export", combined)
            self.assertIn("ORDEN DE LECTURA PARA IA/CODEX", combined)
            self.assertIn("Verificacion media [media_verify]", combined)
            database.close()

    def test_sanitizer_aliases_extra_paths_and_query_secrets(self) -> None:
        text = sanitize_text(
            "url=https://example.test/file.torrent?token=secret&api_key=abc "
            "path=/host/data/media/movies/demo "
            "cfg=/host/arr/config/rules.json "
            "app=/app/logs/buscador.log "
            "win=C:\\arr\\diagnostics\\arr\\jobs"
        )

        self.assertNotIn("secret", text)
        self.assertNotIn("api_key=abc", text)
        self.assertNotIn("/host/data/media", text)
        self.assertNotIn("/host/arr/config", text)
        self.assertNotIn("/app/logs", text)
        self.assertNotIn("C:\\arr\\diagnostics", text)
        self.assertIn("<DATA_MEDIA>/movies/demo", text)
        self.assertIn("<CONFIG>/rules.json", text)
        self.assertIn("<APP_LOGS>/buscador.log", text)
        self.assertIn("<DIAGNOSTICS>\\arr\\jobs", text)

    def test_codex_diagnostic_includes_live_trace_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            diagnostics_root = root / "diagnostics" / "arr"
            blackbox = ArrBlackbox(diagnostics_root)
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()
            job = database.create_job(
                "fs:movies:blackbox-zip",
                "fs",
                "movies",
                "Pelicula con traza viva.mkv",
                state="done",
                source_path="/data/media/movies/Pelicula con traza viva",
            )
            database.add_event(job["job_id"], "cleanup", "finished", "Trabajo terminado")

            result = create_codex_diagnostic(
                database,
                job["job_id"],
                root / "diagnosticos_codex",
                {"orchestrator": {"status": "ok"}},
                force=True,
                diagnostics_root=diagnostics_root,
            )

            self.assertTrue(result["ok"])
            with zipfile.ZipFile(result["path"]) as archive:
                names = set(archive.namelist())
            self.assertIn("traza_viva/meta.json", names)
            self.assertIn("traza_viva/summary.json", names)
            self.assertIn("traza_viva/events.jsonl", names)
            self.assertIn("traza_viva/warnings.jsonl", names)
            self.assertIn("traza_viva/errors.jsonl", names)
            self.assertIn("traza_viva/timeline.md", names)
            self.assertIn("traza_viva/human_follow.json", names)
            self.assertIn("traza_viva/related_files.json", names)
            database.close()


if __name__ == "__main__":
    unittest.main()
