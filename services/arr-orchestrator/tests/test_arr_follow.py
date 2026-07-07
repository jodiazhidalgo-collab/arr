import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from arr_orchestrator.arr_blackbox import ArrBlackbox
from arr_orchestrator.arr_follow import build_follow_payload
from arr_orchestrator.db import Database
from arr_orchestrator.health import start_health_server


class ArrFollowTests(unittest.TestCase):
    def test_follow_payload_uses_job_detail_and_blackbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blackbox = ArrBlackbox(root / "diagnostics" / "arr")
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()
            job = database.create_job(
                "fs:movies:follow",
                "fs",
                "movies",
                "Pelicula Follow.mkv",
                state="waiting_stable",
            )
            database.transition(job["job_id"], "manual_review", "identity", "Revision manual")

            payload = build_follow_payload(
                database.job_detail(job["job_id"]),
                root / "diagnostics" / "arr",
            )

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["schema"], "arr-follow-payload-v2")
            self.assertEqual(payload["translator_version"], "arr-follow-v2")
            self.assertEqual(payload["state"], "manual_review")
            self.assertEqual(payload["operation_status"], "final")
            self.assertEqual(payload["diagnostic_status"], "error")
            self.assertEqual(payload["current"]["phase"], "identity")
            self.assertEqual(payload["cursor"]["event_count"], 2)
            self.assertTrue(payload["advice"])
            self.assertTrue(payload["blackbox"]["available"])
            self.assertTrue(payload["errors"])
            self.assertTrue(payload["lines"])
            self.assertEqual(payload["phases"][-1]["label"], "Identidad")
            self.assertIn("human_follow", payload["blackbox"]["files"])
            database.close()

    def test_follow_labels_warning_and_error_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "test.db")
            database.initialize()
            job = database.create_job(
                "fs:movies:follow-error",
                "fs",
                "movies",
                "Pelicula Error.mkv",
                state="waiting_stable",
            )
            database.add_event(job["job_id"], "resolver", "warning", "Identidad dudosa")
            database.transition(job["job_id"], "error_terminal", "filebot", "FileBot fallo")

            payload = build_follow_payload(database.job_detail(job["job_id"]))

            self.assertEqual(payload["state"], "error_terminal")
            self.assertEqual(payload["phases"][-1]["label"], "FileBot")
            self.assertTrue(any("Identidad" in line for line in payload["lines"]))
            self.assertTrue(payload["errors"])
            database.close()

    def test_follow_matches_download_trace_to_job_by_qbit_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            diagnostics_root = root / "diagnostics" / "arr"
            blackbox = ArrBlackbox(diagnostics_root)
            database = Database(root / "test.db", event_recorder=blackbox.record_event)
            database.initialize()
            job = database.create_job(
                "qbt:abc123",
                "qbt",
                "movies",
                "Pelicula con correlacion.mkv",
                state="waiting_stable",
                infohash="abc123",
                qbt_hash="abc123",
            )

            trace_dir = diagnostics_root / "download" / "2999-01-01" / "download-abc"
            trace_dir.mkdir(parents=True)
            (trace_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "schema": "arr-search-trace-summary-v1",
                        "kind": "download",
                        "trace_id": "download-abc",
                        "state": "submitted_qbit",
                        "last_phase": "finish",
                        "last_message": "Traza cerrada",
                        "correlation": {"qbit_hash": "abc123", "trace_id": "download-abc"},
                    }
                ),
                encoding="utf-8",
            )

            payload = build_follow_payload(database.job_detail(job["job_id"]), diagnostics_root)

            self.assertTrue(payload["correlation"]["available"])
            self.assertEqual(payload["correlation"]["related_traces"][0]["trace_id"], "download-abc")
            self.assertEqual(payload["correlation"]["related_traces"][0]["match"], "qbit_hash")
            database.close()

    def test_health_server_exposes_follow_and_diagnostic(self) -> None:
        server = start_health_server(
            0,
            lambda: {"status": "ok"},
            lambda: [],
            lambda job_id: {"ok": True, "job": {"job_id": job_id}, "timeline": []},
            None,
            lambda job_id: {"ok": True, "job_id": job_id, "timeline": []},
            lambda job_id, force=False: {"ok": True, "job_id": job_id, "force": force},
        )
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/jobs/job-123/follow", timeout=5
            ) as response:
                follow = json.loads(response.read().decode("utf-8"))
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/jobs/job-123/diagnostic",
                data=b'{"force": true}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                diagnostic = json.loads(response.read().decode("utf-8"))

            self.assertEqual(follow["job_id"], "job-123")
            self.assertEqual(diagnostic["job_id"], "job-123")
            self.assertTrue(diagnostic["force"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
