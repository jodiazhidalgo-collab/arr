import copy
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from arr_orchestrator.db import Database
from arr_orchestrator.engine import Engine
from arr_orchestrator.identity.fingerprint import identity_fingerprint
from test_core import test_config


RUNTIME_TEST_ROOT = Path(os.environ["ARR_PYTEST_DATA_DIR"])


class IdentityEngineIntegrationTests(unittest.TestCase):
    @staticmethod
    def _movie_1875_snapshot(engine: Engine, revision: int = 7):
        snapshot = engine.identity.job_snapshot()
        snapshot["revision"] = revision
        snapshot["rules"]["parser"]["year"].update(
            {
                "pattern": r"(?<!\d)((?:18|19|20)\d{2})(?!\d)",
                "min": 1800,
            }
        )
        snapshot["fingerprint"] = identity_fingerprint(snapshot["rules"])
        return snapshot

    def test_job_keeps_identity_snapshot_across_save_and_restart(self) -> None:
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT) as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database_path = root / "orchestrator.db"
            database = Database(database_path)
            database.initialize()
            engine = Engine(config, database)

            source_meta = engine._new_job_source_meta_json()
            job = database.create_job(
                "identity:snapshot:old",
                "test",
                "movies",
                "Pelicula.2024.mkv",
                source_meta_json=source_meta,
            )
            draft = engine.identity_rules()["rules"]
            draft["resolver"]["acceptance"]["min_score"] = 60
            saved = engine.update_identity_rules(
                {"rules": draft, "expected_revision": 0}
            )
            self.assertTrue(saved["ok"])
            database.close()

            restarted_database = Database(database_path)
            restarted_database.initialize()
            restarted = Engine(config, restarted_database)
            old_job = restarted_database.get_job(job["job_id"])
            old_context = restarted.identity.rules_for_job(old_job)
            new_snapshot = json.loads(restarted._new_job_source_meta_json())["identity_rules"]

            self.assertEqual(old_context["revision"], 0)
            self.assertEqual(
                old_context["rules"]["resolver"]["acceptance"]["min_score"],
                75,
            )
            self.assertEqual(new_snapshot["revision"], 1)
            self.assertEqual(
                new_snapshot["rules"]["resolver"]["acceptance"]["min_score"],
                60,
            )
            restarted_database.close()

    def test_reconcile_qbt_uses_common_then_persists_movies_snapshot(self) -> None:
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT) as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "orchestrator.db")
            database.initialize()
            engine = Engine(config, database)
            item = config.complete_root / "manual" / "Obra.1875"
            content = item / "Obra.1875.mkv"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")
            common_snapshot = self._movie_1875_snapshot(engine)
            movie_snapshot = engine.identity.job_snapshot("movies")
            movie_snapshot["revision"] = 17
            snapshot_calls = []

            def common_for_classification():
                snapshot_calls.append("common")
                return copy.deepcopy(common_snapshot)

            def category_snapshot(category):
                snapshot_calls.append(category)
                self.assertEqual(category, "movies")
                return copy.deepcopy(movie_snapshot)

            engine.identity.classification_snapshot = common_for_classification
            engine.identity.job_snapshot_for_category = category_snapshot

            class FakeQbt:
                def torrents(self, _torrent_filter):
                    return [
                        {
                            "hash": "a" * 40,
                            "category": "",
                            "name": "Obra.1875.mkv",
                            "content_path": str(content),
                            "added_on": 100,
                        }
                    ]

            engine.qbt = FakeQbt()
            engine._reconcile_qbt()

            jobs = database.latest_jobs()
            stored = json.loads(jobs[0]["source_meta_json"])["identity_rules"]
            self.assertEqual(snapshot_calls, ["common", "movies"])
            self.assertEqual(jobs[0]["category"], "movies")
            self.assertEqual(stored, movie_snapshot)
            database.close()

    def test_qbt_event_uses_common_then_persists_movies_snapshot(self) -> None:
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT) as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "orchestrator.db")
            database.initialize()
            engine = Engine(config, database)
            item = config.complete_root / "manual" / "Obra.1875"
            content = item / "Obra.1875.mkv"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")
            infohash = "b" * 40
            event_path = config.event_dir / "obra.event"
            event_path.write_text(f"hash={infohash}\n", encoding="utf-8")
            common_snapshot = self._movie_1875_snapshot(engine, revision=8)
            movie_snapshot = engine.identity.job_snapshot("movies")
            movie_snapshot["revision"] = 18
            snapshot_calls = []

            def common_for_classification():
                snapshot_calls.append("common")
                return copy.deepcopy(common_snapshot)

            def category_snapshot(category):
                snapshot_calls.append(category)
                self.assertEqual(category, "movies")
                return copy.deepcopy(movie_snapshot)

            engine.identity.classification_snapshot = common_for_classification
            engine.identity.job_snapshot_for_category = category_snapshot

            class FakeQbt:
                def torrent(self, _infohash):
                    return {
                        "hash": infohash,
                        "category": "",
                        "name": "Obra.1875.mkv",
                        "content_path": str(content),
                        "progress": 1,
                        "completion_on": 123,
                        "added_on": 100,
                    }

            engine.qbt = FakeQbt()
            engine._handle_qbt_event(event_path)

            jobs = database.latest_jobs()
            stored = json.loads(jobs[0]["source_meta_json"])["identity_rules"]
            self.assertEqual(snapshot_calls, ["common", "movies"])
            self.assertEqual(jobs[0]["category"], "movies")
            self.assertEqual(stored, movie_snapshot)
            self.assertFalse(event_path.exists())
            database.close()

    def test_watch_uses_common_then_persists_movies_snapshot(self) -> None:
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT) as temporary:
            root = Path(temporary)
            config = replace(test_config(root), mode="dry-run")
            config.ensure_directories()
            database = Database(root / "orchestrator.db")
            database.initialize()
            engine = Engine(config, database)
            torrent_path = config.watch_inbox / "Obra.1875.torrent"
            torrent_path.parent.mkdir(parents=True, exist_ok=True)
            torrent_path.write_bytes(b"torrent")
            common_snapshot = self._movie_1875_snapshot(engine, revision=11)
            movie_snapshot = engine.identity.job_snapshot("movies")
            movie_snapshot["revision"] = 19
            snapshot_calls = []

            def common_for_classification():
                snapshot_calls.append("common")
                return copy.deepcopy(common_snapshot)

            def category_snapshot(category):
                snapshot_calls.append(category)
                self.assertEqual(category, "movies")
                return copy.deepcopy(movie_snapshot)

            engine.identity.classification_snapshot = common_for_classification
            engine.identity.job_snapshot_for_category = category_snapshot
            with patch(
                "arr_orchestrator.engine.torrent_info",
                return_value=("e" * 40, "Obra.1875.mkv"),
            ):
                engine._handle_watch_path(torrent_path)

            jobs = database.latest_jobs()
            stored = json.loads(jobs[0]["source_meta_json"])["identity_rules"]
            self.assertEqual(snapshot_calls, ["common", "movies"])
            self.assertEqual(jobs[0]["category"], "movies")
            self.assertEqual(stored, movie_snapshot)
            database.close()

    def test_existing_qbt_job_uses_its_frozen_identity_snapshot(self) -> None:
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT) as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "orchestrator.db")
            database.initialize()
            engine = Engine(config, database)
            item = config.complete_root / "manual" / "Obra.1875"
            content = item / "Obra.1875.mkv"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")
            infohash = "c" * 40
            frozen = self._movie_1875_snapshot(engine, revision=9)
            frozen.pop("profile", None)
            job = database.create_job(
                "qbt:frozen:identity",
                "qbt",
                "manual",
                "Obra.1875.mkv",
                state="waiting_stable",
                source_path=str(item),
                source_meta_json=engine._new_job_source_meta_json(
                    identity_context=frozen
                ),
            )

            def no_current_snapshot():
                raise AssertionError("se consulto la identidad actual para un job congelado")

            engine.identity.job_snapshot = no_current_snapshot

            class FakeQbt:
                def torrents(self, _torrent_filter):
                    return [
                        {
                            "hash": infohash,
                            "category": "",
                            "name": "Obra.1875.mkv",
                            "content_path": str(content),
                            "added_on": 100,
                        }
                    ]

            engine.qbt = FakeQbt()
            engine._reconcile_qbt()

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["category"], "movies")
            database.close()

    def test_materialized_qbt_adoption_uses_the_jobs_frozen_parser(self) -> None:
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT) as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "orchestrator.db")
            database.initialize()
            engine = Engine(config, database)
            item = config.complete_root / "manual" / "Obra.1875"
            content = item / "Obra.1875.mkv"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")
            frozen = self._movie_1875_snapshot(engine, revision=10)
            frozen.pop("profile", None)
            job = database.create_job(
                "fs:frozen:adoption",
                "fs",
                "manual",
                "Obra.1875.mkv",
                state="waiting_stable",
                source_path=str(item),
                source_meta_json=engine._new_job_source_meta_json(
                    identity_context=frozen
                ),
            )

            def no_current_snapshot():
                raise AssertionError("se consulto la identidad actual durante la adopcion")

            engine.identity.job_snapshot = no_current_snapshot

            class FakeQbt:
                def torrents(self, _torrent_filter):
                    return [
                        {
                            "hash": "d" * 40,
                            "category": "",
                            "name": "Obra.1875.mkv",
                            "content_path": str(content),
                            "added_on": 100,
                        }
                    ]

            engine.qbt = FakeQbt()
            engine._register_materialized("manual", item)

            updated = database.get_job(job["job_id"])
            self.assertEqual(updated["category"], "movies")
            self.assertEqual(updated["qbt_hash"], "d" * 40)
            database.close()

    def test_new_manual_job_freezes_movies_profile_when_category_arrives(self) -> None:
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT) as temporary:
            root = Path(temporary)
            config = test_config(root)
            config.ensure_directories()
            database = Database(root / "orchestrator.db")
            database.initialize()
            engine = Engine(config, database)
            item = config.complete_root / "manual" / "Obra.1875"
            content = item / "Obra.1875.mkv"
            content.parent.mkdir(parents=True)
            content.write_bytes(b"movie")

            movie_rules = engine.identity_rules("movies")["rules"]
            movie_rules["resolver"]["acceptance"]["min_score"] = 63
            saved = engine.update_identity_rules(
                {"rules": movie_rules, "expected_revision": 0}, "movies"
            )
            common_snapshot = self._movie_1875_snapshot(engine, revision=12)
            job = database.create_job(
                "fs:new:profile-promotion",
                "fs",
                "manual",
                "Obra.1875.mkv",
                state="waiting_stable",
                source_path=str(item),
                source_meta_json=engine._new_job_source_meta_json(
                    identity_context=common_snapshot
                ),
            )

            class FakeQbt:
                def torrents(self, _torrent_filter):
                    return [
                        {
                            "hash": "f" * 40,
                            "category": "",
                            "name": "Obra.1875.mkv",
                            "content_path": str(content),
                            "added_on": 100,
                        }
                    ]

            engine.qbt = FakeQbt()
            engine._register_materialized("manual", item)

            updated = database.get_job(job["job_id"])
            stored = json.loads(updated["source_meta_json"])["identity_rules"]
            self.assertEqual(updated["category"], "movies")
            self.assertEqual(stored["profile"], "movies")
            self.assertEqual(stored["revision"], saved["revision"])
            self.assertEqual(
                stored["rules"]["resolver"]["acceptance"]["min_score"], 63
            )
            database.close()


if __name__ == "__main__":
    unittest.main()
