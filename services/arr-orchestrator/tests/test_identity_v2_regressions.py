import copy
import itertools
import json
from pathlib import Path

import pytest

from arr_orchestrator.identity.defaults import factory_identity_rules
from arr_orchestrator.identity.resolver.cache import (
    RESOLVER_ALGORITHM_VERSION,
    RESOLVER_CACHE_VERSION,
    cache_key,
)
from arr_orchestrator.identity.resolver.evidence import collect_file_episode_intents
from arr_orchestrator.identity.resolver.models import ResolverCandidate
from arr_orchestrator.identity.resolver.phased import adjudicate_candidates
from arr_orchestrator.identity.resolver.policy import effective_policy


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "identity_v2" / "regression_cases.json"
)


@pytest.fixture
def cases():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _policy(category="movies"):
    return effective_policy(factory_identity_rules(), category)


def _candidate(payload, media_type):
    title = str(payload["title"])
    return ResolverCandidate(
        tmdb_id=int(payload["tmdb_id"]),
        media_type=media_type,
        title=title,
        original_title=str(payload.get("original_title") or title),
        year=payload.get("year"),
        aliases=[str(value) for value in payload.get("aliases") or [title]],
        popularity=float(payload.get("popularity") or 0),
        vote_count=int(payload.get("vote_count") or 0),
        runtime_minutes=payload.get("runtime_minutes"),
        episode_runtime_minutes=[
            int(value) for value in payload.get("episode_runtime_minutes") or []
        ],
        season_count=payload.get("season_count"),
        season_episode_counts={
            int(season): int(count)
            for season, count in (payload.get("season_episode_counts") or {}).items()
        },
        known_episodes={
            int(season): [int(value) for value in episodes]
            for season, episodes in (payload.get("known_episodes") or {}).items()
        },
    )


def _candidates(case, media_type):
    return [_candidate(item, media_type) for item in case["candidates"]]


def _episode_family(candidate):
    return next(item for item in candidate.evidence if item["family"] == "episode")


def test_obsession_2025_without_runtime_accepts_fallback_with_alternatives(cases):
    case = cases["movies"]["obsession_2025"]

    outcome = adjudicate_candidates(
        _candidates(case, "movie"),
        case["guess"],
        "movie",
        _policy(),
        source="search",
    )

    expected = case["expected_without_runtime"]
    assert outcome.status == expected["status"]
    assert outcome.selected.tmdb_id == expected["selected_tmdb_id"]
    assert outcome.decision["fallback_reason"] == "ambiguity_adjudicated"
    assert outcome.decision["accepted"] is True
    assert {item["tmdb_id"] for item in outcome.decision["alternatives"]} == {
        item["tmdb_id"] for item in case["candidates"]
    }
    assert "title_selection_uncertain" not in json.dumps(outcome.decision)


def test_obsession_2025_runtime_eliminates_shorts_and_keeps_feature(cases):
    case = cases["movies"]["obsession_2025"]
    source = "Obsession.2025.mkv"

    outcome = adjudicate_candidates(
        _candidates(case, "movie"),
        case["guess"],
        "movie",
        _policy(),
        source="search",
        runtime_evidence=[
            {
                "source": source,
                "runtime_minutes": case["observed_runtime_minutes"],
            }
        ],
    )

    expected = case["expected_with_runtime"]
    assert outcome.status == expected["status"]
    assert outcome.selected.tmdb_id == expected["selected_tmdb_id"]
    by_id = {candidate.tmdb_id: candidate for candidate in outcome.ordered}
    for tmdb_id in expected["eliminated_tmdb_ids"]:
        assert by_id[tmdb_id].eliminated is True
        assert "runtime_class_conflict" in by_id[tmdb_id].elimination_reasons
    assert by_id[expected["selected_tmdb_id"]].eliminated is False


def test_obsesion_without_year_never_uses_oldest_as_tiebreak(cases):
    case = cases["movies"]["obsesion_without_year"]

    outcome = adjudicate_candidates(
        _candidates(case, "movie"),
        case["guess"],
        "movie",
        _policy(),
        source="search",
    )

    assert outcome.status == case["expected"]["status"]
    assert outcome.selected.tmdb_id == case["expected"]["selected_tmdb_id"]
    assert outcome.selected.tmdb_id != case["expected"]["oldest_tmdb_id"]
    assert outcome.selected.year != min(item["year"] for item in case["candidates"])


def test_the_office_fallback_without_year_and_confident_with_2005(cases):
    case = cases["tv"]["the_office"]

    ambiguous = adjudicate_candidates(
        _candidates(case, "tv"),
        case["guess_without_year"],
        "tv",
        _policy("tv"),
        source="search",
    )
    dated = adjudicate_candidates(
        _candidates(case, "tv"),
        case["guess_2005"],
        "tv",
        _policy("tv"),
        source="search",
    )

    assert ambiguous.status == case["expected_without_year"]["status"]
    assert ambiguous.selected.tmdb_id == case["expected_without_year"]["selected_tmdb_id"]
    assert len(ambiguous.decision["alternatives"]) == 3
    assert dated.status == case["expected_2005"]["status"]
    assert dated.selected.tmdb_id == case["expected_2005"]["selected_tmdb_id"]
    assert dated.decision["phase_counts"] == {
        "discovered": 3,
        "enriched": 3,
        "eliminated": 2,
        "plausible": 1,
    }


def test_tv_intents_cover_cap_absolute_special_double_season_and_multifile_pack(
    cases, tmp_path
):
    case = cases["tv"]["episode_intents"]
    for filename in case["files"]:
        (tmp_path / filename).write_bytes(b"media")
    double_source = next(name for name in case["files"] if "E01E02" in name)
    runtime = [
        {
            "source": double_source,
            "runtime_minutes": case["double_episode_runtime_minutes"],
        }
    ]

    intents = collect_file_episode_intents(tmp_path, _policy("tv"), runtime)

    assert len(intents) == case["expected_sources"]
    assert {item["source"] for item in intents} == set(case["files"])
    assert any(item["season"] == 1 and item["episodes"] == [1] for item in intents)
    assert any(item["absolute_episode"] == 14 for item in intents)
    assert any(item["is_special"] and item["season"] == 0 for item in intents)
    assert any(item["episodes"] == [1, 2] for item in intents)
    assert any(item["is_season_pack"] and item["season"] == 2 for item in intents)

    candidate = ResolverCandidate(
        2316,
        "tv",
        "The Office",
        "The Office",
        2005,
        ["The Office"],
        season_count=2,
        episode_runtime_minutes=[case["candidate_episode_runtime_minutes"]],
        season_episode_counts={0: 2, 1: 20, 2: 20},
        known_episodes={0: [1, 2], 1: list(range(1, 21)), 2: list(range(1, 21))},
    )
    outcome = adjudicate_candidates(
        [candidate],
        {"title": "The Office", "year": 2005, "_episode_intents": intents},
        "tv",
        _policy("tv"),
        source="search",
        runtime_evidence=runtime,
    )

    assert outcome.status == "ACCEPTED_CONFIDENT"
    assert _episode_family(candidate)["state"] == "AGREE"
    subchecks = {
        item["name"]: item["state"]
        for item in _episode_family(candidate)["value"]["subchecks"]
    }
    assert subchecks == {
        "season": "AGREE",
        "numbered_episode": "AGREE",
        "multi_episode": "AGREE",
        "absolute_episode": "AGREE",
        "special": "AGREE",
        "season_pack": "AGREE",
    }
    runtime_family = next(
        item for item in candidate.evidence if item["family"] == "runtime"
    )
    assert runtime_family["state"] == "AGREE"


def test_missing_episode_in_every_tv_candidate_is_a_hard_block(cases):
    case = cases["tv"]["missing_episode"]
    candidates = []
    for tmdb_id in (2316, 2996):
        candidates.append(
            ResolverCandidate(
                tmdb_id,
                "tv",
                "The Office",
                "The Office",
                None,
                ["The Office"],
                season_count=1,
                season_episode_counts={
                    case["season"]: case["candidate_episode_count"]
                },
                known_episodes={
                    case["season"]: list(
                        range(1, case["candidate_episode_count"] + 1)
                    )
                },
            )
        )

    outcome = adjudicate_candidates(
        candidates,
        {
            "title": "The Office",
            "_episode_intents": [
                {
                    "source": "The.Office.S01E99.mkv",
                    "season": case["season"],
                    "episodes": [case["episode"]],
                    "absolute_episode": None,
                    "is_season_pack": False,
                }
            ],
        },
        "tv",
        _policy("tv"),
        source="search",
    )

    assert outcome.status == case["expected_status"]
    assert outcome.selected is None
    assert outcome.decision["phase_counts"]["plausible"] == 0
    assert all(
        "episode_conflict" in candidate.elimination_reasons
        for candidate in outcome.ordered
    )


def test_decision_is_order_invariant_and_candidate_five_is_not_truncated(cases):
    obsession = cases["movies"]["obsession_2025"]
    selected_ids = set()
    alternative_orders = set()
    for permutation in itertools.permutations(obsession["candidates"]):
        local_case = {"candidates": list(permutation)}
        outcome = adjudicate_candidates(
            _candidates(local_case, "movie"),
            obsession["guess"],
            "movie",
            _policy(),
            source="search",
        )
        selected_ids.add(outcome.selected.tmdb_id)
        alternative_orders.add(
            tuple(item["tmdb_id"] for item in outcome.decision["alternatives"])
        )
    assert selected_ids == {obsession["expected_without_runtime"]["selected_tmdb_id"]}
    assert len(alternative_orders) == 1

    outside = cases["movies"]["candidate_outside_top_three"]
    outcome = adjudicate_candidates(
        _candidates(outside, "movie"),
        outside["guess"],
        "movie",
        _policy(),
        source="search",
    )
    assert outcome.selected.tmdb_id == outside["expected_tmdb_id"]
    assert outcome.decision["phase_counts"]["discovered"] == 5
    assert len(outcome.decision["alternatives"]) == 5


def test_unknown_data_is_not_a_contradiction_and_explicit_id_is_authoritative():
    unknown = ResolverCandidate(
        70, "movie", "Objetivo", "Objetivo", None, ["Objetivo"]
    )
    unknown_outcome = adjudicate_candidates(
        [unknown],
        {"title": "Objetivo", "year": 2025},
        "movie",
        _policy(),
        source="search",
        runtime_evidence=[{"source": "Objetivo.mkv", "runtime_minutes": 100}],
    )

    assert unknown_outcome.status == "ACCEPTED_CONFIDENT"
    states = {item["family"]: item["state"] for item in unknown.evidence}
    assert states["year"] == "UNKNOWN"
    assert states["runtime"] == "UNKNOWN"
    assert unknown.disagree_count == 0

    explicit = ResolverCandidate(
        88, "movie", "Canonical TMDb Title", "Canonical TMDb Title", None, []
    )
    explicit_outcome = adjudicate_candidates(
        [explicit],
        {"title": "Ruido del nombre descargado"},
        "movie",
        _policy(),
        source="tmdb_id",
    )
    explicit_states = {item["family"]: item["state"] for item in explicit.evidence}
    assert explicit_outcome.status == "ACCEPTED_CONFIDENT"
    assert explicit_outcome.selected.tmdb_id == 88
    assert explicit_states["explicit_id"] == "AGREE"


def test_partial_provider_failure_retries_and_v2_cache_is_versioned(cases):
    case = cases["movies"]["obsession_2025"]
    retry = adjudicate_candidates(
        _candidates(case, "movie"),
        case["guess"],
        "movie",
        _policy(),
        source="search",
        provider_failures=1,
        coverage_limited=True,
    )

    assert retry.status == "RETRY_PROVIDER"
    assert retry.selected is None
    assert retry.decision["accepted"] is False
    assert retry.decision["fallback_reason"] == "provider_unavailable"
    assert retry.decision["alternatives"]

    guessed = copy.deepcopy(case["guess"])
    first = cache_key(
        "movie", ["Obsession.2025.mkv"], guessed, None, None, resolution_fingerprint="a"
    )
    second = cache_key(
        "movie", ["Obsession.2025.mkv"], guessed, None, None, resolution_fingerprint="b"
    )
    direct = cache_key(
        "movie", ["Obsession.tmdb-1339713.mkv"], guessed, "1339713", None,
        resolution_fingerprint="a",
    )
    assert RESOLVER_ALGORITHM_VERSION == "phased-er-v2"
    assert RESOLVER_CACHE_VERSION == 5
    assert len({first, second, direct}) == 3
    assert all(len(value) == 64 for value in (first, second, direct))
