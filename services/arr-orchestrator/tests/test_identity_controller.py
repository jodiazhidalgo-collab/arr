import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from arr_orchestrator.db import Database
from arr_orchestrator.identity.controller import IdentityController
from arr_orchestrator.identity.fingerprint import identity_fingerprint


RUNTIME_TEST_ROOT = Path(__file__).resolve().parents[3] / "_codex_runtime" / "test-data"


def _config(**overrides):
    values = {
        "resolver_language": "es-ES",
        "resolver_region": "ES",
        "tmdb_api_token": "",
        "resolver_http_timeout_ms": 2500,
        "resolver_total_budget_ms": 5000,
        "resolver_retry_seconds": 60,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FileBotSettings:
    def __init__(self, rules=None) -> None:
        self._rules = rules or {}

    def snapshot(self):
        return copy.deepcopy(self._rules)


class IdentityControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT)
        self.database = Database(Path(self.temporary.name) / "orchestrator.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def test_parser_test_uses_unsaved_draft_and_returns_trace(self) -> None:
        controller = IdentityController(_config(), self.database, _FileBotSettings())
        document = controller.payload()
        draft = copy.deepcopy(document["rules"])
        draft["parser"]["site_words"].append("MarcaPrueba")

        tested = controller.test_parser(
            {
                "name": "Pelicula.MarcaPrueba.2024.1080p.mkv",
                "category": "auto",
                "rules": draft,
            }
        )

        self.assertTrue(tested["ok"])
        self.assertEqual(tested["status"], "CLEAN")
        self.assertEqual(tested["result"]["title"], "Pelicula")
        self.assertTrue(tested["result"]["steps"])
        self.assertEqual(controller.payload()["revision"], 0)

    def test_resolver_tester_reports_missing_tmdb_without_side_effects(self) -> None:
        controller = IdentityController(_config(), self.database, _FileBotSettings())

        result = controller.test_resolver(
            {"name": "Blade.Runner.1982.1080p.mkv", "category": "movies"}
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "TMDB_UNAVAILABLE")
        self.assertEqual(result["decision"]["status"], "TMDB_UNAVAILABLE")
        self.assertFalse(result["decision"]["has_scoring"])
        self.assertEqual(self.database.resolver_cache_stats()["total"], 0)

    def test_resolver_tester_returns_human_contract_for_invalid_draft(self) -> None:
        controller = IdentityController(_config(), self.database, _FileBotSettings())
        draft = controller.payload()["rules"]
        draft["resolver"]["acceptance"]["min_margin"] = "no-es-un-numero"

        result = controller.test_resolver(
            {
                "name": "Blade.Runner.1982.1080p.mkv",
                "category": "movies",
                "rules": draft,
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "INVALID_RULES")
        self.assertEqual(result["error"], "invalid_rules")
        self.assertEqual(
            result["decision"],
            {
                "status": "INVALID_RULES",
                "accepted": False,
                "has_scoring": False,
                "bypass": False,
            },
        )

    def test_revision_zero_defaults_come_from_runtime_config(self) -> None:
        controller = IdentityController(
            _config(
                resolver_http_timeout_ms=1300,
                resolver_total_budget_ms=7200,
                resolver_retry_seconds=41,
            ),
            self.database,
            _FileBotSettings(),
        )

        payload = controller.payload()
        self.assertEqual(payload["revision"], 0)
        self.assertEqual(payload["rules"]["resolver"]["http"]["timeout_ms"], 1300)
        self.assertEqual(
            payload["rules"]["resolver"]["http"]["total_budget_ms"],
            7200,
        )
        self.assertEqual(
            payload["rules"]["resolver"]["retry"]["base_seconds"],
            41,
        )

    def test_legacy_identity_fields_migrate_once_from_filebot_settings(self) -> None:
        legacy = {
            "movies": {
                "language": "en-GB",
                "region": "GB",
                "query_aliases": ["Origen | Destination"],
                "forced_matches": ["Alien | 1979 | 348"],
            },
            "tv": {
                "language": "en-US",
                "query_aliases": ["La oficina | The Office"],
                "forced_matches": ["The Office | 2316"],
            },
        }

        controller = IdentityController(
            _config(), self.database, _FileBotSettings(legacy)
        )
        payload = controller.payload()

        self.assertEqual(payload["revision"], 1)
        self.assertEqual(
            payload["rules"]["resolver"]["locales"]["movies"],
            {"language": "en-GB", "region": "GB"},
        )
        self.assertEqual(
            payload["rules"]["resolver"]["aliases"]["tv"],
            ["La oficina | The Office"],
        )

        restarted = IdentityController(
            _config(), self.database, _FileBotSettings({})
        )
        self.assertEqual(restarted.payload()["revision"], 1)

    def test_legacy_migration_never_overwrites_an_existing_invalid_value(self) -> None:
        legacy = {
            "movies": {
                "language": "en-GB",
                "query_aliases": ["Origen | Destination"],
            }
        }

        for raw in ("{invalid", json.dumps({"schema_version": 99})):
            with self.subTest(raw=raw):
                self.database.set_setting("identity.pipeline", raw)
                controller = IdentityController(
                    _config(), self.database, _FileBotSettings(legacy)
                )

                self.assertEqual(self.database.get_setting("identity.pipeline"), raw)
                self.assertEqual(controller.payload()["revision"], 0)
                self.assertTrue(controller.payload()["repair_required"])

    def test_job_snapshot_is_stable_after_later_save(self) -> None:
        controller = IdentityController(_config(), self.database, _FileBotSettings())
        before = controller.job_snapshot()
        changed = controller.payload()["rules"]
        changed["resolver"]["acceptance"]["min_score"] = 60
        saved = controller.update({"rules": changed, "expected_revision": 0})
        self.assertTrue(saved["ok"])

        old_job = {
            "source_meta_json": json.dumps({"identity_rules": before})
        }
        restored = controller.rules_for_job(old_job)

        self.assertEqual(restored["revision"], 0)
        self.assertEqual(
            restored["rules"]["resolver"]["acceptance"]["min_score"], 75
        )
        self.assertEqual(
            controller.payload()["rules"]["resolver"]["acceptance"]["min_score"],
            60,
        )

    def test_legacy_v1_job_snapshot_keeps_old_behavior_and_coherent_fingerprint(self) -> None:
        controller = IdentityController(_config(), self.database, _FileBotSettings())
        legacy_rules = copy.deepcopy(controller.payload()["rules"])
        del legacy_rules["resolver"]["original_language_preference"]
        stale_fingerprint = "sha256:" + ("a" * 64)
        old_job = {
            "source_meta_json": json.dumps(
                {
                    "identity_rules": {
                        "rules": legacy_rules,
                        "revision": 4,
                        "saved_at": "2026-01-01T00:00:00Z",
                        "fingerprint": stale_fingerprint,
                    }
                }
            )
        }

        restored = controller.rules_for_job(old_job)

        self.assertEqual(restored["revision"], 4)
        self.assertEqual(
            restored["rules"]["resolver"]["original_language_preference"],
            {"enabled": False, "language": "en"},
        )
        self.assertNotEqual(restored["fingerprint"], stale_fingerprint)
        self.assertEqual(
            restored["fingerprint"], identity_fingerprint(restored["rules"])
        )

    def test_legacy_filebot_job_snapshot_does_not_gain_language_preference(self) -> None:
        controller = IdentityController(_config(), self.database, _FileBotSettings())
        old_job = {
            "source_meta_json": json.dumps(
                {
                    "filebot_rules": {
                        "rules": {
                            "movies": {
                                "language": "es-ES",
                                "region": "ES",
                                "query_aliases": [],
                                "forced_matches": [],
                            }
                        },
                        "revision": 2,
                    }
                }
            )
        }

        restored = controller.rules_for_job(old_job)

        self.assertEqual(restored["source"], "legacy_filebot_snapshot")
        self.assertFalse(
            restored["rules"]["resolver"]["original_language_preference"]["enabled"]
        )
        self.assertEqual(
            restored["fingerprint"], identity_fingerprint(restored["rules"])
        )


if __name__ == "__main__":
    unittest.main()
