import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from modulos.rdt_monitor import MonitorStore, RdtMonitor, RdtMonitorPorts, TorrentListing
from modulos.rdt_monitor.model import is_finished, status_progress


def monitor_settings(fallback_enabled: bool = True) -> dict:
    return {
        "rdt": {
            "fallback_enabled": fallback_enabled,
            "cleanup_on_fallback": True,
            "ready_timeout_sec": 5,
        },
        "qbit": {"fallback_enabled": fallback_enabled},
    }


class MutableClock:
    def __init__(self, value: float = 1_000_000) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RdtMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        self.logger = Mock()
        self.clock = MutableClock()
        self.settings = monitor_settings(True)
        self.context = object()
        self.list_torrents = Mock()
        self.rdt_delete = Mock()
        self.rdt_cleanup_finished = Mock()
        self.qbit_add_magnet = Mock(return_value={"engine": "qBittorrent", "hash": "f" * 40})
        self.qbit_add_torrent = Mock(return_value={"engine": "qBittorrent", "hash": "e" * 40})
        self.submissions_update = Mock()
        self.store = MonitorStore(
            self.root / "monitor_state.json",
            self.root / "monitor_torrents",
            self.logger,
        )
        self.store.cleanup_artifact = Mock(wraps=self.store.cleanup_artifact)
        self.monitor = RdtMonitor(
            store=self.store,
            ports=RdtMonitorPorts(
                load_settings=lambda: self.settings,
                list_torrents=self.list_torrents,
                rdt_delete=self.rdt_delete,
                rdt_cleanup_finished=self.rdt_cleanup_finished,
                qbit_add_magnet=self.qbit_add_magnet,
                qbit_add_torrent=self.qbit_add_torrent,
                submissions_update=self.submissions_update,
                normalized_category=lambda value: value if value in {"movies", "tv", "manual"} else "manual",
                magnet_hash=lambda value: value.rsplit(":", 1)[-1].lower(),
                torrent_info=lambda raw: {"hash": "d" * 40},
                now=self.clock,
            ),
            logger=self.logger,
            finished_cleanup_delay_sec=30,
            orphan_cleanup_sec=6 * 60 * 60,
            visual_cache_ttl_sec=5,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def listing(self, rows: list[dict]) -> TorrentListing:
        return TorrentListing(context=self.context, rows=tuple(rows))

    def add_item(
        self,
        torrent_id: str = "rdt-a",
        info_hash: str = "a" * 40,
        **changes,
    ) -> dict:
        item = {
            "rdt_id": torrent_id,
            "title": f"Titulo {torrent_id}",
            "category": "movies",
            "first_seen": int(self.clock()) - 60,
            "last_progress_ts": int(self.clock()) - 60,
            "last_progress": -1.0,
            "last_status": "Waiting",
            "finished_seen_ts": 0,
            "submission_key": f"submission-{torrent_id}",
            "trace_id": f"trace-{torrent_id}",
            "kind": "magnet",
            "magnet": f"magnet:?xt=urn:btih:{info_hash}",
            "hash": info_hash,
        }
        item.update(changes)
        self.store.register_item(item)
        return item

    def test_empty_state_skips_the_local_list_for_poll_and_visual_snapshot(self) -> None:
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot(), {})
        self.list_torrents.assert_not_called()

    def test_two_items_share_one_local_list_indexed_by_id_and_hash(self) -> None:
        self.add_item("rdt-a", "a" * 40)
        self.add_item("rdt-b", "b" * 40)
        self.list_torrents.return_value = self.listing(
            [
                {
                    "torrentId": "RDT-A",
                    "hash": "a" * 40,
                    "statusText": "Downloading (12%)",
                    "downloadsCount": 1,
                },
                {
                    "torrentId": "renamed-b",
                    "hash": "b" * 40,
                    "statusText": "Downloading (34%)",
                    "downloadsCount": 1,
                },
            ]
        )

        self.monitor.poll_once()

        self.list_torrents.assert_called_once_with()
        self.assertEqual(set(self.store.snapshot()), {"rdt-a", "rdt-b"})
        self.qbit_add_magnet.assert_not_called()
        self.rdt_delete.assert_not_called()

    def test_visual_sequence_null_comma_point_and_terminal_never_touches_decision_fields(self) -> None:
        original = self.add_item()
        before = self.store.state_path.read_bytes()
        self.list_torrents.side_effect = [
            self.listing([{"torrentId": "rdt-a", "statusText": "Preparing", "downloadsCount": 0}]),
            self.listing([{"torrentId": "rdt-a", "statusText": "Downloading (18,5%)", "downloadsCount": 1}]),
            self.listing([{"torrentId": "rdt-a", "statusText": "Downloading (62.0%)", "downloadsCount": 1}]),
            self.listing([{"torrentId": "rdt-a", "statusText": "Finished", "downloadsCount": 1}]),
        ]

        observed = []
        for index in range(4):
            if index:
                self.clock.advance(6)
            observed.append(self.monitor.snapshot()["rdt-a"])

        self.assertEqual([row["progress"] for row in observed], [None, 18.5, 62.0, 100.0])
        self.assertTrue(all(row["progress_stale"] is False for row in observed))
        self.assertEqual(self.list_torrents.call_count, 4)
        self.assertEqual(self.store.state_path.read_bytes(), before)
        stored = self.store.snapshot()["rdt-a"]
        self.assertEqual(stored["last_progress"], original["last_progress"])
        self.assertEqual(stored["last_progress_ts"], original["last_progress_ts"])
        self.assertNotIn("progress", stored)

    def test_visual_cache_makes_at_most_one_list_call_per_five_second_window(self) -> None:
        self.add_item()
        self.list_torrents.return_value = self.listing(
            [{"torrentId": "rdt-a", "statusText": "Downloading (21%)", "downloadsCount": 1}]
        )

        first = self.monitor.snapshot()["rdt-a"]
        second = self.monitor.snapshot()["rdt-a"]

        self.assertEqual(first["progress"], 21.0)
        self.assertEqual(second["progress"], 21.0)
        self.list_torrents.assert_called_once_with()

    def test_visual_list_failure_preserves_previous_measurement_as_stale_without_writing(self) -> None:
        self.add_item()
        self.list_torrents.side_effect = [
            self.listing([{"torrentId": "rdt-a", "statusText": "Downloading (18,5%)"}]),
            RuntimeError("RDT local unavailable"),
        ]
        first = self.monitor.snapshot()["rdt-a"]
        before = self.store.state_path.read_bytes()
        self.clock.advance(6)

        stale = self.monitor.snapshot()["rdt-a"]

        self.assertEqual(stale["progress"], 18.5)
        self.assertEqual(stale["progress_observed_at"], first["progress_observed_at"])
        self.assertTrue(stale["progress_stale"])
        self.assertEqual(self.store.state_path.read_bytes(), before)
        self.qbit_add_magnet.assert_not_called()
        self.rdt_cleanup_finished.assert_not_called()

    def test_visual_list_failure_without_previous_observation_is_unknown_but_not_stale(self) -> None:
        self.add_item()
        self.list_torrents.side_effect = RuntimeError("RDT local unavailable")

        row = self.monitor.snapshot()["rdt-a"]

        self.assertIsNone(row["progress"])
        self.assertIsNone(row["progress_observed_at"])
        self.assertFalse(row["progress_stale"])

    def test_fresh_row_without_percentage_refreshes_a_previous_unknown_observation(self) -> None:
        self.add_item()
        self.list_torrents.side_effect = [
            self.listing([{"torrentId": "rdt-a", "statusText": "Preparing"}]),
            self.listing([{"torrentId": "rdt-a", "statusText": "Queued locally"}]),
        ]
        first = self.monitor.snapshot()["rdt-a"]
        self.clock.advance(6)

        second = self.monitor.snapshot()["rdt-a"]

        self.assertIsNone(first["progress"])
        self.assertIsNone(second["progress"])
        self.assertEqual(second["progress_status"], "Queued locally")
        self.assertGreater(second["progress_observed_at"], first["progress_observed_at"])
        self.assertFalse(second["progress_stale"])

    def test_visual_missing_row_is_only_presentational_and_never_invents_one_hundred(self) -> None:
        self.add_item()
        before = self.store.state_path.read_bytes()
        self.list_torrents.return_value = self.listing([])

        row = self.monitor.snapshot()["rdt-a"]

        self.assertIsNone(row["progress"])
        self.assertFalse(row["progress_stale"])
        self.assertEqual(self.store.state_path.read_bytes(), before)
        self.qbit_add_magnet.assert_not_called()
        self.rdt_delete.assert_not_called()
        self.store.cleanup_artifact.assert_not_called()

    def test_visual_missing_row_preserves_a_previous_percentage_as_stale(self) -> None:
        self.add_item()
        self.list_torrents.side_effect = [
            self.listing([{"torrentId": "rdt-a", "statusText": "Downloading (44%)"}]),
            self.listing([]),
        ]
        first = self.monitor.snapshot()["rdt-a"]
        self.clock.advance(6)

        missing = self.monitor.snapshot()["rdt-a"]

        self.assertEqual(missing["progress"], 44.0)
        self.assertEqual(missing["progress_observed_at"], first["progress_observed_at"])
        self.assertTrue(missing["progress_stale"])

    def test_visual_nonterminal_hundred_is_held_below_terminal_value(self) -> None:
        self.add_item()
        self.list_torrents.return_value = self.listing(
            [{"torrentId": "rdt-a", "statusText": "Finalizing (100%)", "completed": False}]
        )

        row = self.monitor.snapshot()["rdt-a"]

        self.assertEqual(row["progress"], 99.0)
        self.assertFalse(row["progress_stale"])

    def test_poll_list_failure_preserves_state_byte_for_byte_without_effects_or_last_error(self) -> None:
        self.add_item()
        before = self.store.state_path.read_bytes()
        self.list_torrents.side_effect = RuntimeError("RDT local unavailable")

        self.monitor.poll_once()

        self.assertEqual(self.store.state_path.read_bytes(), before)
        self.assertNotIn("last_error", self.store.snapshot()["rdt-a"])
        self.qbit_add_magnet.assert_not_called()
        self.rdt_delete.assert_not_called()
        self.rdt_cleanup_finished.assert_not_called()
        self.store.cleanup_artifact.assert_not_called()

    def test_poll_missing_row_falls_back_once_when_enabled(self) -> None:
        self.add_item()
        self.list_torrents.return_value = self.listing([])

        self.monitor.poll_once()

        self.assertEqual(self.store.snapshot(), {})
        self.list_torrents.assert_called_once_with()
        self.qbit_add_magnet.assert_called_once()
        self.rdt_delete.assert_called_once_with(self.context, "rdt-a")
        self.store.cleanup_artifact.assert_called_once()

    def test_poll_missing_row_stays_untouched_when_fallback_is_disabled(self) -> None:
        self.settings = monitor_settings(False)
        self.add_item()
        before = self.store.state_path.read_bytes()
        self.list_torrents.return_value = self.listing([])

        self.monitor.poll_once()

        self.assertEqual(self.store.state_path.read_bytes(), before)
        self.qbit_add_magnet.assert_not_called()
        self.rdt_delete.assert_not_called()

    def test_poll_missing_persisted_terminal_finalizes_without_qbit(self) -> None:
        self.add_item(finished_seen_ts=int(self.clock()) - 40, completed=True)
        self.list_torrents.return_value = self.listing([])

        self.monitor.poll_once()

        self.assertEqual(self.store.snapshot(), {})
        self.qbit_add_magnet.assert_not_called()
        self.rdt_cleanup_finished.assert_not_called()
        self.submissions_update.assert_called_once()
        self.assertEqual(self.submissions_update.call_args.kwargs["state"], "transport_done")

    def test_terminal_cleanup_keeps_29_second_boundary_and_removes_at_30(self) -> None:
        for elapsed, should_remove in ((29, False), (30, True)):
            with self.subTest(elapsed=elapsed):
                self.tearDown()
                self.setUp()
                self.add_item(finished_seen_ts=int(self.clock()) - elapsed)
                self.list_torrents.return_value = self.listing(
                    [{"torrentId": "rdt-a", "statusText": "error", "completed": False}]
                )

                self.monitor.poll_once()

                if should_remove:
                    self.assertEqual(self.store.snapshot(), {})
                    self.rdt_cleanup_finished.assert_called_once_with(self.context, "rdt-a")
                else:
                    self.assertIn("rdt-a", self.store.snapshot())
                    self.rdt_cleanup_finished.assert_not_called()
                self.qbit_add_magnet.assert_not_called()

    def test_finished_status_is_terminal_even_without_completed_flag(self) -> None:
        self.add_item()
        self.list_torrents.return_value = self.listing(
            [{"torrentId": "rdt-a", "statusText": "Finished", "completed": False}]
        )

        self.monitor.poll_once()

        stored = self.store.snapshot()["rdt-a"]
        self.assertTrue(stored["completed"])
        self.assertEqual(stored["last_progress"], 100.0)
        self.assertEqual(stored["finished_seen_ts"], int(self.clock()))
        self.qbit_add_magnet.assert_not_called()

    def test_provider_completed_waits_for_local_download_before_terminal_progress(self) -> None:
        self.add_item()
        waiting = {
            "torrentId": "rdt-a",
            "statusText": "Torrent finished, waiting for download links downloaded",
            "rdProgress": 100,
            "downloadsCount": 1,
            "completed": True,
        }
        self.list_torrents.return_value = self.listing([waiting])

        self.monitor.poll_once()
        preparing = self.monitor.snapshot()["rdt-a"]

        self.assertFalse(is_finished(waiting))
        self.assertIsNone(preparing["progress"])
        self.assertEqual(self.store.snapshot()["rdt-a"]["finished_seen_ts"], 0)
        self.rdt_cleanup_finished.assert_not_called()
        self.qbit_add_magnet.assert_not_called()

        self.clock.advance(6)
        queued = {**waiting, "statusText": "Queued for downloading downloaded"}
        self.list_torrents.return_value = self.listing([queued])

        queued_visual = self.monitor.snapshot()["rdt-a"]

        self.assertIsNone(queued_visual["progress"])

        self.clock.advance(6)
        downloading = {
            **waiting,
            "statusText": "Downloading file 1/1 (26.87% - 265.69 MB/s) downloaded",
        }
        self.list_torrents.return_value = self.listing([downloading])

        observed = self.monitor.snapshot()["rdt-a"]

        self.assertEqual(observed["progress"], 26.87)
        self.assertFalse(is_finished(downloading))

        self.clock.advance(6)
        finished = {**waiting, "statusText": "Finished downloaded"}
        self.list_torrents.return_value = self.listing([finished])

        terminal = self.monitor.snapshot()["rdt-a"]

        self.assertTrue(is_finished(finished))
        self.assertEqual(terminal["progress"], 100.0)

    def test_files_downloaded_to_host_is_not_terminal_and_never_exposes_one_hundred(self) -> None:
        self.add_item()
        self.list_torrents.return_value = self.listing(
            [
                {
                    "torrentId": "rdt-a",
                    "statusText": "Files downloaded to host",
                    "rdProgress": 100,
                    "downloadsCount": 1,
                    "completed": False,
                }
            ]
        )

        self.monitor.poll_once()
        visual = self.monitor.snapshot()["rdt-a"]

        stored = self.store.snapshot()["rdt-a"]
        self.assertEqual(stored["finished_seen_ts"], 0)
        self.assertNotIn("completed", stored)
        self.assertIsNone(visual["progress"])
        self.assertFalse(is_finished({"statusText": "Files downloaded to host"}))
        self.rdt_cleanup_finished.assert_not_called()

    def test_existing_error_row_preserves_fallback_behavior(self) -> None:
        self.add_item()
        self.list_torrents.return_value = self.listing(
            [{"torrentId": "rdt-a", "statusText": "Failed: host error", "completed": False}]
        )

        self.monitor.poll_once()

        self.assertEqual(self.store.snapshot(), {})
        self.qbit_add_magnet.assert_called_once()
        self.rdt_delete.assert_called_once_with(self.context, "rdt-a")

    def test_delta_merge_preserves_registration_arriving_during_the_list_call(self) -> None:
        self.add_item("rdt-a", "a" * 40)

        def listing_with_concurrent_registration() -> TorrentListing:
            self.add_item("rdt-b", "b" * 40)
            return self.listing(
                [{"torrentId": "rdt-a", "statusText": "Waiting (5%)", "downloadsCount": 0}]
            )

        self.list_torrents.side_effect = listing_with_concurrent_registration

        self.monitor.poll_once()

        stored = self.store.snapshot()
        self.assertEqual(set(stored), {"rdt-a", "rdt-b"})
        self.assertEqual(stored["rdt-a"]["last_progress"], 5.0)
        self.assertEqual(stored["rdt-b"]["last_progress"], -1.0)

    def test_old_delta_is_rejected_when_same_id_is_replaced_during_network_call(self) -> None:
        self.add_item("rdt-a", "a" * 40, first_seen=100, last_progress_ts=100)

        def listing_with_replacement() -> TorrentListing:
            self.add_item(
                "rdt-a",
                "b" * 40,
                first_seen=200,
                last_progress_ts=200,
                title="Reemplazo",
            )
            return self.listing(
                [{"torrentId": "rdt-a", "hash": "a" * 40, "statusText": "Waiting (7%)"}]
            )

        self.list_torrents.side_effect = listing_with_replacement

        self.monitor.poll_once()

        stored = self.store.snapshot()["rdt-a"]
        self.assertEqual(stored["title"], "Reemplazo")
        self.assertEqual(stored["hash"], "b" * 40)
        self.assertEqual(stored["last_progress"], -1.0)
        self.assertEqual(stored["last_progress_ts"], 200)


class RdtMonitorStructureTests(unittest.TestCase):
    def test_only_rdt_wait_ready_keeps_the_productive_per_id_endpoint(self) -> None:
        service_root = Path(__file__).resolve().parents[1]
        productive_sources = [
            path
            for path in service_root.rglob("*.py")
            if "tests" not in path.parts and "__pycache__" not in path.parts
        ]
        matches = []
        for path in productive_sources:
            source = path.read_text(encoding="utf-8")
            for _ in range(source.count("/Api/Torrents/Get/")):
                matches.append(path)

        self.assertEqual(matches, [service_root / "app.py"])
        app_source = (service_root / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(app_source)
        wait_ready = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "rdt_wait_ready"
        )
        self.assertIn("/Api/Torrents/Get/", ast.get_source_segment(app_source, wait_ready))

        monitor_root = service_root / "modulos" / "rdt_monitor"
        monitor_source = "\n".join(path.read_text(encoding="utf-8") for path in monitor_root.glob("*.py"))
        self.assertNotIn("/Api/Torrents/Get/", monitor_source)
        ports_source = (monitor_root / "ports.py").read_text(encoding="utf-8")
        self.assertNotIn("rdt_json", ports_source)
        self.assertNotIn("rdt_find_row", ports_source)


class RdtMonitorProgressParsingTests(unittest.TestCase):
    def test_local_status_percentage_wins_over_terminal_rd_progress(self) -> None:
        row = {
            "statusText": "Downloading file 1/1 (18.50% - 2 MB/s)",
            "rdProgress": 100,
        }

        self.assertEqual(status_progress(row), 18.5)

    def test_rd_progress_is_already_a_percentage_not_a_fraction(self) -> None:
        self.assertEqual(status_progress({"rdProgress": 0.5}), 0.5)
        self.assertEqual(status_progress({"RdProgress": "0,5"}), 0.5)


if __name__ == "__main__":
    unittest.main()
