import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modulos.arr_trace import ArrTrace


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


class ArrTraceTests(unittest.TestCase):
    def test_trace_writes_events_summary_and_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "diagnostics" / "arr"
            trace = ArrTrace(root)
            trace_id = trace.trace_id("search", {"query": "Torrente Presidente"})

            trace.start("search", trace_id, {"query": "Torrente Presidente"})
            trace.event("search", trace_id, "jackett", "warning", "Indexer lento", {"log_file": "/app/logs/buscador.log"})
            trace.finish("search", trace_id, "done", {"count": 3})

            trace_dirs = list((root / "search").glob("*/*"))
            self.assertEqual(len(trace_dirs), 1)
            trace_dir = trace_dirs[0]
            events = (trace_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            summary = json.loads((trace_dir / "summary.json").read_text(encoding="utf-8"))
            meta = json.loads((trace_dir / "meta.json").read_text(encoding="utf-8"))
            human = json.loads((trace_dir / "human_follow.json").read_text(encoding="utf-8"))
            related = json.loads((trace_dir / "related_files.json").read_text(encoding="utf-8"))
            timeline = (trace_dir / "timeline.md").read_text(encoding="utf-8")

            self.assertEqual(len(events), 3)
            self.assertEqual(summary["event_count"], 3)
            self.assertEqual(summary["warnings"], 1)
            self.assertEqual(summary["counts"]["events"], 3)
            self.assertEqual(summary["diagnostic_status"], "warning")
            self.assertEqual(summary["read_order"], EXPECTED_READ_ORDER)
            self.assertEqual(meta["read_order"], EXPECTED_READ_ORDER)
            self.assertEqual(meta["trace_kind"], "buscador_bridge_trace")
            self.assertEqual(meta["action"], "search")
            self.assertEqual(summary["state"], "done")
            self.assertEqual(summary["correlation"]["trace_id"], trace_id)
            self.assertTrue((trace_dir / "meta.json").exists())
            self.assertTrue((trace_dir / "warnings.jsonl").exists())
            self.assertTrue((trace_dir / "errors.jsonl").exists())
            self.assertIn("jackett/warning", timeline)
            self.assertIn("finish/finished", human["lines"][-1])
            self.assertEqual(related["files"][0]["path"], "<APP_LOGS>/buscador.log")

    def test_trace_config_snapshot_is_written_to_meta_and_summary(self) -> None:
        env = {
            "RDT_SAVE_ROOT": "/data/downloads",
            "QBIT_SAVE_ROOT": "/data/downloads/torrents/complete",
            "LOG_DIR": "/app/logs",
            "DATA_DIR": "/app/data",
            "ARR_DIAGNOSTICS_ROOT": "/diagnostics/arr",
            "REAL_DEBRID_API": "https://api.real-debrid.com/rest/1.0",
            "JACKETT_API_KEY": "jackett-secret",
            "REAL_DEBRID_TOKEN": "rd-secret",
        }
        with patch.dict("os.environ", env, clear=False):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "diagnostics" / "arr"
                trace = ArrTrace(root)
                trace.event("search", "search-config", "request", "started", "Trazabilidad iniciada")

                trace_dir = next((root / "search").glob("*/*"))
                meta = json.loads((trace_dir / "meta.json").read_text(encoding="utf-8"))
                summary = json.loads((trace_dir / "summary.json").read_text(encoding="utf-8"))
                meta_text = json.dumps(meta, ensure_ascii=False)

                self.assertEqual(meta["config_snapshot"]["schema"], "arr-bridge-config-snapshot-v1")
                self.assertEqual(summary["config_snapshot"], meta["config_snapshot"])
                self.assertEqual(meta["config_snapshot"]["paths"]["rdt_save_root"], "<DATA_DOWNLOADS>")
                self.assertEqual(
                    meta["config_snapshot"]["paths"]["qbit_save_root"],
                    "<DATA_DOWNLOADS>/torrents/complete",
                )
                self.assertEqual(meta["config_snapshot"]["paths"]["logs"], "<APP_LOGS>")
                self.assertEqual(
                    meta["config_snapshot"]["services"]["real_debrid"],
                    "https://api.real-debrid.com/rest/1.0",
                )
                self.assertTrue(meta["config_snapshot"]["credential_flags"]["jackett_credential_present"])
                self.assertTrue(meta["config_snapshot"]["credential_flags"]["real_debrid_credential_present"])
                self.assertNotIn("jackett-secret", meta_text)
                self.assertNotIn("rd-secret", meta_text)

    def test_trace_preserves_command_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "diagnostics" / "arr"
            trace = ArrTrace(root)

            trace.event(
                "download",
                "download-command",
                "transport",
                "command",
                "Envio preparado",
                {
                    "command_preview": {
                        "method": "POST",
                        "endpoint": "/api/v2/torrents/add",
                        "savepath": "/data/downloads/torrents/complete/movies",
                    }
                },
            )

            trace_dir = next((root / "download").glob("*/*"))
            event = json.loads((trace_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
            summary = json.loads((trace_dir / "summary.json").read_text(encoding="utf-8"))
            timeline = (trace_dir / "timeline.md").read_text(encoding="utf-8")

            self.assertEqual(event["event_type"], "command")
            self.assertEqual(summary["last_event_type"], "command")
            self.assertIn("transport/command", timeline)
            self.assertIn("<DATA_DOWNLOADS>/torrents/complete/movies", json.dumps(event, ensure_ascii=False))

    def test_trace_sanitizes_secrets_paths_truncates_and_limits_related_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "diagnostics" / "arr"
            trace = ArrTrace(root)
            trace_id = "download-sanitize"
            trace.event(
                "download",
                trace_id,
                "qbit",
                "decision",
                "Envio magnet:?xt=urn:btih:" + "b" * 40,
                {
                    "auth": "secret-auth",
                    "download_url": "https://example.test/file.torrent",
                    "magnet": "magnet:?xt=urn:btih:" + "c" * 40,
                    "log_file": "/app/logs/buscador.log",
                    "long": "x" * 2500,
                    "many": list(range(60)),
                    "nested": [{"log_file": f"/app/logs/{index}.log"} for index in range(30)],
                },
            )

            trace_dir = next((root / "download").glob("*/*"))
            events_text = (trace_dir / "events.jsonl").read_text(encoding="utf-8")
            related = json.loads((trace_dir / "related_files.json").read_text(encoding="utf-8"))
            summary = json.loads((trace_dir / "summary.json").read_text(encoding="utf-8"))

            self.assertNotIn("secret-auth", events_text)
            self.assertNotIn("https://example.test/file.torrent", events_text)
            self.assertNotIn("magnet:?xt=", events_text)
            self.assertNotIn("/app/logs", events_text)
            self.assertIn("<REDACTED>", events_text)
            self.assertIn("<MAGNET_REDACTED>", events_text)
            self.assertIn("<APP_LOGS>/buscador.log", events_text)
            self.assertIn("RECORTADO", events_text)
            self.assertIn("TRUNCATED_LIST", events_text)
            self.assertLessEqual(len(related["files"]), 20)
            self.assertEqual(summary["last_phase_label"], "qBittorrent")

    def test_trace_records_job_correlation_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "diagnostics" / "arr"
            trace = ArrTrace(root)
            trace_id = "download-linked"

            trace.start("download", trace_id, {"title": "Pelicula"})
            trace.link_job("download", trace_id, "job-123", qbit_hash="abc123", correlation_id="corr-123")

            trace_dir = next((root / "download").glob("*/*"))
            summary = json.loads((trace_dir / "summary.json").read_text(encoding="utf-8"))
            human = json.loads((trace_dir / "human_follow.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["correlation"]["job_id"], "job-123")
            self.assertEqual(summary["correlation"]["qbit_hash"], "abc123")
            self.assertEqual(summary["correlation"]["correlation_id"], "corr-123")
            self.assertEqual(human["correlation"]["job_id"], "job-123")


if __name__ == "__main__":
    unittest.main()
