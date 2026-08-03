import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from arr_orchestrator.db import Database
from arr_orchestrator.identity.controller import IdentityController
from arr_orchestrator.identity.defaults import (
    IDENTITY_PROFILES,
    IDENTITY_SETTING_KEY,
    factory_identity_rules,
    identity_profile_setting_key,
)
from arr_orchestrator.identity.fingerprint import identity_fingerprint


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

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "TMDB_UNAVAILABLE")
        self.assertEqual(result["decision"]["status"], "TMDB_UNAVAILABLE")
        self.assertFalse(result["decision"]["has_scoring"])
        self.assertEqual(self.database.resolver_cache_stats()["total"], 0)

    def test_resolver_tester_returns_human_contract_for_invalid_draft(self) -> None:
        controller = IdentityController(_config(), self.database)
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

    def test_legacy_pipeline_is_cloned_once_and_profiles_are_independent(self) -> None:
        legacy_rules = factory_identity_rules()
        legacy_raw = json.dumps(
            {
                "rules": legacy_rules,
                "revision": 4,
                "saved_at": "2026-07-30T12:00:00Z",
                "history": [
                    {
                        "revision": 4,
                        "saved_at": "2026-07-30T12:00:00Z",
                        "fingerprint": identity_fingerprint(legacy_rules),
                        "action": "save",
                    }
                ],
            },
            ensure_ascii=False,
        )
        self.database.set_setting(IDENTITY_SETTING_KEY, legacy_raw)

        controller = IdentityController(_config(), self.database)

        self.assertEqual(self.database.get_setting(IDENTITY_SETTING_KEY), legacy_raw)
        for profile in IDENTITY_PROFILES:
            self.assertEqual(
                self.database.get_setting(identity_profile_setting_key(profile)),
                legacy_raw,
            )
            payload = controller.payload(profile)
            self.assertEqual(payload["profile"], profile)
            self.assertEqual(payload["revision"], 4)

        movie_rules = controller.payload("movies")["rules"]
        movie_rules["resolver"]["acceptance"]["min_score"] = 61
        saved = controller.update(
            {"rules": movie_rules, "expected_revision": 4}, "movies"
        )

        self.assertTrue(saved["ok"])
        self.assertEqual(saved["revision"], 5)
        self.assertEqual(saved["profile"], "movies")
        self.assertEqual(controller.payload("common")["revision"], 4)
        self.assertEqual(controller.payload("tv")["revision"], 4)
        self.assertEqual(
            controller.payload("tv")["rules"]["resolver"]["acceptance"][
                "min_score"
            ],
            75,
        )
        self.assertNotEqual(
            controller.payload("movies")["fingerprint"],
            controller.payload("tv")["fingerprint"],
        )
        tv_rules = controller.payload("tv")["rules"]
        tv_rules["resolver"]["acceptance"]["min_margin"] = 13
        tv_saved = controller.update(
            {"rules": tv_rules, "expected_revision": 4}, "tv"
        )
        self.assertTrue(tv_saved["ok"])
        self.assertEqual(tv_saved["revision"], 5)
        self.assertEqual(controller.payload("common")["revision"], 4)
        self.assertEqual(
            [entry["revision"] for entry in controller.payload("movies")["history"]],
            [4, 5],
        )
        self.assertEqual(
            [entry["revision"] for entry in controller.payload("tv")["history"]],
            [4, 5],
        )
        self.assertEqual(self.database.get_setting(IDENTITY_SETTING_KEY), legacy_raw)

    def test_category_snapshot_selects_profile_and_old_job_keeps_snapshot(self) -> None:
        controller = IdentityController(_config(), self.database)
        old_snapshot = controller.job_snapshot("movies")
        old_job = {
            "category": "movies",
            "source_meta_json": json.dumps({"identity_rules": old_snapshot}),
        }
        movie_rules = controller.payload("movies")["rules"]
        movie_rules["resolver"]["acceptance"]["min_score"] = 62
        saved = controller.update(
            {"rules": movie_rules, "expected_revision": 0}, "movies"
        )

        restored = controller.rules_for_job(old_job)
        new_movie = controller.job_snapshot_for_category("movies")
        new_tv = controller.job_snapshot_for_category("tv")

        self.assertEqual(restored["source"], "job_snapshot")
        self.assertEqual(restored["profile"], "movies")
        self.assertEqual(
            restored["rules"]["resolver"]["acceptance"]["min_score"], 75
        )
        self.assertEqual(new_movie["revision"], saved["revision"])
        self.assertEqual(new_movie["profile"], "movies")
        self.assertEqual(new_tv["revision"], 0)
        self.assertEqual(new_tv["profile"], "tv")

    def test_job_snapshot_is_stable_after_later_save(self) -> None:
        controller = IdentityController(_config(), self.database)
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
        controller = IdentityController(_config(), self.database)
        legacy_rules = copy.deepcopy(controller.payload()["rules"])
        del legacy_rules["resolver"]["original_language_preference"]
        for key in (
            "video_extensions",
            "video_markers",
            "non_video_markers",
            "season_number_words",
        ):
            del legacy_rules["parser"][key]
        del legacy_rules["parser"]["normalization"][
            "movie_without_year_from_video"
        ]
        del legacy_rules["parser"]["normalization"]["allow_tv_year_range"]
        for key in ("series_sxe", "explicit_season", "season_pack"):
            del legacy_rules["parser"]["patterns"][key]
        del legacy_rules["resolver"]["acceptance"][
            "prefer_oldest_exact_title_without_year"
        ]
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
        self.assertEqual(restored["rules"]["parser"]["video_extensions"], [])
        self.assertEqual(restored["rules"]["parser"]["video_markers"], [])
        self.assertEqual(restored["rules"]["parser"]["non_video_markers"], [])
        self.assertEqual(restored["rules"]["parser"]["season_number_words"], [])
        self.assertFalse(
            restored["rules"]["parser"]["normalization"][
                "movie_without_year_from_video"
            ]
        )
        self.assertFalse(
            restored["rules"]["parser"]["normalization"]["allow_tv_year_range"]
        )
        self.assertFalse(
            restored["rules"]["resolver"]["acceptance"][
                "prefer_oldest_exact_title_without_year"
            ]
        )
        self.assert_legacy_tv_regex_semantics(restored["rules"])
        self.assertNotEqual(restored["fingerprint"], stale_fingerprint)
        self.assertEqual(
            restored["fingerprint"], identity_fingerprint(restored["rules"])
        )

    def test_legacy_filebot_job_snapshot_remains_read_only_compatible(self) -> None:
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

        restored = controller.rules_for_job(old_job)

        self.assertEqual(restored["source"], "legacy_filebot_snapshot")
        self.assertEqual(restored["revision"], 2)
        self.assertEqual(
            restored["rules"]["resolver"]["locales"]["movies"],
            {"language": "es-ES", "region": "ES"},
        )
        self.assertFalse(
            restored["rules"]["resolver"]["original_language_preference"]["enabled"]
        )
        self.assertEqual(restored["rules"]["parser"]["video_extensions"], [])
        self.assertEqual(restored["rules"]["parser"]["video_markers"], [])
        self.assertEqual(restored["rules"]["parser"]["non_video_markers"], [])
        self.assertEqual(restored["rules"]["parser"]["season_number_words"], [])
        self.assertFalse(
            restored["rules"]["resolver"]["acceptance"][
                "prefer_oldest_exact_title_without_year"
            ]
        )
        self.assert_legacy_tv_regex_semantics(restored["rules"])
        self.assertEqual(
            restored["fingerprint"], identity_fingerprint(restored["rules"])
        )


if __name__ == "__main__":
    unittest.main()
