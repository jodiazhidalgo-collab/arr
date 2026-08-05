import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arr_orchestrator.db import Database
from arr_orchestrator.identity.controller import IdentityController
from arr_orchestrator.identity.defaults import factory_identity_rules
from arr_orchestrator.identity.resolver.models import ResolverCandidate
from arr_orchestrator.identity.resolver.evidence import collect_file_episode_intents
from arr_orchestrator.identity.resolver.phased import adjudicate_candidates
from arr_orchestrator.identity.resolver.service import NameResolver
from arr_orchestrator.identity.schema import identity_settings_schema
from arr_orchestrator.identity.validation import (
    IdentityRulesValidationError,
    normalize_identity_rules,
)


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _Session:
    def __init__(self, candidates=None, *, status=200):
        self.candidates = list(candidates or [])
        self.status = status
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.split("/3", 1)[1]
        self.calls.append((path, dict(params or {})))
        if self.status != 200:
            return _Response({}, self.status)
        if path == "/search/movie":
            return _Response({"results": self.candidates})
        if path.startswith("/movie/"):
            tmdb_id = int(path.rsplit("/", 1)[1])
            return _Response(next(item for item in self.candidates if item["id"] == tmdb_id))
        raise AssertionError(path)


def _movie(tmdb_id, title="Objetivo", year=2024, popularity=1.0, votes=1):
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "release_date": f"{year}-01-01",
        "original_language": "es",
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
        "release_dates": {"results": []},
        "popularity": popularity,
        "vote_count": votes,
        "runtime": 100,
    }


def _config():
    return SimpleNamespace(
        resolver_language="es-ES",
        resolver_region="ES",
        resolver_http_timeout_ms=2500,
        resolver_total_budget_ms=20_000,
        resolver_retry_seconds=60,
        tmdb_api_token="token",
    )


def test_v2_defaults_schema_and_active_payload_have_no_scoring_contract():
    rules = factory_identity_rules()
    resolver = rules["resolver"]
    assert rules["schema_version"] == 2
    assert resolver["algorithm"] == "phased-er-v2"
    assert resolver["coverage"] == {
        "max_searches": 12,
        "max_candidates": 60,
        "batch_size": 8,
        "max_details": 40,
        "total_budget_ms": 20_000,
    }
    assert "scoring" not in resolver
    assert "acceptance" not in resolver
    serialized_schema = json.dumps(identity_settings_schema("common"))
    assert "resolver.scoring" not in serialized_schema
    assert "min_margin" not in serialized_schema
    assert "prefer_oldest" not in serialized_schema
    assert "parser" in identity_settings_schema("common")
    assert "parser" not in identity_settings_schema("movies")
    movie_paths = {
        control["path"]
        for group in identity_settings_schema("movies")["resolver"]["groups"]
        for control in group["controls"]
    }
    assert movie_paths and all(path.startswith("resolver.movies.") for path in movie_paths)


def test_v1_contract_has_no_active_migration_path():
    legacy = factory_identity_rules()
    legacy["schema_version"] = 1

    with pytest.raises(IdentityRulesValidationError, match="schema_version debe ser 2"):
        normalize_identity_rules(legacy)


def test_common_and_movie_are_composed_atomically_with_scope_metadata(tmp_path: Path):
    database = Database(tmp_path / "identity.db")
    database.initialize()
    try:
        controller = IdentityController(_config(), database)
        common_payload = controller.payload("common")
        common_rules = copy.deepcopy(common_payload["rules"])
        common_rules["resolver"]["locales"]["movies"]["language"] = "fr-FR"
        common_saved = controller.update(
            {
                "rules": common_rules,
                "expected_revision": common_payload["revision"],
            },
            "common",
        )
        assert common_saved["ok"]

        movie_payload = controller.payload("movies")
        movie_rules = copy.deepcopy(movie_payload["rules"])
        assert set(movie_rules) == {"schema_version", "resolver"}
        assert set(movie_rules["resolver"]) == {"movies"}
        movie_rules["resolver"]["movies"]["runtime_tolerance_minutes"] = 12
        crossed = copy.deepcopy(movie_rules)
        crossed["resolver"]["locales"] = common_rules["resolver"]["locales"]
        rejected = controller.update(
            {
                "rules": crossed,
                "expected_revision": movie_payload["revision"],
            },
            "movies",
        )
        assert rejected["ok"] is False
        assert rejected["error"] == "invalid_rules"
        movie_saved = controller.update(
            {
                "rules": movie_rules,
                "expected_revision": movie_payload["revision"],
            },
            "movies",
        )
        assert movie_saved["ok"]
        effective = controller.job_snapshot_for_category("movies")
        assert effective["rules"]["resolver"]["locales"]["movies"]["language"] == "fr-FR"
        assert effective["rules"]["resolver"]["movies"]["runtime_tolerance_minutes"] == 12
        assert effective["revisions"] == {"common": 1, "movies": 1}
        assert effective["fingerprint"] == effective["combined_fingerprint"]
        assert set(effective["fingerprints"]) == {"common", "movies"}
        assert movie_saved["effective_fingerprint"] == effective["fingerprint"]
    finally:
        database.close()


def test_adjudication_uses_lexicographic_fallback_and_one_verdict_per_family():
    rules = normalize_identity_rules(factory_identity_rules())["resolver"]
    guessed = {"title": "Objetivo", "_title_candidates": ["Objetivo"]}
    older_popular = ResolverCandidate(
        20, "movie", "Objetivo", "Objetivo", 2020, ["Objetivo"], popularity=10, vote_count=50
    )
    newer_less_popular = ResolverCandidate(
        10, "movie", "Objetivo", "Objetivo", 2024, ["Objetivo"], popularity=5, vote_count=500
    )
    outcome = adjudicate_candidates(
        [newer_less_popular, older_popular],
        guessed,
        "movie",
        rules,
        source="search",
    )
    assert outcome.status == "ACCEPTED_FALLBACK"
    assert outcome.selected.tmdb_id == 20
    for candidate in outcome.ordered:
        families = [item["family"] for item in candidate.evidence]
        assert len(families) == len(set(families))
        assert {item["state"] for item in candidate.evidence} <= {
            "AGREE",
            "DISAGREE",
            "UNKNOWN",
        }
    assert outcome.decision["has_scoring"] is False
    assert outcome.decision["phase_counts"]["plausible"] == 2


def test_tv_year_conflict_is_hard_and_absolute_episode_uses_episode_counts():
    rules = normalize_identity_rules(factory_identity_rules())["resolver"]
    guessed = {
        "title": "Serie",
        "year": 2024,
        "_title_candidates": ["Serie"],
        "_episode_intents": [
            {
                "source": "Serie 34",
                "season": None,
                "episodes": [],
                "absolute_episode": 34,
                "is_season_pack": False,
            }
        ],
    }
    wrong = ResolverCandidate(1, "tv", "Serie", "Serie", 2020, ["Serie"])
    correct = ResolverCandidate(
        2,
        "tv",
        "Serie",
        "Serie",
        2024,
        ["Serie"],
        season_episode_counts={1: 20, 2: 20},
    )
    outcome = adjudicate_candidates(
        [wrong, correct], guessed, "tv", rules, source="search"
    )
    assert outcome.status == "ACCEPTED_CONFIDENT"
    assert outcome.selected.tmdb_id == 2
    episode = next(item for item in correct.evidence if item["family"] == "episode")
    absolute = next(
        item
        for item in episode["value"]["subchecks"]
        if item["name"] == "absolute_episode"
    )
    assert absolute["state"] == "AGREE"
    assert "year_conflict" in wrong.elimination_reasons


def test_tv_multi_episode_setting_is_enforced_as_hard_evidence():
    rules = normalize_identity_rules(factory_identity_rules())["resolver"]
    rules["tv"]["allow_multi_episode"] = False
    guessed = {
        "title": "Serie",
        "_title_candidates": ["Serie"],
        "_episode_intents": [
            {
                "source": "Serie.S01E01E02.mkv",
                "season": 1,
                "episodes": [1, 2],
                "absolute_episode": None,
                "is_season_pack": False,
            }
        ],
    }
    candidate = ResolverCandidate(
        2,
        "tv",
        "Serie",
        "Serie",
        2024,
        ["Serie"],
        season_episode_counts={1: 8},
    )

    outcome = adjudicate_candidates(
        [candidate], guessed, "tv", rules, source="search"
    )

    assert outcome.status == "BLOCKED_HARD"
    assert "multi_episode_disabled" in candidate.elimination_reasons
    episode = next(item for item in candidate.evidence if item["family"] == "episode")
    verdict = next(
        item
        for item in episode["value"]["subchecks"]
        if item["name"] == "multi_episode"
    )
    assert verdict["state"] == "DISAGREE"


def test_preview_exposes_v2_decision_and_retry_provider(tmp_path: Path):
    candidates = [_movie(1, popularity=2, votes=10), _movie(2, popularity=1, votes=20)]
    database = Database(tmp_path / "preview.db")
    database.initialize()
    try:
        session = _Session(candidates)
        resolver = NameResolver(
            "token", "es-ES", "ES", 2500, 20_000, database, session=session
        )
        accepted = resolver.preview("Objetivo", "movies")
        assert accepted["ok"] is True
        assert accepted["status"] == "ACCEPTED_FALLBACK"
        decision = accepted["decision"]
        assert decision["accepted"] is True
        assert decision["selected"]["tmdb_id"] == 1
        assert set(decision["phase_counts"]) == {
            "discovered",
            "enriched",
            "eliminated",
            "plausible",
        }
        assert "score" not in accepted["identity"]
        assert "margin" not in accepted["identity"]

        retry = NameResolver(
            "token",
            "es-ES",
            "ES",
            2500,
            20_000,
            database,
            session=_Session(status=503),
        ).preview("Objetivo", "movies")
        assert retry["ok"] is True
        assert retry["status"] == "RETRY_PROVIDER"
        assert retry["decision"]["accepted"] is False
    finally:
        database.close()


def test_episode_intents_are_bound_one_per_physical_file_with_full_basename(tmp_path: Path):
    input_root = tmp_path / "Carpeta que no coincide con la serie"
    input_root.mkdir()
    long_name = f"Serie.{'x' * 170}.S01E01.mkv"
    second_name = "Copia.distinta.S01E01.mkv"
    (input_root / long_name).write_bytes(b"a")
    (input_root / second_name).write_bytes(b"b")
    rules = normalize_identity_rules(factory_identity_rules())["resolver"]
    rules["parser"] = factory_identity_rules()["parser"]

    intents = collect_file_episode_intents(input_root, rules)

    assert len(intents) == 2
    assert {item["source"] for item in intents} == {long_name, second_name}
    assert any(len(item["source"]) > 160 for item in intents)
    assert all(item["season"] == 1 and item["episodes"] == [1] for item in intents)


def test_episode_intents_validate_every_physical_file_beyond_title_evidence_cap(
    tmp_path: Path,
):
    input_root = tmp_path / "Serie"
    input_root.mkdir()
    for episode in range(1, 26):
        (input_root / f"Serie.S01E{episode:02d}.mkv").write_bytes(b"media")
    rules = normalize_identity_rules(factory_identity_rules())["resolver"]
    rules["parser"] = factory_identity_rules()["parser"]
    rules["evidence"]["max_media_files"] = 3

    intents = collect_file_episode_intents(input_root, rules)

    assert len(intents) == 25
    assert {item["episodes"][0] for item in intents} == set(range(1, 26))
