import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from modulos.arr_trace import ArrTrace
from modulos.persistent_jobs import PersistentJobStore
from modulos.submission_store import SubmissionStore


def settings(fallback_enabled: bool = True) -> dict:
    data = app_module.copy_defaults()
    data["rdt"]["fallback_enabled"] = fallback_enabled
    data["qbit"]["fallback_enabled"] = fallback_enabled
    return data


class DeliveryTracingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        app_module.ui_jobs = PersistentJobStore(self.root / "ui_jobs", app_module.logger)
        app_module.submissions = SubmissionStore(self.root / "submissions.sqlite3", app_module.logger)
        app_module.arr_trace = ArrTrace(self.root / "diagnostics" / "arr", app_module.logger)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def trace_summary(self, trace_id: str) -> dict:
        matches = list((self.root / "diagnostics" / "arr" / "download").glob(f"*/{trace_id}/summary.json"))
        self.assertEqual(len(matches), 1)
        return json.loads(matches[0].read_text(encoding="utf-8"))

    def test_delivery_trace_normal_rdt_submission(self) -> None:
        with (
            patch.object(app_module, "load_settings", return_value=settings()),
            patch.object(
                app_module,
                "rdt_upload_magnet",
                return_value={"engine": "RDT-Client", "rdt_id": "rdt-normal"},
            ),
        ):
            result = app_module.deliver(
                "Pelicula normal",
                "magnet:?xt=urn:btih:" + "a" * 40,
                "movies",
                cleanup=True,
                source_result_id="normal",
                trace_id="download-normal",
            )

        summary = self.trace_summary("download-normal")
        self.assertTrue(result["ok"])
        self.assertEqual(result["engine"], "RDT-Client")
        self.assertEqual(summary["state"], "transport_done")
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["correlation"]["rdt_id"], "rdt-normal")

    def test_delivery_trace_fallback_to_qbit(self) -> None:
        with (
            patch.object(app_module, "load_settings", return_value=settings()),
            patch.object(app_module, "rdt_upload_magnet", side_effect=RuntimeError("rdt caido")),
            patch.object(
                app_module,
                "qbit_add_magnet",
                return_value={"engine": "qBittorrent", "hash": "abc"},
            ),
        ):
            result = app_module.deliver(
                "Pelicula fallback",
                "magnet:?xt=urn:btih:" + "b" * 40,
                "movies",
                cleanup=False,
                source_result_id="fallback",
                trace_id="download-fallback",
            )

        summary = self.trace_summary("download-fallback")
        self.assertTrue(result["ok"])
        self.assertEqual(result["engine"], "qBittorrent")
        self.assertEqual(summary["state"], "submitted_qbit")
        self.assertGreaterEqual(summary["warnings"], 1)
        self.assertEqual(summary["correlation"]["qbit_hash"], "abc")

    def test_delivery_trace_duplicate_reuse(self) -> None:
        magnet = "magnet:?xt=urn:btih:" + "c" * 40
        with (
            patch.object(app_module, "load_settings", return_value=settings()),
            patch.object(
                app_module,
                "rdt_upload_magnet",
                return_value={"engine": "RDT-Client", "rdt_id": "rdt-reuse"},
            ),
        ):
            first = app_module.deliver(
                "Pelicula repetida",
                magnet,
                "movies",
                cleanup=True,
                source_result_id="reuse",
                trace_id="download-reuse-first",
            )

        with (
            patch.object(app_module, "load_settings", return_value=settings()),
            patch.object(app_module, "rdt_upload_magnet", side_effect=AssertionError("no debe reenviar")),
        ):
            second = app_module.deliver(
                "Pelicula repetida",
                magnet,
                "movies",
                cleanup=True,
                source_result_id="reuse",
                trace_id="download-reuse-second",
            )

        summary = self.trace_summary("download-reuse-second")
        self.assertTrue(first["ok"])
        self.assertTrue(second["duplicate_guard"])
        self.assertEqual(summary["state"], "reused")

    def test_delivery_trace_transport_error(self) -> None:
        with (
            patch.object(app_module, "load_settings", return_value=settings(fallback_enabled=False)),
            patch.object(app_module, "rdt_upload_magnet", side_effect=RuntimeError("rdt caido")),
        ):
            with self.assertRaises(RuntimeError):
                app_module.deliver(
                    "Pelicula error",
                    "magnet:?xt=urn:btih:" + "d" * 40,
                    "movies",
                    cleanup=False,
                    source_result_id="error",
                    trace_id="download-error",
                )

        summary = self.trace_summary("download-error")
        self.assertEqual(summary["state"], "transport_error")
        self.assertEqual(summary["errors"], 1)

    def test_delivery_progress_reports_rd_then_qbit(self) -> None:
        progress_events = []

        with (
            patch.object(app_module, "load_settings", return_value=settings()),
            patch.object(app_module, "rdt_upload_magnet", side_effect=RuntimeError("rdt caido")),
            patch.object(
                app_module,
                "qbit_add_magnet",
                return_value={"engine": "qBittorrent", "hash": "abc"},
            ),
        ):
            result = app_module.deliver(
                "Pelicula visual",
                "magnet:?xt=urn:btih:" + "e" * 40,
                "movies",
                cleanup=False,
                source_result_id="visual",
                trace_id="download-visual",
                progress=lambda payload: progress_events.append(payload),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["engine"], "qBittorrent")
        self.assertEqual(result["submission_state"], "submitted_qbit")
        self.assertEqual([event["label"] for event in progress_events], ["Enviando a RD", "Enviando a qB"])
        self.assertEqual(progress_events[0]["tone"], "rd")
        self.assertEqual(progress_events[1]["tone"], "qbit")

    def test_download_job_dismiss_removes_finished_ui_job_only(self) -> None:
        job_id = "job_finished_123"
        path = self.root / "ui_jobs" / f"{job_id}.json"
        path.write_text(
            json.dumps(
                {
                    "id": job_id,
                    "kind": "download",
                    "fingerprint": "fp",
                    "state": "done",
                    "created_at": 1,
                    "updated_at": 1,
                    "request": {"title": "Pelicula"},
                    "result": {"ok": True},
                    "error": "",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        response = app_module.app.test_client().post(f"/api/jobs/download/{job_id}/dismiss")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertFalse(path.exists())
        self.assertEqual(app_module.submissions.stats(), {})

    def test_download_job_dismiss_does_not_remove_active_ui_job(self) -> None:
        job_id = "job_running_123"
        path = self.root / "ui_jobs" / f"{job_id}.json"
        now = int(time.time())
        path.write_text(
            json.dumps(
                {
                    "id": job_id,
                    "kind": "download",
                    "fingerprint": "fp",
                    "state": "running",
                    "created_at": now,
                    "updated_at": now,
                    "request": {"title": "Pelicula"},
                    "result": None,
                    "error": "",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        response = app_module.app.test_client().post(f"/api/jobs/download/{job_id}/dismiss")

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])
        self.assertTrue(path.exists())

    def test_download_job_dismiss_removes_stale_active_ui_job(self) -> None:
        job_id = "job_stale_123"
        path = self.root / "ui_jobs" / f"{job_id}.json"
        path.write_text(
            json.dumps(
                {
                    "id": job_id,
                    "kind": "download",
                    "fingerprint": "fp",
                    "state": "running",
                    "created_at": 1,
                    "updated_at": 1,
                    "request": {"title": "Pelicula"},
                    "progress": {"phase": "qbit_sending", "label": "Enviando a qB", "tone": "qbit"},
                    "result": None,
                    "error": "",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        response = app_module.app.test_client().post(f"/api/jobs/download/{job_id}/dismiss")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertFalse(path.exists())


class DeliveryFrontendContractTests(unittest.TestCase):
    def test_rdt_result_with_hash_is_classified_as_rd_before_qbit(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")
        start = script.index("function acceptedSendTone(job)")
        end = script.index("\n}\n\nfunction acceptedSendLabel", start)
        function_body = script[start:end]

        rdt_condition = 'if (engine.includes("rdt") || state === "rdt_monitoring" || state === "transport_done" || result.rdt_id) return "rd";'
        qbit_condition = 'if (engine.includes("qbit") || state === "submitted_qbit") return "qbit";'

        self.assertIn(rdt_condition, function_body)
        self.assertIn(qbit_condition, function_body)
        self.assertLess(function_body.index(rdt_condition), function_body.index(qbit_condition))
        self.assertNotIn("result.hash", function_body)


if __name__ == "__main__":
    unittest.main()
