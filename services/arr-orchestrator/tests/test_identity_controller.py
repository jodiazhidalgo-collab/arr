import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arr_orchestrator.db import Database
from arr_orchestrator.identity.controller import IdentityController
from arr_orchestrator.identity.defaults import (
    identity_profile_setting_key,
)
from arr_orchestrator.identity.validation import IdentityRulesValidationError
from arr_orchestrator.name_resolver import ResolverAmbiguous


RUNTIME_TEST_ROOT = Path(os.environ["ARR_PYTEST_DATA_DIR"])


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


class IdentityControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT)
        self.database = Database(Path(self.temporary.name) / "orchestrator.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def assert_legacy_tv_regex_semantics(self, rules) -> None:
        patterns = rules["parser"]["patterns"]

        self.assertIsNotNone(re.search(patterns["series_sxe"], "Serie S01E02"))
        self.assertIsNotNone(
            re.search(patterns["explicit_season"], "Serie Temporada 2")
        )
        self.assertIsNotNone(re.search(patterns["season_pack"], "Serie T02"))
        self.assertIsNone(re.search(patterns["series_sxe"], "Serie 01E02"))
        self.assertIsNone(re.search(patterns["explicit_season"], "Serie Temp 2"))
        self.assertIsNone(re.search(patterns["explicit_season"], "Serie miniserie"))
        self.assertIsNone(re.search(patterns["season_pack"], "Serie S02"))

    def test_parser_test_uses_unsaved_draft_and_returns_trace(self) -> None:
        controller = IdentityController(_config(), self.database)
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
        controller = IdentityController(_config(), self.database)

        result = controller.test_resolver(
            {"name": "Blade.Runner.1982.1080p.mkv", "category": "movies"}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "RETRY_PROVIDER")
        self.assertEqual(result["decision"]["status"], "RETRY_PROVIDER")
        self.assertFalse(result["decision"]["accepted"])
        self.assertFalse(result["decision"]["has_scoring"])
        self.assertEqual(self.database.resolver_cache_stats()["total"], 0)

    def test_resolver_tester_preserves_structured_decision_and_safe_provenance(self) -> None:
        controller = IdentityController(_config(), self.database)
        preview = {
            "ok": True,
            "status": "ACCEPTED_FALLBACK",
            "resolver_algorithm_version": "phased-er-v2",
            "decision": {
                "status": "ACCEPTED_FALLBACK",
                "accepted": True,
                "confidence": "probable",
                "fallback_reason": "ambiguity",
                "coverage_limited": False,
                "selected": {"tmdb_id": 1317149, "title": "I Swear"},
                "alternatives": [{"tmdb_id": 1317149, "title": "I Swear"}],
                "evidence": [],
                "phase_counts": {
                    "discovered": 1,
                    "enriched": 1,
                    "eliminated": 0,
                    "plausible": 1,
                },
                "has_scoring": False,
                "resolver_algorithm_version": "phased-er-v2",
            },
            "candidates": [
                {
                    "tmdb_id": 1317149,
                    "title": "I Swear",
                    "search_provenance": {
                        "sources": ["alternate"],
                        "phases": ["alternate"],
                        "hits": 1,
                    },
                }
            ],
        }

        with patch.object(controller.resolver, "preview", return_value=preview):
            result = controller.test_resolver(
                {
                    "name": "Incontrolable (I Swear) (2025).mkv",
                    "category": "movies",
                }
            )

        self.assertEqual(result["decision"], preview["decision"])
        self.assertEqual(result["candidates"], preview["candidates"])
        self.assertEqual(result["profile"], "common")
        self.assertEqual(
            result["parser_test"]["title_evidence"][0]["value"],
            "Incontrolable",
        )
        self.assertNotIn(
            "query",
            result["candidates"][0]["search_provenance"],
        )

    def test_resolver_tester_keeps_blocked_status_and_reason(self) -> None:
        controller = IdentityController(_config(), self.database)
        reason = "hard_conflict"

        def reject_candidate(resolver, *_args, **_kwargs):
            resolver._trace = {
                "queries": [],
                "candidates": [
                    {
                        "tmdb_id": 300,
                        "eliminated": True,
                        "elimination_reasons": [reason],
                    }
                ],
                "cache_hit": False,
                "decision": {
                    "status": "BLOCKED_HARD",
                    "accepted": False,
                    "has_scoring": False,
                    "resolver_algorithm_version": "phased-er-v2",
                    "fallback_reason": reason,
                    "coverage_limited": False,
                    "selected": None,
                    "alternatives": [],
                    "evidence": [],
                    "phase_counts": {
                        "discovered": 1,
                        "enriched": 1,
                        "eliminated": 1,
                        "plausible": 0,
                    },
                },
            }
            raise ResolverAmbiguous(
                "La identidad presenta un conflicto duro",
                {
                    "reason_code": reason,
                    "status": "BLOCKED_HARD",
                    "resolver_algorithm_version": "phased-er-v2",
                },
            )

        with patch.object(
            type(controller.resolver),
            "resolve",
            autospec=True,
            side_effect=reject_candidate,
        ):
            result = controller.test_resolver(
                {
                    "name": (
                        "Unrelated local title "
                        "(Long Saga Chapter final) (2024).mkv"
                    ),
                    "category": "movies",
                }
            )

        self.assertEqual(result["status"], "BLOCKED_HARD")
        self.assertEqual(result["decision"]["status"], "BLOCKED_HARD")
        self.assertEqual(result["decision"]["fallback_reason"], reason)
        self.assertFalse(result["decision"]["accepted"])
        self.assertEqual(result["details"]["reason_code"], reason)
        self.assertEqual(result["profile"], "common")

    def test_resolver_tester_returns_human_contract_for_invalid_draft(self) -> None:
        controller = IdentityController(_config(), self.database)
        draft = controller.payload()["rules"]
        draft["resolver"]["coverage"]["max_candidates"] = "no-es-un-entero"

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
        )

        payload = controller.payload()
        self.assertEqual(payload["revision"], 0)
        self.assertEqual(payload["rules"]["resolver"]["http"]["timeout_ms"], 1300)
        self.assertEqual(
            payload["rules"]["resolver"]["coverage"]["total_budget_ms"],
            7200,
        )
        self.assertEqual(
            payload["rules"]["resolver"]["retry"]["base_seconds"],
            41,
        )

    def test_constructor_ignores_live_legacy_filebot_setting(self) -> None:
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
        self.database.set_setting(
            "filebot.rules",
            json.dumps({"rules": legacy, "revision": 7}),
        )

        controller = IdentityController(_config(), self.database)
        payload = controller.payload()

        self.assertEqual(payload["revision"], 0)
        self.assertEqual(
            payload["rules"]["resolver"]["locales"]["movies"],
            {"language": "es-ES", "region": "ES"},
        )
        self.assertEqual(payload["rules"]["resolver"]["aliases"]["tv"], [])
        self.assertIsNone(self.database.get_setting("identity.pipeline"))
        self.assertEqual(
            json.loads(self.database.get_setting("filebot.rules"))["revision"], 7
        )

    def test_existing_v2_corruption_is_visible_and_not_reseeded(self) -> None:
        IdentityController(_config(), self.database)
        untouched_common = self.database.get_setting(
            identity_profile_setting_key("common")
        )
        corrupt_raw = json.dumps({"rules": {}})
        self.database.set_setting(identity_profile_setting_key("movies"), corrupt_raw)

        controller = IdentityController(_config(), self.database)

        self.assertTrue(controller.stores["movies"].payload()["repair_required"])
        with self.assertRaises(IdentityRulesValidationError):
            controller.payload("movies")

        self.assertEqual(
            self.database.get_setting(identity_profile_setting_key("movies")),
            corrupt_raw,
        )
        self.assertEqual(
            self.database.get_setting(identity_profile_setting_key("common")),
            untouched_common,
        )

    def test_partial_v2_scopes_stop_startup_instead_of_being_completed(self) -> None:
        self.database.set_setting(identity_profile_setting_key("common"), "{}")

        with self.assertRaisesRegex(RuntimeError, "identity.pipeline.v2 parcial"):
            IdentityController(_config(), self.database)

        self.assertIsNone(
            self.database.get_setting(identity_profile_setting_key("movies"))
        )
        self.assertIsNone(self.database.get_setting(identity_profile_setting_key("tv")))

    def test_fresh_v2_seed_invalidates_preexisting_resolver_cache(self) -> None:
        self.database.set_resolver_cache("legacy-entry", "movie", "{}", 3600)
        self.assertEqual(self.database.resolver_cache_stats()["total"], 1)

        IdentityController(_config(), self.database)

        self.assertEqual(self.database.resolver_cache_stats()["total"], 0)

    def test_category_snapshot_selects_profile_and_old_job_keeps_snapshot(self) -> None:
        controller = IdentityController(_config(), self.database)
        old_snapshot = controller.job_snapshot("movies")
        old_job = {
            "category": "movies",
            "source_meta_json": json.dumps({"identity_rules": old_snapshot}),
        }
        movie_rules = controller.payload("movies")["rules"]
        movie_rules["resolver"]["movies"]["runtime_tolerance_minutes"] = 12
        saved = controller.update(
            {"rules": movie_rules, "expected_revision": 0}, "movies"
        )

        restored = controller.rules_for_job(old_job)
        new_movie = controller.job_snapshot_for_category("movies")
        new_tv = controller.job_snapshot_for_category("tv")

        self.assertEqual(restored["source"], "job_snapshot")
        self.assertEqual(restored["profile"], "movies")
        self.assertEqual(
            restored["rules"]["resolver"]["movies"]["runtime_tolerance_minutes"],
            10,
        )
        self.assertEqual(new_movie["revision"], saved["revision"])
        self.assertEqual(new_movie["profile"], "movies")
        self.assertEqual(new_tv["revision"], 0)
        self.assertEqual(new_tv["profile"], "tv")

    def test_job_snapshot_is_stable_after_later_save(self) -> None:
        controller = IdentityController(_config(), self.database)
        before = controller.job_snapshot()
        changed = controller.payload()["rules"]
        changed["resolver"]["coverage"]["max_candidates"] = 55
        saved = controller.update({"rules": changed, "expected_revision": 0})
        self.assertTrue(saved["ok"])

        old_job = {
            "source_meta_json": json.dumps({"identity_rules": before})
        }
        restored = controller.rules_for_job(old_job)

        self.assertEqual(restored["revision"], 0)
        self.assertEqual(
            restored["rules"]["resolver"]["coverage"]["max_candidates"], 60
        )
        self.assertEqual(
            controller.payload()["rules"]["resolver"]["coverage"][
                "max_candidates"
            ],
            55,
        )

    def test_legacy_v1_job_snapshot_is_historical_and_not_executable(self) -> None:
        controller = IdentityController(_config(), self.database)
        legacy_rules = copy.deepcopy(controller.payload()["rules"])
        legacy_rules["schema_version"] = 1
        legacy_rules["resolver"]["locales"]["movies"]["language"] = "en-GB"
        legacy_rules["resolver"]["aliases"]["movies"] = [
            "La oficina | The Office"
        ]
        legacy_rules["resolver"]["original_language_preference"] = {
            "enabled": True,
            "language": "en",
        }
        legacy_rules["resolver"]["scoring"] = {"title_exact": 50}
        legacy_rules["resolver"]["acceptance"] = {
            "min_score": 75,
            "min_margin": 10,
            "prefer_oldest_exact_title_without_year": True,
        }
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

        with self.assertRaisesRegex(
            IdentityRulesValidationError, "no es ejecutable por phased-er-v2"
        ):
            controller.rules_for_job(old_job)

    def test_legacy_filebot_job_snapshot_is_historical_and_not_executable(self) -> None:
        controller = IdentityController(_config(), self.database)
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

        with self.assertRaisesRegex(
            IdentityRulesValidationError, "historico.*no puede ejecutar"
        ):
            controller.rules_for_job(old_job)


if __name__ == "__main__":
    unittest.main()
