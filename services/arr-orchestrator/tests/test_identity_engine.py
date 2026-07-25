import json
import tempfile
import unittest
from pathlib import Path

from arr_orchestrator.db import Database
from arr_orchestrator.engine import Engine
from test_core import test_config


RUNTIME_TEST_ROOT = Path(__file__).resolve().parents[3] / "_codex_runtime" / "test-data"


class IdentityEngineIntegrationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
