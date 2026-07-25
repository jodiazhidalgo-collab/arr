import copy
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from arr_orchestrator.db import Database
from arr_orchestrator.identity import (
    IDENTITY_SETTING_KEY,
    IdentityRulesValidationError,
    IdentitySettingsStore,
    factory_identity_rules,
    identity_fingerprint,
    normalize_identity_rules,
)
from arr_orchestrator.identity.parser_rules import DEFAULT_PARSER_RULES


RUNTIME_TEST_ROOT = Path(__file__).resolve().parents[3] / "_codex_runtime" / "test-data"


def _leaf_paths(value, prefix=""):
    if isinstance(value, dict):
        result = set()
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            result.update(_leaf_paths(item, path))
        return result
    return {prefix}


class FakeSettingsDatabase:
    def __init__(self, *, fail=False):
        self.values = {}
        self.fail = fail
        self.lock = threading.Lock()
        self.cache_entries = 0

    def get_setting(self, key):
        return self.values.get(key)

    def set_setting(self, key, value):
        if self.fail:
            raise OSError("fallo simulado")
        self.values[key] = value

    def compare_and_set_setting(self, key, expected_revision, value):
        if self.fail:
            raise OSError("fallo simulado")
        with self.lock:
            current = self.values.get(key)
            revision = 0
            if current:
                parsed = json.loads(current)
                revision = int(parsed.get("revision", 0))
            if revision != expected_revision:
                return False
            self.values[key] = value
            return True

    def resolver_cache_stats(self):
        return {
            "total": self.cache_entries,
            "active": self.cache_entries,
            "expired": 0,
            "by_media_type": {},
        }

    def clear_resolver_cache(self):
        deleted = self.cache_entries
        self.cache_entries = 0
        return deleted


def changed_rules(language="EN-us", score=76):
    rules = factory_identity_rules()
    rules["resolver"]["locales"]["movies"].update(  # type: ignore[index]
        {"language": language, "region": "us"}
    )
    rules["resolver"]["aliases"]["movies"] = [  # type: ignore[index]
        "The Visitors|Los visitantes",
        "the visitors | los visitantes",
    ]
    rules["resolver"]["forced_matches"]["movies"] = [  # type: ignore[index]
        "The Visitors | 1993 | 11687"
    ]
    rules["resolver"]["acceptance"]["min_score"] = score  # type: ignore[index]
    return rules


def leaf_paths(value, prefix=""):
    result = set()
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            result.update(leaf_paths(child, path))
    elif isinstance(value, list):
        result.add(prefix)
    else:
        result.add(prefix)
    return result


def _padded_unique(prefix, index, length):
    lead = f"{prefix}{index:03d}"
    return (lead + ("x" * length))[:length]


def _large_valid_rules():
    rules = factory_identity_rules()
    parser = rules["parser"]
    for key in (
        "site_words",
        "technical_tokens",
        "tail_noise_tokens",
        "language_tokens",
        "manual_keywords",
        "manual_exact_names",
        "collection_keywords",
        "season_pack_markers",
    ):
        parser[key] = [_padded_unique(key, index, 512) for index in range(256)]
    parser["ocr_replacements"] = [
        {
            "pattern": (
                f"a{{{index + 1}}}"
                + ("b" * (2000 - len(f"a{{{index + 1}}}")))
            ),
            "replacement": _padded_unique("replacement", index, 512),
        }
        for index in range(256)
    ]

    resolver = rules["resolver"]
    for category in ("movies", "tv"):
        resolver["aliases"][category] = []
        resolver["forced_matches"][category] = []
        for index in range(256):
            source = _padded_unique(f"source-{category}", index, 254)
            destination = _padded_unique(f"target-{category}", index, 255)
            resolver["aliases"][category].append(f"{source} | {destination}")
            suffix = f" | 2024 | {index + 1}"
            title = _padded_unique(f"forced-{category}", index, 512 - len(suffix))
            resolver["forced_matches"][category].append(f"{title}{suffix}")
    return rules


class IdentityRulesTests(unittest.TestCase):
    def test_visual_schema_exposes_every_editable_leaf(self):
        rules = factory_identity_rules()
        schema = IdentitySettingsStore(FakeSettingsDatabase()).payload()["schema"]
        control_paths = {
            control["path"]
            for section in ("parser", "resolver")
            for group in schema[section]["groups"]
            for control in group["controls"]
        }

        self.assertEqual(control_paths, _leaf_paths(rules) - {"schema_version"})

    def test_factory_uses_parser_source_of_truth_and_runtime_locales(self):
        rules = factory_identity_rules("en-gb", "gb")

        self.assertEqual(rules["parser"], DEFAULT_PARSER_RULES)
        self.assertIsNot(rules["parser"], DEFAULT_PARSER_RULES)
        self.assertEqual(rules["resolver"]["locales"]["movies"]["language"], "en-GB")
        self.assertEqual(rules["resolver"]["locales"]["movies"]["region"], "GB")
        self.assertEqual(rules["resolver"]["locales"]["tv"]["language"], "en-GB")

    def test_normalization_is_strict_and_canonical(self):
        normalized = normalize_identity_rules(changed_rules())

        self.assertEqual(
            normalized["resolver"]["locales"]["movies"],
            {"language": "en-US", "region": "US"},
        )
        self.assertEqual(
            normalized["resolver"]["aliases"]["movies"],
            ["The Visitors | Los visitantes"],
        )
        self.assertEqual(
            normalized["resolver"]["forced_matches"]["movies"],
            ["The Visitors | 1993 | 11687"],
        )

        invalid = changed_rules()
        invalid["resolver"]["cache"]["exec"] = "rm"  # type: ignore[index]
        with self.assertRaises(IdentityRulesValidationError):
            normalize_identity_rules(invalid)

        invalid = changed_rules()
        invalid["parser"]["patterns"]["series_sxe"] = "("  # type: ignore[index]
        with self.assertRaises(IdentityRulesValidationError):
            normalize_identity_rules(invalid)

        invalid = changed_rules()
        invalid["parser"]["patterns"]["series_sxe"] = "foo"  # type: ignore[index]
        with self.assertRaises(IdentityRulesValidationError):
            normalize_identity_rules(invalid)

        invalid = changed_rules()
        invalid["parser"]["ocr_replacements"][0]["replacement"] = r"\9"  # type: ignore[index]
        with self.assertRaises(IdentityRulesValidationError):
            normalize_identity_rules(invalid)

        invalid = changed_rules()
        del invalid["resolver"]["scoring"]["title_exact"]  # type: ignore[index]
        with self.assertRaises(IdentityRulesValidationError):
            normalize_identity_rules(invalid)

    def test_fingerprint_is_stable_after_normalization(self):
        first = changed_rules("EN-us")
        second = changed_rules("en-US")

        self.assertEqual(identity_fingerprint(first), identity_fingerprint(second))
        self.assertRegex(identity_fingerprint(first), r"^sha256:[0-9a-f]{64}$")

    def test_large_valid_export_round_trips_under_the_web_limit(self):
        normalized = normalize_identity_rules(_large_valid_rules())
        exported = json.dumps(
            {
                "exported_at": "2026-07-25T00:00:00.000Z",
                "revision": 0,
                "fingerprint": identity_fingerprint(normalized),
                "rules": normalized,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

        self.assertGreater(len(exported), 2 * 1024 * 1024)
        self.assertLessEqual(len(exported), 4 * 1024 * 1024)
        imported = json.loads(exported.decode("utf-8"))
        self.assertEqual(normalize_identity_rules(imported["rules"]), normalized)


class IdentityStoreTests(unittest.TestCase):
    def test_payload_has_complete_visual_schema_and_defensive_copies(self):
        database = FakeSettingsDatabase()
        store = IdentitySettingsStore(database)

        payload = store.payload()
        controls = {
            control["path"]
            for section in payload["schema"].values()
            for group in section["groups"]
            for control in group["controls"]
        }
        expected = leaf_paths(payload["rules"]) - {"schema_version"}
        self.assertEqual(expected - controls, set())
        self.assertEqual(payload["revision"], 0)
        self.assertIsNone(payload["saved_at"])
        self.assertEqual(payload["rules_path"], "settings/identity.pipeline")
        self.assertTrue(payload["cache_status"]["available"])
        self.assertFalse(payload["repair_required"])

        payload["rules"]["resolver"]["cache"]["enabled"] = False
        payload["defaults"]["parser"]["extensions"].clear()
        current = store.payload()
        self.assertTrue(current["rules"]["resolver"]["cache"]["enabled"])
        self.assertTrue(current["defaults"]["parser"]["extensions"])

    def test_save_reload_reset_and_limited_history(self):
        database = FakeSettingsDatabase()
        store = IdentitySettingsStore(
            database,
            default_language="en-gb",
            default_region="gb",
            history_limit=2,
        )

        first = store.update(
            {"rules": changed_rules(score=76), "expected_revision": 0}
        )
        second_rules = copy.deepcopy(first["rules"])
        second_rules["resolver"]["acceptance"]["min_score"] = 77
        second = store.update({"rules": second_rules, "expected_revision": 1})
        third_rules = copy.deepcopy(second["rules"])
        third_rules["resolver"]["acceptance"]["min_score"] = 78
        third = store.update({"rules": third_rules, "expected_revision": 2})

        self.assertTrue(third["ok"])
        self.assertEqual(third["revision"], 3)
        self.assertEqual([item["revision"] for item in third["history"]], [2, 3])
        self.assertEqual(len(third["history"]), 2)
        self.assertEqual(
            json.loads(database.values[IDENTITY_SETTING_KEY])["revision"], 3
        )

        restarted = IdentitySettingsStore(
            database,
            default_language="en-gb",
            default_region="gb",
            history_limit=2,
        )
        self.assertEqual(restarted.snapshot(), third["rules"])
        reset = restarted.reset({"expected_revision": 3})
        self.assertTrue(reset["ok"])
        self.assertTrue(reset["saved"])
        self.assertEqual(reset["action"], "reset")
        self.assertEqual(reset["revision"], 4)
        self.assertEqual(
            reset["rules"]["resolver"]["locales"]["movies"],
            {"language": "en-GB", "region": "GB"},
        )
        self.assertEqual([item["revision"] for item in reset["history"]], [3, 4])

    def test_invalid_conflict_persistence_failure_and_noop_contracts(self):
        database = FakeSettingsDatabase()
        store = IdentitySettingsStore(database)

        invalid = store.update({"rules": factory_identity_rules()})
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error"], "invalid_rules")

        saved = store.update({"rules": changed_rules(), "expected_revision": 0})
        conflict = store.reset({"expected_revision": 0})
        self.assertTrue(saved["ok"])
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"], "revision_conflict")
        self.assertEqual(conflict["current_revision"], 1)

        noop = store.update(
            {"rules": copy.deepcopy(saved["rules"]), "expected_revision": 1}
        )
        self.assertTrue(noop["ok"])
        self.assertFalse(noop["saved"])
        self.assertEqual(noop["revision"], 1)

        failed = IdentitySettingsStore(FakeSettingsDatabase(fail=True)).update(
            {"rules": changed_rules(), "expected_revision": 0}
        )
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"], "persistence_failed")

    def test_two_stores_cannot_overwrite_the_same_revision(self):
        database = FakeSettingsDatabase()
        first = IdentitySettingsStore(database)
        second = IdentitySettingsStore(database)

        winner = first.update({"rules": changed_rules(score=76), "expected_revision": 0})
        loser = second.update({"rules": changed_rules(score=77), "expected_revision": 0})

        self.assertTrue(winner["ok"])
        self.assertFalse(loser["ok"])
        self.assertEqual(loser["error"], "revision_conflict")
        self.assertEqual(loser["rules"], winner["rules"])


class IdentityDatabaseTests(unittest.TestCase):
    def setUp(self):
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = TemporaryDirectory(dir=RUNTIME_TEST_ROOT)
        self.database = Database(Path(self.temporary.name) / "identity.db")
        self.database.initialize()

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def test_database_cas_cache_stats_and_clear(self):
        first = json.dumps({"revision": 1})
        second = json.dumps({"revision": 2})
        self.assertTrue(self.database.compare_and_set_setting("test", 0, first))
        self.assertFalse(self.database.compare_and_set_setting("test", 0, second))
        self.assertTrue(self.database.compare_and_set_setting("test", 1, second))

        self.database.set_resolver_cache("movie", "movie", "{}", 3600)
        self.database.set_resolver_cache("expired", "tv", "{}", -1)
        stats = self.database.resolver_cache_stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["expired"], 1)
        self.assertEqual(stats["by_media_type"], {"movie": 1})
        self.assertEqual(self.database.clear_resolver_cache(), 2)
        self.assertEqual(self.database.resolver_cache_stats()["total"], 0)

    def test_invalid_old_schema_can_be_replaced_without_revision_deadlock(self):
        for schema_version in (1, 99):
            with self.subTest(schema_version=schema_version):
                self.database.set_setting(
                    IDENTITY_SETTING_KEY,
                    json.dumps(
                        {
                            "schema_version": schema_version,
                            "parser": {},
                            "resolver": {},
                        }
                    ),
                )
                store = IdentitySettingsStore(self.database)
                self.assertEqual(store.payload()["revision"], 0)

                self.assertTrue(store.payload()["repair_required"])
                result = store.reset({"expected_revision": 0})

                self.assertTrue(result["ok"])
                self.assertTrue(result["saved"])
                self.assertEqual(result["revision"], 1)
                self.assertFalse(result["repair_required"])
                persisted = json.loads(
                    self.database.get_setting(IDENTITY_SETTING_KEY) or "{}"
                )
                self.assertEqual(persisted["revision"], 1)
                self.assertEqual(persisted["rules"], result["defaults"])


if __name__ == "__main__":
    unittest.main()
