import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from arr_orchestrator.engine import Engine
from arr_orchestrator.watchers import EventHandler, WatcherEventInbox


def filesystem_event(
    event_type: str,
    src_path: Path,
    dest_path: Path | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_type=event_type,
        src_path=str(src_path),
        dest_path=str(dest_path) if dest_path is not None else "",
    )


def bare_engine(events: WatcherEventInbox) -> Engine:
    engine = object.__new__(Engine)
    engine.events = events
    engine.log = Mock()
    engine._handle_watch_path = Mock()
    engine._handle_qbt_event = Mock()
    engine._handle_complete_path = Mock()
    return engine


class WatcherEventInboxTests(unittest.TestCase):
    def test_fifty_thousand_inner_events_become_one_immediate_item(self) -> None:
        root = Path("/data/downloads/torrents/complete/tv")
        inbox = WatcherEventInbox(2048)
        handler = EventHandler(inbox, "complete", collapse_root=root)

        for index in range(50_000):
            handler.on_any_event(
                filesystem_event(
                    "modified",
                    root / "Serie" / "Season 01" / f"fragment-{index}.tmp",
                )
            )

        stats = inbox.stats()
        self.assertEqual(inbox.qsize(), 1)
        self.assertEqual(stats["received"], 50_000)
        self.assertEqual(stats["coalesced"], 49_999)
        self.assertEqual(stats["high_watermark"], 1)
        event = inbox.get_nowait()
        self.assertEqual(event.path, root / "Serie")

        self.assertFalse(inbox.offer("complete", root / "Serie"))
        inbox.acknowledge(event)
        self.assertTrue(inbox.offer("complete", root / "Serie"))

    def test_capacity_is_bounded_and_requests_lossless_reconcile(self) -> None:
        inbox = WatcherEventInbox(2048)

        for index in range(2049):
            inbox.offer("watch", Path(f"/watch/item-{index}.torrent"))

        stats = inbox.stats()
        self.assertEqual(inbox.qsize(), 2048)
        self.assertEqual(stats["overflowed"], 1)
        self.assertEqual(stats["high_watermark"], 2048)
        self.assertTrue(stats["reconcile_requested"])
        ticket = inbox.reconcile_ticket()
        self.assertGreater(ticket, 0)
        inbox.acknowledge_reconcile(ticket)
        self.assertFalse(inbox.stats()["reconcile_requested"])

    def test_move_source_and_destination_are_coalesced_by_top_item(self) -> None:
        root = Path("/data/downloads/torrents/complete/movies")
        inbox = WatcherEventInbox(8)
        handler = EventHandler(inbox, "complete", collapse_root=root)

        handler.on_any_event(
            filesystem_event(
                "moved",
                root / "Pelicula" / "temporary.mkv",
                root / "Pelicula" / "Pelicula.mkv",
            )
        )

        self.assertEqual(inbox.qsize(), 1)
        self.assertEqual(inbox.stats()["received"], 2)
        self.assertEqual(inbox.stats()["coalesced"], 1)

    def test_engine_drain_keeps_pending_key_until_handler_finishes(self) -> None:
        inbox = WatcherEventInbox(8)
        inbox.offer("complete", Path("/complete/Serie"))
        engine = bare_engine(inbox)

        def handle(path: Path) -> None:
            self.assertFalse(inbox.offer("complete", path))

        engine._handle_complete_path.side_effect = handle
        with patch("arr_orchestrator.engine.time.monotonic", return_value=0.0):
            engine._drain_events()

        engine._handle_complete_path.assert_called_once_with(Path("/complete/Serie"))
        self.assertEqual(inbox.qsize(), 0)
        self.assertTrue(inbox.offer("complete", Path("/complete/Serie")))

    def test_engine_drain_respects_event_and_time_budgets(self) -> None:
        inbox = WatcherEventInbox(700)
        for index in range(600):
            inbox.offer("watch", Path(f"/watch/{index}.torrent"))
        engine = bare_engine(inbox)

        with patch("arr_orchestrator.engine.time.monotonic", return_value=0.0):
            engine._drain_events()
        self.assertEqual(engine._handle_watch_path.call_count, 500)
        self.assertEqual(inbox.qsize(), 100)

        timed_inbox = WatcherEventInbox(8)
        for index in range(3):
            timed_inbox.offer("watch", Path(f"/watch/timed-{index}.torrent"))
        timed_engine = bare_engine(timed_inbox)
        with patch(
            "arr_orchestrator.engine.time.monotonic",
            side_effect=[100.0, 100.030],
        ):
            timed_engine._drain_events()
        self.assertEqual(timed_engine._handle_watch_path.call_count, 1)
        self.assertEqual(timed_inbox.qsize(), 2)

    def test_overflow_forces_one_immediate_reconcile(self) -> None:
        inbox = WatcherEventInbox(1)
        inbox.offer("watch", Path("/watch/first.torrent"))
        inbox.offer("watch", Path("/watch/overflow.torrent"))
        engine = bare_engine(inbox)
        engine.config = SimpleNamespace(reconcile_seconds=30)
        engine._last_reconcile = 100.0
        engine.reconcile = Mock()

        engine._reconcile_if_due(101.0)

        engine.reconcile.assert_called_once_with()
        self.assertEqual(engine._last_reconcile, 101.0)
        self.assertFalse(inbox.stats()["reconcile_requested"])


if __name__ == "__main__":
    unittest.main()
