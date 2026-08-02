import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as app_module
from modulos.arr_trace import ArrTrace
from modulos.persistent_jobs import PersistentJobStore
from modulos.rdt_monitor import MonitorStore, TorrentListing
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
        self.original_monitor_store = app_module.rdt_monitor._store
        self.monitor_store = MonitorStore(
            self.root / "monitor_state.json",
            self.root / "monitor_torrents",
            app_module.logger,
        )
        app_module.rdt_monitor._store = self.monitor_store
        app_module.rdt_monitor._listing_cache = None
        app_module.rdt_monitor._observations = {}

    def tearDown(self) -> None:
        app_module.rdt_monitor._store = self.original_monitor_store
        app_module.rdt_monitor._listing_cache = None
        app_module.rdt_monitor._observations = {}
        self.temporary.cleanup()

    def seed_monitor_state(self, state: dict) -> None:
        for item in state.values():
            self.monitor_store.register_item(dict(item))

    def reset_monitor_store(self, suffix: str) -> None:
        self.monitor_store = MonitorStore(
            self.root / f"monitor_state_{suffix}.json",
            self.root / f"monitor_torrents_{suffix}",
            app_module.logger,
        )
        app_module.rdt_monitor._store = self.monitor_store
        app_module.rdt_monitor._listing_cache = None
        app_module.rdt_monitor._observations = {}

    @staticmethod
    def monitor_listing(rows: list[dict], context=None) -> TorrentListing:
        return TorrentListing(context=context or object(), rows=tuple(rows))

    def trace_summary(self, trace_id: str) -> dict:
        matches = list((self.root / "diagnostics" / "arr" / "download").glob(f"*/{trace_id}/summary.json"))
        self.assertEqual(len(matches), 1)
        return json.loads(matches[0].read_text(encoding="utf-8"))

    def test_monitor_empty_state_skips_login_and_save(self) -> None:
        with (
            patch.object(app_module, "_rdt_monitor_list_torrents") as listing,
            patch.object(self.monitor_store, "apply_deltas", wraps=self.monitor_store.apply_deltas) as apply,
        ):
            app_module.rdt_monitor.poll_once()

        listing.assert_not_called()
        apply.assert_not_called()

    def test_monitor_active_download_keeps_current_unknown_progress_without_fallback(self) -> None:
        now = 1_000_000
        key = "rdt-active-download"
        item = {
            "rdt_id": key,
            "title": "Pelicula descargando",
            "category": "movies",
            "first_seen": now - 60,
            "last_progress_ts": now - 60,
            "last_progress": -1.0,
            "last_status": "Waiting",
        }
        state = {key: item}
        row = {
            "torrentId": key,
            "statusText": "Downloading file 1/1 (37.50% - 1 MB/s)",
            "downloadsCount": 1,
            "completed": False,
        }
        session = object()
        self.seed_monitor_state(state)

        with (
            patch.object(app_module, "load_settings", return_value=settings(True)),
            patch.object(
                app_module,
                "_rdt_monitor_list_torrents",
                return_value=self.monitor_listing([row], session),
            ) as get_rows,
            patch.object(app_module, "qbit_add_magnet") as fallback,
            patch.object(app_module.time, "time", return_value=now),
        ):
            app_module.rdt_monitor.poll_once()

        get_rows.assert_called_once_with()
        fallback.assert_not_called()
        stored = self.monitor_store.snapshot()[key]
        self.assertEqual(stored["last_progress"], -1.0)
        self.assertEqual(stored["last_progress_ts"], now - 60)
        self.assertEqual(stored["last_status"], "Waiting")

    def test_monitor_list_error_preserves_item_without_last_error(self) -> None:
        key = "rdt-temporary-error"
        item = {
            "rdt_id": key,
            "title": "Pelicula con error temporal",
            "category": "movies",
            "last_progress": -1.0,
        }
        state = {key: item}
        self.seed_monitor_state(state)
        before = self.monitor_store.state_path.read_bytes()

        with (
            patch.object(app_module, "load_settings", return_value=settings(True)),
            patch.object(
                app_module,
                "_rdt_monitor_list_torrents",
                side_effect=RuntimeError("RDT-Client: HTTP 503"),
            ),
            patch.object(app_module, "qbit_add_magnet") as fallback,
        ):
            app_module.rdt_monitor.poll_once()

        fallback.assert_not_called()
        self.assertEqual(self.monitor_store.state_path.read_bytes(), before)
        self.assertNotIn("last_error", self.monitor_store.snapshot()[key])

    def test_rdt_wait_ready_starts_with_exact_per_id_endpoint(self) -> None:
        torrent_id = "rdt-current-contract"
        session = object()
        row = {
            "torrentId": torrent_id,
            "statusText": "Downloading file 1/1 (1.00% - 1 MB/s)",
            "downloadsCount": 1,
        }

        with (
            patch.object(app_module, "load_settings", return_value=settings()),
            patch.object(app_module, "rdt_json", return_value=row) as get_row,
            patch.object(app_module, "rdt_find_row") as find_row,
        ):
            result = app_module.rdt_wait_ready(session, torrent_id)

        get_row.assert_called_once_with(session, f"/Api/Torrents/Get/{torrent_id}")
        find_row.assert_not_called()
        self.assertEqual(result["rdt_id"], torrent_id)

    def test_monitor_list_adapter_uses_one_exact_local_list_endpoint(self) -> None:
        session = object()
        rows = [{"torrentId": "rdt-list-contract"}]

        with (
            patch.object(app_module, "rdt_login", return_value=session) as login,
            patch.object(app_module, "rdt_json", return_value=rows) as get_rows,
        ):
            listing = app_module._rdt_monitor_list_torrents()

        login.assert_called_once_with()
        get_rows.assert_called_once_with(session, "/Api/Torrents")
        self.assertEqual(listing.context, session)
        self.assertEqual(listing.rows, tuple(rows))

    def test_engine_status_preserves_current_top_level_contract(self) -> None:
        item = {
            "rdt_id": "rdt-engine-status",
            "last_progress": -1.0,
            "progress": None,
            "progress_status": "Preparing",
            "progress_observed_at": 1_000_000,
            "progress_stale": False,
        }

        with patch.object(app_module.rdt_monitor, "snapshot", return_value={item["rdt_id"]: item}):
            response = app_module.app.test_client().get("/api/engine-status")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            set(payload),
            {"ok", "monitoring", "items", "submissions", "recent_submissions"},
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["monitoring"], 1)
        self.assertEqual(payload["items"], [item])
        self.assertEqual(
            {"progress", "progress_status", "progress_observed_at", "progress_stale"},
            set(payload["items"][0]) - {"rdt_id", "last_progress"},
        )

    def test_monitor_loop_keeps_current_order_and_sixty_second_sleep(self) -> None:
        events = []

        def stop_after_sleep(seconds: int) -> None:
            events.append(("sleep", seconds))
            raise RuntimeError("stop monitor loop after one iteration")

        with (
            patch.object(
                app_module.rdt_monitor,
                "poll_once",
                side_effect=lambda: events.append("poll_once"),
            ),
            patch.object(
                app_module,
                "link_pending_submissions",
                side_effect=lambda: events.append("link_pending_submissions") or 0,
            ),
            patch.object(app_module.time, "sleep", side_effect=stop_after_sleep),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop monitor loop after one iteration"):
                app_module.monitor_loop()

        self.assertEqual(
            events,
            ["poll_once", "link_pending_submissions", ("sleep", 60)],
        )

    def test_monitor_cleans_retained_finished_row_even_when_fallback_is_disabled(self) -> None:
        self.assertEqual(app_module.RDT_FINISHED_CLEANUP_DELAY_SEC, 30)
        now = 1_000_000
        key = "rdt-fallback-off"
        item = {
            "rdt_id": key,
            "title": "Pelicula terminada",
            "category": "movies",
            "kind": "magnet",
            "hash": "b" * 40,
            "finished_seen_ts": now - app_module.RDT_FINISHED_CLEANUP_DELAY_SEC,
        }
        state = {key: item}
        session = object()
        self.seed_monitor_state(state)

        with (
            patch.object(app_module, "load_settings", return_value=settings(False)),
            patch.object(
                app_module,
                "_rdt_monitor_list_torrents",
                return_value=self.monitor_listing(
                    [{"torrentId": key, "status": "finished", "progress": 100, "completed": True}],
                    session,
                ),
            ),
            patch.object(app_module, "rdt_cleanup_finished") as cleanup,
            patch.object(app_module.time, "time", return_value=now),
        ):
            app_module.rdt_monitor.poll_once()

        cleanup.assert_called_once_with(session, key)
        self.assertEqual(self.monitor_store.snapshot(), {})

    def test_missing_finished_row_never_triggers_qbit(self) -> None:
        for fallback_enabled in (False, True):
            with self.subTest(fallback_enabled=fallback_enabled):
                key = f"rdt-finished-missing-{fallback_enabled}"
                item = {
                    "rdt_id": key,
                    "title": "Pelicula ya terminada",
                    "category": "movies",
                    "finished_seen_ts": 100,
                    "submission_key": f"submission-{fallback_enabled}",
                }
                state = {key: item}
                self.reset_monitor_store(f"missing-{fallback_enabled}")
                self.seed_monitor_state(state)
                app_module.submissions.begin(
                    item["submission_key"],
                    item["title"],
                    "movies",
                    "movies",
                    "result-1",
                    "magnet:?xt=urn:btih:" + "a" * 40,
                    3600,
                )
                app_module.submissions.update(
                    item["submission_key"],
                    state="rdt_monitoring",
                    engine="RDT-Client",
                    rdt_id=key,
                )

                with (
                    patch.object(
                        app_module,
                        "load_settings",
                        return_value=settings(fallback_enabled),
                    ),
                    patch.object(
                        app_module,
                        "_rdt_monitor_list_torrents",
                        return_value=self.monitor_listing([]),
                    ),
                    patch.object(app_module, "qbit_add_magnet", side_effect=AssertionError("qB no debe intervenir")),
                    patch.object(self.monitor_store, "cleanup_artifact", wraps=self.monitor_store.cleanup_artifact) as artifact_cleanup,
                ):
                    app_module.rdt_monitor.poll_once()

                artifact_cleanup.assert_called_once_with(item, "finished-missing")
                self.assertEqual(self.monitor_store.snapshot(), {})
                submission = app_module.submissions.get(item["submission_key"])
                self.assertEqual(submission["state"], "transport_done")
                self.assertEqual(submission["rdt_id"], key)

    def test_finished_item_ignores_regressed_rdt_row_without_qbit(self) -> None:
        now = 1_000_000
        for route in ("id", "hash"):
            for elapsed, should_cleanup in ((29, False), (30, True)):
                with self.subTest(route=route, elapsed=elapsed):
                    key = f"rdt-regressed-{route}-{elapsed}"
                    info_hash = ("a" if route == "id" else "b") * 40
                    row = {
                        "torrentId": key if route == "id" else "rdt-renamed",
                        "hash": info_hash,
                        "status": "error",
                        "progress": 0,
                        "completed": False,
                    }
                    item = {
                        "rdt_id": key,
                        "title": "Pelicula ya terminada",
                        "category": "movies",
                        "finished_seen_ts": now - elapsed,
                        "hash": info_hash,
                        "kind": "magnet",
                    }
                    state = {key: item}
                    session = object()
                    self.reset_monitor_store(f"regressed-{route}-{elapsed}")
                    self.seed_monitor_state(state)

                    with (
                        patch.object(app_module, "load_settings", return_value=settings(True)),
                        patch.object(
                            app_module,
                            "_rdt_monitor_list_torrents",
                            return_value=self.monitor_listing([row], session),
                        ),
                        patch.object(app_module, "rdt_cleanup_finished") as cleanup,
                        patch.object(app_module, "qbit_add_magnet") as fallback,
                        patch.object(app_module.time, "time", return_value=now),
                    ):
                        app_module.rdt_monitor.poll_once()

                    fallback.assert_not_called()

                    if should_cleanup:
                        cleanup.assert_called_once_with(session, key)
                        self.assertEqual(self.monitor_store.snapshot(), {})
                    else:
                        cleanup.assert_not_called()
                        self.assertIn(key, self.monitor_store.snapshot())

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
        self.assertEqual(result["trace_id"], "download-normal")
        self.assertEqual(
            app_module.submissions.get(result["submission_key"])["result"]["trace_id"],
            "download-normal",
        )
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

    def test_pending_submission_links_trace_to_one_exact_arr_job(self) -> None:
        info_hash = "a" * 40
        trace_id = "download-correlation-exact"
        key = "submission-correlation-exact"
        app_module.submissions.begin(
            key,
            "Pelicula correlacionada",
            "movies",
            "movies",
            "result-correlation",
            "magnet:?xt=urn:btih:" + info_hash,
            3600,
        )
        app_module.submissions.update(
            key,
            state="submitted_qbit",
            engine="qBittorrent",
            qbit_hash=info_hash,
            result={"trace_id": trace_id, "hash": info_hash},
        )
        app_module.arr_trace.start("download", trace_id, {"title": "Pelicula correlacionada"})
        row = app_module.submissions.get(key)
        job = {
            "job_id": "job-arr-exacto",
            "category": "movies",
            "created_at": row["created_at"] + 10,
            "qbt_hash": info_hash.upper(),
            "rdt_id": "",
        }
        jobs_response = Mock()
        jobs_response.json.return_value = [job]
        event_response = Mock()

        with (
            patch.object(app_module.requests, "get", return_value=jobs_response) as get,
            patch.object(app_module.requests, "post", return_value=event_response) as post,
        ):
            linked = app_module.link_pending_submissions()

        self.assertEqual(linked, 1)
        get.assert_called_once()
        post.assert_called_once()
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["phase"], "correlation")
        self.assertEqual(payload["structured"]["trace_id"], trace_id)
        self.assertEqual(payload["structured"]["job_id"], job["job_id"])
        stored = app_module.submissions.get(key)["result"]
        self.assertTrue(stored["correlation_linked"])
        self.assertEqual(stored["job_id"], job["job_id"])
        self.assertEqual(self.trace_summary(trace_id)["correlation"]["job_id"], job["job_id"])

    def test_pending_submission_does_not_link_ambiguous_exact_jobs(self) -> None:
        info_hash = "b" * 40
        trace_id = "download-correlation-ambiguous"
        key = "submission-correlation-ambiguous"
        app_module.submissions.begin(
            key,
            "Pelicula ambigua",
            "movies",
            "movies",
            "result-ambiguous",
            "magnet:?xt=urn:btih:" + info_hash,
            3600,
        )
        app_module.submissions.update(
            key,
            state="submitted_qbit",
            engine="qBittorrent",
            qbit_hash=info_hash,
            result={"trace_id": trace_id, "hash": info_hash},
        )
        row = app_module.submissions.get(key)
        jobs = [
            {
                "job_id": f"job-arr-{index}",
                "category": "movies",
                "created_at": row["created_at"] + index,
                "qbt_hash": info_hash,
            }
            for index in (1, 2)
        ]
        jobs_response = Mock()
        jobs_response.json.return_value = jobs

        with (
            patch.object(app_module.requests, "get", return_value=jobs_response),
            patch.object(app_module.requests, "post") as post,
        ):
            linked = app_module.link_pending_submissions()

        self.assertEqual(linked, 0)
        post.assert_not_called()
        stored = app_module.submissions.get(key)["result"]
        self.assertNotIn("job_id", stored)
        self.assertFalse(stored.get("correlation_linked", False))


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
