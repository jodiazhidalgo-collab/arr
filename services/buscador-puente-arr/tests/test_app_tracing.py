import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from modulos.arr_trace import ArrTrace
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


if __name__ == "__main__":
    unittest.main()
