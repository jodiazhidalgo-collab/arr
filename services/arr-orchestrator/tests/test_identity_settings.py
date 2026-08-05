import copy
import json
import threading

import pytest

from arr_orchestrator.identity import (
    IdentityRulesValidationError,
    IdentitySettingsStore,
    factory_identity_rules,
    identity_fingerprint,
    normalize_identity_rules,
)
from arr_orchestrator.identity.parser_rules import DEFAULT_PARSER_RULES
from arr_orchestrator.identity.defaults import identity_profile_setting_key
from arr_orchestrator.identity.resolver.policy import effective_policy
from arr_orchestrator.identity.schema import identity_settings_schema


class FakeSettingsDatabase:
    def __init__(self, *, fail=False):
        self.values = {}
        self.fail = fail
        self.lock = threading.Lock()
        self.cache_entries = 0
        self.cache_stats_profiles = []
        self.cache_clear_profiles = []

    def get_setting(self, key):
        return self.values.get(key)

    def set_setting(self, key, value):
        if self.fail:
            raise OSError("fallo simulado")
        self.values[key] = value

    def compare_and_set_setting_value(self, key, expected_value, value):
        if self.fail:
            raise OSError("fallo simulado")
        with self.lock:
            if self.values.get(key) != expected_value:
                return False
            self.values[key] = value
            return True

    def resolver_cache_stats(self, profile=None):
        self.cache_stats_profiles.append(profile)
        return {
            "total": self.cache_entries,
            "active": self.cache_entries,
            "expired": 0,
            "by_media_type": {},
        }

    def clear_resolver_cache(self, profile=None):
        self.cache_clear_profiles.append(profile)
        deleted = self.cache_entries
        self.cache_entries = 0
        return deleted


def _control_paths(schema, section="resolver"):
    return {
        control["path"]
        for group in schema[section]["groups"]
        for control in group["controls"]
    }


def _changed_rules():
    rules = factory_identity_rules()
    resolver = rules["resolver"]
    resolver["locales"]["movies"] = {"language": "EN-us", "region": "us"}
    resolver["aliases"]["movies"] = [
        "The Visitors | Los visitantes",
        "the visitors | los visitantes",
        "Mismo titulo | mismo titulo",
    ]
    resolver["forced_matches"]["movies"] = ["The Visitors | 1993 | 11687"]
    resolver["coverage"]["max_candidates"] = 55
    resolver["title_matching"]["roman_arabic_equivalence"] = False
    resolver["movies"]["runtime_tolerance_minutes"] = 12
    return rules


def test_schema_is_profile_filtered_and_contains_no_v1_controls():
    common = identity_settings_schema("common")
    movies = identity_settings_schema("movies")
    tv = identity_settings_schema("tv")

    common_paths = _control_paths(common)
    movie_paths = _control_paths(movies)
    tv_paths = _control_paths(tv)
    serialized = json.dumps(common, ensure_ascii=False)

    assert common["schema_version"] == 2
    assert "parser" in common
    assert "parser" not in movies and "parser" not in tv
    assert "resolver.coverage.max_searches" in common_paths
    assert "resolver.adjudication.tie_breakers" in common_paths
    assert not any(path.startswith("resolver.movies.") for path in common_paths)
    assert not any(path.startswith("resolver.tv.") for path in common_paths)
    assert movie_paths and all(path.startswith("resolver.movies.") for path in movie_paths)
    assert tv_paths and all(path.startswith("resolver.tv.") for path in tv_paths)
    assert "scoring" not in serialized
    assert "min_margin" not in serialized
    assert "prefer_oldest" not in serialized


def test_factory_uses_parser_source_and_exact_v2_operational_defaults():
    rules = factory_identity_rules("en-gb", "gb", 1400, 6600, 45)
    resolver = rules["resolver"]

    assert rules["schema_version"] == 2
    assert rules["parser"] == DEFAULT_PARSER_RULES
    assert rules["parser"] is not DEFAULT_PARSER_RULES
    assert resolver["algorithm"] == "phased-er-v2"
    assert resolver["locales"]["movies"] == {"language": "en-GB", "region": "GB"}
    assert resolver["locales"]["tv"] == {"language": "en-GB"}
    assert resolver["http"] == {"timeout_ms": 1400}
    assert resolver["coverage"]["total_budget_ms"] == 6600
    assert resolver["retry"]["base_seconds"] == 45
    assert resolver["retry"]["max_attempts"] == 3
    assert resolver["coverage"] == {
        "max_searches": 12,
        "max_candidates": 60,
        "batch_size": 8,
        "max_details": 40,
        "total_budget_ms": 6600,
    }
    assert resolver["adjudication"] == {
        "mode": "most_probable",
        "tie_breakers": [
            "explicit_year",
            "agreements",
            "disagreements",
            "popularity",
            "vote_count",
            "newest_year",
            "lowest_tmdb_id",
        ],
    }
    assert "scoring" not in resolver and "acceptance" not in resolver


def test_v1_rules_are_rejected_after_final_cutover():
    legacy = _changed_rules()
    legacy["schema_version"] = 1

    with pytest.raises(IdentityRulesValidationError, match="schema_version debe ser 2"):
        normalize_identity_rules(legacy)


def test_normalization_is_canonical_and_fingerprint_covers_v2_fields():
    normalized = normalize_identity_rules(_changed_rules())
    resolver = normalized["resolver"]

    assert resolver["locales"]["movies"] == {"language": "en-US", "region": "US"}
    assert resolver["aliases"]["movies"] == ["The Visitors | Los visitantes"]
    assert resolver["forced_matches"]["movies"] == ["The Visitors | 1993 | 11687"]
    assert identity_fingerprint(normalized) == identity_fingerprint(
        normalize_identity_rules(copy.deepcopy(normalized))
    )

    changed = copy.deepcopy(normalized)
    changed["resolver"]["coverage"]["max_candidates"] = 54
    assert identity_fingerprint(changed) != identity_fingerprint(normalized)

    policy = effective_policy(normalized, "movies")
    assert policy["coverage"]["max_candidates"] == 55
    assert policy["movies"]["runtime_tolerance_minutes"] == 12
    assert "scoring" not in policy and "acceptance" not in policy


def test_validation_rejects_v1_controls_and_invalid_v2_invariants():
    cases = []

    extra = factory_identity_rules()
    extra["resolver"]["scoring"] = {"title_exact": 1}
    cases.append(extra)

    coverage = factory_identity_rules()
    coverage["resolver"]["coverage"]["max_searches"] = 13
    cases.append(coverage)

    batch = factory_identity_rules()
    batch["resolver"]["coverage"].update({"batch_size": 8, "max_details": 7})
    cases.append(batch)

    adjudication = factory_identity_rules()
    adjudication["resolver"]["adjudication"]["tie_breakers"].reverse()
    cases.append(adjudication)

    evidence = factory_identity_rules()
    evidence["resolver"]["evidence"].update(
        {
            "use_job_name": False,
            "use_folder_name": False,
            "use_media_files": True,
            "max_media_files": 0,
        }
    )
    cases.append(evidence)

    movie_runtime = factory_identity_rules()
    movie_runtime["resolver"]["movies"].update(
        {"short_runtime_minutes": 60, "feature_runtime_minutes": 60}
    )
    cases.append(movie_runtime)

    for rules in cases:
        try:
            normalize_identity_rules(rules)
        except IdentityRulesValidationError:
            pass
        else:
            raise AssertionError("una configuracion v2 invalida fue aceptada")


def test_store_payload_save_reset_history_and_defensive_copies():
    database = FakeSettingsDatabase()
    database.cache_entries = 3
    store = IdentitySettingsStore(database, history_limit=2, profile="movies")

    initial = store.payload()
    assert initial["schema_version"] == 2
    assert initial["export_format"] == "arr-identity-export-v2"
    assert initial["revision"] == 0
    assert initial["cache_status"]["total"] == 3
    assert database.cache_stats_profiles[-1] == "movies"

    initial["rules"]["resolver"]["movies"]["runtime_tolerance_minutes"] = 1
    assert store.snapshot()["resolver"]["movies"]["runtime_tolerance_minutes"] == 10
    assert set(store.snapshot()["resolver"]) == {"movies"}

    rules = store.snapshot()
    rules["resolver"]["movies"]["runtime_tolerance_minutes"] = 12
    saved = store.update({"rules": rules, "expected_revision": 0})
    assert saved["ok"] and saved["saved"] and saved["revision"] == 1
    assert len(saved["history"]) == 1

    no_op = store.update({"rules": copy.deepcopy(saved["rules"]), "expected_revision": 1})
    assert no_op["ok"] and no_op["saved"] is False and no_op["revision"] == 1

    reset = store.reset({"expected_revision": 1})
    assert reset["ok"] and reset["saved"] and reset["revision"] == 2
    assert reset["rules"]["resolver"]["movies"]["runtime_tolerance_minutes"] == 10
    assert [item["action"] for item in reset["history"]] == ["save", "reset"]

    cleared = store.clear_cache()
    assert cleared == {"ok": True, "deleted": 3, "cache_status": {
        "total": 0,
        "active": 0,
        "expired": 0,
        "by_media_type": {},
        "available": True,
    }}
    assert database.cache_clear_profiles == ["movies"]


def test_store_cas_prevents_stale_overwrite_and_reports_persistence_failure():
    database = FakeSettingsDatabase()
    first = IdentitySettingsStore(database)
    second = IdentitySettingsStore(database)

    first_rules = first.snapshot()
    first_rules["resolver"]["coverage"]["max_candidates"] = 50
    assert first.update({"rules": first_rules, "expected_revision": 0})["ok"]

    stale_rules = second.snapshot()
    stale_rules["resolver"]["coverage"]["max_candidates"] = 40
    conflict = second.update({"rules": stale_rules, "expected_revision": 0})
    assert conflict["ok"] is False
    assert conflict["error"] == "revision_conflict"
    assert conflict["current_revision"] == 1

    failing = IdentitySettingsStore(FakeSettingsDatabase(fail=True))
    failed_rules = failing.snapshot()
    failed_rules["resolver"]["coverage"]["max_candidates"] = 50
    failed = failing.update({"rules": failed_rules, "expected_revision": 0})
    assert failed["ok"] is False
    assert failed["error"] == "persistence_failed"


def test_default_store_never_reads_or_rewrites_the_retired_v1_key():
    database = FakeSettingsDatabase()
    retired_key = "identity.pipeline"
    legacy = factory_identity_rules()
    legacy["schema_version"] = 1
    legacy["resolver"]["scoring"] = {"title_exact": 500}
    legacy["resolver"]["acceptance"] = {"min_score": 500}
    original_raw = json.dumps(legacy)
    database.values[retired_key] = original_raw

    store = IdentitySettingsStore(database)
    payload = store.payload()

    assert payload["rules"]["schema_version"] == 2
    assert payload["revision"] == 0
    assert payload["repair_required"] is False
    assert database.values[retired_key] == original_raw

    payload["rules"]["resolver"]["coverage"]["max_candidates"] = 59
    saved = store.update({"rules": payload["rules"], "expected_revision": 0})
    assert saved["ok"] and saved["saved"]
    persisted = json.loads(
        database.values[identity_profile_setting_key("common")]
    )
    assert persisted["rules"]["schema_version"] == 2
    assert database.values[retired_key] == original_raw


def test_corrupt_persisted_scope_is_visible_for_repair_but_never_executable():
    database = FakeSettingsDatabase()
    setting_key = "identity.pipeline.v2.movies"
    corrupt_raw = json.dumps({"rules": {}})
    database.values[setting_key] = corrupt_raw
    store = IdentitySettingsStore(
        database,
        profile="movies",
        setting_key=setting_key,
    )

    payload = store.payload()
    assert payload["repair_required"] is True
    assert database.values[setting_key] == corrupt_raw

    for operation in (
        store.snapshot,
        store.job_snapshot,
        lambda: store.job_snapshot_from_raw(None),
        lambda: store.job_snapshot_from_raw(corrupt_raw),
    ):
        try:
            operation()
        except IdentityRulesValidationError:
            pass
        else:
            raise AssertionError("un scope v2 corrupto se uso como snapshot ejecutable")
