import copy
from types import SimpleNamespace

from arr_orchestrator.identity.defaults import factory_identity_rules
from arr_orchestrator.identity.resolver.cache import (
    RESOLVER_ALGORITHM_VERSION,
    RESOLVER_CACHE_VERSION,
    cache_key,
)
from arr_orchestrator.identity.resolver.models import (
    ResolutionError,
    ResolverCandidate,
    ResolverUnavailable,
)
from arr_orchestrator.identity.resolver.evidence import best_guess, probe_media_runtimes
from arr_orchestrator.identity.resolver.phased import adjudicate_candidates
from arr_orchestrator.identity.resolver.phased_search import (
    MAX_CANDIDATE_IDS_V2,
    MAX_DETAIL_BATCH_V2,
    MAX_DETAIL_REQUESTS_V2,
    MAX_TMDB_SEARCHES_V2,
    discover_and_enrich,
)
from arr_orchestrator.identity.resolver.policy import effective_policy
from arr_orchestrator.identity.resolver.rules import (
    apply_query_aliases,
    matching_forced_rule,
    parse_forced_matches,
    parse_query_aliases,
)
from arr_orchestrator.identity.resolver.title_matching import (
    analyze_candidate_title_evidence,
    best_title_match,
    matching_rules_for_pairs,
    resolved_title_evidence,
)


def _policy(category="movies"):
    return effective_policy(factory_identity_rules(), category)


def _movie(
    tmdb_id,
    title="Objetivo",
    year=2024,
    *,
    popularity=0.0,
    votes=0,
    runtime=100,
):
    return ResolverCandidate(
        tmdb_id,
        "movie",
        title,
        title,
        year,
        [title],
        popularity=popularity,
        vote_count=votes,
        runtime_minutes=runtime,
    )


def _movie_payload(tmdb_id, title="Objetivo", year=2024, popularity=1.0):
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "release_date": f"{year}-01-01",
        "popularity": popularity,
        "vote_count": tmdb_id * 10,
    }


def test_effective_policy_projects_shared_and_category_v2_rules():
    snapshot = factory_identity_rules()
    resolver = snapshot["resolver"]
    resolver["locales"]["movies"] = {"language": "fr-FR", "region": "FR"}
    resolver["aliases"]["movies"] = ["Origine | Destination"]
    resolver["forced_matches"]["movies"] = ["Destination | 2024 | 77"]
    resolver["coverage"]["max_candidates"] = 42
    resolver["movies"]["runtime_tolerance_minutes"] = 13

    policy = effective_policy(snapshot, "movies")

    assert policy["algorithm"] == "phased-er-v2"
    assert policy["language"] == "fr-FR"
    assert policy["region"] == "FR"
    assert policy["query_aliases"] == ["Origine | Destination"]
    assert policy["forced_matches"] == ["Destination | 2024 | 77"]
    assert policy["coverage"]["max_candidates"] == 42
    assert policy["movies"]["runtime_tolerance_minutes"] == 13
    assert policy["retry"]["max_attempts"] == 3
    assert "scoring" not in policy
    assert "acceptance" not in policy
    assert len(policy["fingerprint"]) == 64


def test_effective_policy_ignores_legacy_scoring_but_keeps_safe_direct_values():
    legacy = {
        "movies": {
            "language": "en-US",
            "region": "US",
            "query_aliases": ["Origin | Target"],
            "forced_matches": ["Target | 88"],
            "scoring": {"title_exact": 999},
            "acceptance": {"min_score": 999},
        },
        "scoring": {"title_exact": 999},
    }

    policy = effective_policy(legacy, "movies")

    assert policy["language"] == "en-US"
    assert policy["region"] == "US"
    assert policy["query_aliases"] == ["Origin | Target"]
    assert policy["forced_matches"] == ["Target | 88"]
    assert "scoring" not in policy
    assert "acceptance" not in policy

    changed = copy.deepcopy(legacy)
    changed["movies"]["query_aliases"] = ["Origin | Another"]
    assert effective_policy(changed, "movies")["fingerprint"] != policy["fingerprint"]


def test_cache_v5_separates_algorithm_evidence_and_rules_fingerprint():
    assert RESOLVER_CACHE_VERSION == 5
    assert RESOLVER_ALGORITHM_VERSION == "phased-er-v2"
    guessed = {
        "title": "Objetivo",
        "year": 2024,
        "_title_evidence": [
            {
                "value": "Objetivo",
                "role": "primary",
                "source": "parser",
                "group_id": "parser:0",
            }
        ],
    }
    first = cache_key(
        "movie", ["Objetivo.2024"], guessed, None, None, resolution_fingerprint="a"
    )

    changed_role = copy.deepcopy(guessed)
    changed_role["_title_evidence"][0]["role"] = "alternate"
    second = cache_key(
        "movie",
        ["Objetivo.2024"],
        changed_role,
        None,
        None,
        resolution_fingerprint="a",
    )
    third = cache_key(
        "movie", ["Objetivo.2024"], guessed, None, None, resolution_fingerprint="b"
    )

    assert len({first, second, third}) == 3
    assert all(len(value) == 64 for value in (first, second, third))


def test_aliases_are_directional_and_forced_match_prefers_exact_year():
    aliases = parse_query_aliases(
        ["A | B", "B | C", {"source": "A", "destination": "B"}]
    )
    guessed = apply_query_aliases(
        {"title": "A", "_title_candidates": ["A"]}, aliases
    )

    assert guessed["_rule_query_aliases"] == ["B"]
    assert "C" not in guessed["_title_candidates"]
    configured = [
        item
        for item in guessed["_title_evidence"]
        if item["role"] == "configured_primary"
    ]
    assert [item["value"] for item in configured] == ["B"]

    forced = parse_forced_matches(
        ["Objetivo | 99", "Objetivo | 2024 | 77", "Objetivo | 2023 | 66"]
    )
    assert matching_forced_rule(
        {"title": "Objetivo", "year": 2024}, forced
    ) == ("Objetivo", 2024, 77)
    assert matching_forced_rule(
        {"title": "Objetivo", "year": 2025}, forced
    ) == ("Objetivo", None, 99)


def test_title_rules_produce_evidence_without_scores():
    enabled = {
        "roman_arabic_equivalence": True,
        "allow_omitted_part_number": True,
        "omitted_part_min_words": 3,
        "supplemental_min_chars": 3,
    }
    roman = best_title_match(["Saga 4 Capitulo final"], ["Saga IV Capitulo final"], enabled)
    omitted = best_title_match(["Saga Capitulo final"], ["Saga IV Capitulo final"], enabled)
    disabled = best_title_match(
        ["Saga 4 Capitulo final"],
        ["Saga IV Capitulo final"],
        {**enabled, "roman_arabic_equivalence": False},
    )

    assert roman.exact
    assert roman.exact_pair.roman_equivalences == (("IV", "4"),)
    assert omitted.exact and omitted.exact_pair.used_omitted_part_number
    assert disabled.exact is False
    paths = {
        item["path"]
        for item in matching_rules_for_pairs([roman.exact_pair, omitted.exact_pair])
    }
    assert paths == {
        "resolver.title_matching.roman_arabic_equivalence",
        "resolver.title_matching.allow_omitted_part_number",
    }

    guessed = {
        "title": "Principal",
        "_title_evidence": [
            {
                "value": "Principal",
                "role": "primary",
                "source": "parser",
                "group_id": "parser:0",
            },
            {
                "value": "EP",
                "role": "alternate",
                "source": "parser",
                "group_id": "parser:0",
            },
        ],
    }
    evidence = resolved_title_evidence(guessed, enabled)
    assert [item["value"] for item in evidence] == ["Principal"]
    analysis = analyze_candidate_title_evidence(["Principal"], guessed, enabled)
    assert analysis["level"] == "primary"
    assert "score" not in analysis


def test_discovery_uses_phased_order_and_reports_search_cap():
    policy = _policy()
    policy["use_fallback_language"] = False
    policy["query_variants"] = {
        "with_year": False,
        "without_year": True,
        "use_parser_candidates": True,
        "use_guessit": False,
        "use_tail_cleanup": False,
        "use_spanish_correction": False,
    }
    policy["coverage"] = {
        "max_searches": 2,
        "max_candidates": 60,
        "batch_size": 2,
        "max_details": 40,
        "total_budget_ms": 20_000,
    }
    guessed = {
        "title": "Primary",
        "_title_evidence": [
            {
                "value": "Configured",
                "role": "configured_primary",
                "source": "configured_alias",
                "group_id": "configured:0",
            },
            {
                "value": "Primary",
                "role": "primary",
                "source": "parser",
                "group_id": "parser:0",
            },
            {
                "value": "Alternate",
                "role": "alternate",
                "source": "parentheses",
                "group_id": "parser:0",
            },
        ],
    }
    queries = []

    def get_payload(_endpoint, params):
        queries.append(params["query"])
        candidate_id = {"Configured": 1, "Primary": 2, "Alternate": 3}[params["query"]]
        return {"results": [_movie_payload(candidate_id, params["query"])]}

    def details(_media_type, tmdb_id, _language):
        title = {1: "Configured", 2: "Primary", 3: "Alternate"}[tmdb_id]
        return _movie(tmdb_id, title)

    coverage = discover_and_enrich(
        "movie", "Primary", guessed, "es-ES", "ES", policy, get_payload, details
    )

    assert queries == ["Configured", "Primary"]
    assert coverage.search_requests == 2
    assert coverage.discovered == 2
    assert coverage.enriched == 2
    assert coverage.coverage_limited is True
    assert coverage.limit_reasons == ["search_cap"]
    assert coverage.trace()["mode"] == "phased-er-v2"


def test_discovery_enforces_candidate_detail_and_batch_hard_caps():
    assert MAX_TMDB_SEARCHES_V2 == 12
    assert MAX_CANDIDATE_IDS_V2 == 60
    assert MAX_DETAIL_REQUESTS_V2 == 40
    assert MAX_DETAIL_BATCH_V2 == 8
    policy = _policy()
    policy["use_fallback_language"] = False
    policy["query_variants"].update(
        {
            "with_year": False,
            "without_year": True,
            "use_parser_candidates": False,
            "use_guessit": False,
            "use_tail_cleanup": False,
            "use_spanish_correction": False,
        }
    )
    policy["coverage"] = {
        "max_searches": 1,
        "max_candidates": 3,
        "batch_size": 2,
        "max_details": 2,
        "total_budget_ms": 20_000,
    }
    detail_calls = []

    def details(_media_type, tmdb_id, _language):
        detail_calls.append(tmdb_id)
        return _movie(tmdb_id, popularity=tmdb_id)

    coverage = discover_and_enrich(
        "movie",
        "Objetivo",
        {"title": "Objetivo"},
        "es-ES",
        "ES",
        policy,
        lambda _endpoint, _params: {
            "results": [_movie_payload(index, popularity=index) for index in range(1, 5)]
        },
        details,
    )

    assert coverage.discovered == 3
    assert coverage.enriched == 2
    assert coverage.detail_requests == 2
    assert len(detail_calls) == 2
    assert set(coverage.limit_reasons) == {"candidate_cap", "detail_cap"}


def test_tv_season_enrichment_cap_becomes_coverage_limited_fallback():
    policy = _policy("tv")
    policy["use_fallback_language"] = False
    policy["query_variants"].update(
        {
            "with_year": False,
            "without_year": True,
            "use_parser_candidates": False,
            "use_guessit": False,
            "use_tail_cleanup": False,
            "use_spanish_correction": False,
        }
    )
    # Una ficha de serie y una temporada caben; la segunda temporada solicitada
    # queda fuera del mismo tope global de detalles.
    policy["coverage"] = {
        "max_searches": 1,
        "max_candidates": 1,
        "batch_size": 1,
        "max_details": 2,
        "total_budget_ms": 20_000,
    }
    guessed = {
        "title": "Serie",
        "_episode_intents": [
            {
                "source": "Serie.S01E01.mkv",
                "season": 1,
                "episodes": [1],
                "absolute_episode": None,
                "is_season_pack": False,
            },
            {
                "source": "Serie.S02E01.mkv",
                "season": 2,
                "episodes": [1],
                "absolute_episode": None,
                "is_season_pack": False,
            },
        ],
    }
    calls = []

    def get_payload(endpoint, _params):
        calls.append(endpoint)
        if endpoint == "/search/tv":
            return {
                "results": [
                    {
                        "id": 10,
                        "name": "Serie",
                        "original_name": "Serie",
                        "first_air_date": "2024-01-01",
                        "popularity": 10,
                        "vote_count": 100,
                    }
                ]
            }
        if endpoint == "/tv/10/season/1":
            return {"episodes": [{"episode_number": 1, "runtime": 42}]}
        raise AssertionError(f"Consulta fuera del presupuesto: {endpoint}")

    coverage = discover_and_enrich(
        "tv",
        "Serie",
        guessed,
        "es-ES",
        "ES",
        policy,
        get_payload,
        lambda *_args: ResolverCandidate(
            10,
            "tv",
            "Serie",
            "Serie",
            2024,
            ["Serie"],
            popularity=10,
            vote_count=100,
            season_count=2,
        ),
    )
    outcome = adjudicate_candidates(
        coverage.candidates,
        guessed,
        "tv",
        policy,
        source="search",
        discovered=coverage.discovered,
        enriched=coverage.enriched,
        coverage_limited=coverage.coverage_limited,
        provider_failures=coverage.provider_failures,
    )

    assert calls == ["/search/tv", "/tv/10/season/1"]
    assert coverage.detail_requests == 2
    assert coverage.provider_failures == 0
    assert coverage.coverage_limited is True
    assert "tv_season_cap" in coverage.limit_reasons
    assert coverage.trace()["coverage_limited"] is True
    assert "tv_season_cap" in coverage.trace()["limit_reasons"]
    assert coverage.candidates[0].known_episodes == {1: [1]}
    assert outcome.status == "ACCEPTED_FALLBACK"
    assert outcome.selected is not None
    assert outcome.selected.tmdb_id == 10
    assert outcome.decision["coverage_limited"] is True
    assert outcome.decision["fallback_reason"] == "coverage_limited"


def test_discovery_paginates_and_finds_a_candidate_outside_the_first_page():
    policy = _policy()
    policy["use_fallback_language"] = False
    policy["query_variants"].update(
        {
            "with_year": False,
            "without_year": True,
            "use_parser_candidates": False,
            "use_guessit": False,
            "use_tail_cleanup": False,
            "use_spanish_correction": False,
        }
    )
    policy["coverage"].update({"max_searches": 2, "max_candidates": 60})
    pages = []

    def get_payload(_endpoint, params):
        page = int(params.get("page") or 1)
        pages.append(page)
        if page == 1:
            return {
                "page": 1,
                "total_pages": 2,
                "total_results": 21,
                "results": [
                    _movie_payload(index, title=f"Relleno {index}")
                    for index in range(1, 21)
                ],
            }
        return {
            "page": 2,
            "total_pages": 2,
            "total_results": 21,
            "results": [_movie_payload(99, title="Objetivo")],
        }

    coverage = discover_and_enrich(
        "movie",
        "Objetivo",
        {"title": "Objetivo"},
        "es-ES",
        "ES",
        policy,
        get_payload,
        lambda _media_type, tmdb_id, _language: _movie(
            tmdb_id, title="Objetivo" if tmdb_id == 99 else f"Relleno {tmdb_id}"
        ),
    )

    assert pages == [1, 2]
    assert coverage.discovered == 21
    assert any(candidate.tmdb_id == 99 for candidate in coverage.candidates)
    assert "search_cap" not in coverage.limit_reasons


def test_provider_failure_is_retryable_after_phased_discovery():
    policy = _policy()
    policy["use_fallback_language"] = False
    policy["query_variants"].update(
        {
            "with_year": False,
            "without_year": True,
            "use_parser_candidates": False,
            "use_guessit": False,
            "use_tail_cleanup": False,
            "use_spanish_correction": False,
        }
    )

    def unavailable(_endpoint, _params):
        raise ResolverUnavailable("TMDb caido")

    coverage = discover_and_enrich(
        "movie",
        "Objetivo",
        {"title": "Objetivo"},
        "es-ES",
        "ES",
        policy,
        unavailable,
        lambda *_args: _movie(1),
    )
    outcome = adjudicate_candidates(
        coverage.candidates,
        {"title": "Objetivo"},
        "movie",
        policy,
        source="search",
        provider_failures=coverage.provider_failures,
        coverage_limited=coverage.coverage_limited,
    )

    assert coverage.provider_failures == 1
    assert coverage.limit_reasons == ["provider_partial"]
    assert outcome.status == "RETRY_PROVIDER"
    assert outcome.decision["accepted"] is False
    assert outcome.decision["fallback_reason"] == "provider_unavailable"


def test_tmdb_detail_not_found_removes_the_stale_search_candidate():
    policy = _policy()
    policy["use_fallback_language"] = False
    policy["query_variants"].update(
        {
            "with_year": False,
            "without_year": True,
            "use_parser_candidates": False,
            "use_guessit": False,
            "use_tail_cleanup": False,
            "use_spanish_correction": False,
        }
    )

    coverage = discover_and_enrich(
        "movie",
        "Objetivo",
        {"title": "Objetivo"},
        "es-ES",
        "ES",
        policy,
        lambda _endpoint, _params: {"results": [_movie_payload(91)]},
        lambda *_args: (_ for _ in ()).throw(ResolutionError("not found")),
    )

    assert coverage.discovered == 1
    assert coverage.enriched == 0
    assert coverage.provider_failures == 0
    assert coverage.candidates == []


def test_adjudication_is_lexicographic_and_never_emits_scores():
    policy = _policy()
    guessed = {"title": "Objetivo", "year": 2024}
    popular_tolerated_year = _movie(20, year=2023, popularity=100, votes=1000)
    exact_year = _movie(30, year=2024, popularity=1, votes=1)

    outcome = adjudicate_candidates(
        [popular_tolerated_year, exact_year],
        guessed,
        "movie",
        policy,
        source="search",
    )

    assert outcome.status == "ACCEPTED_FALLBACK"
    assert outcome.selected.tmdb_id == 30
    assert outcome.decision["selected_tmdb_id"] == 30
    assert outcome.decision["has_scoring"] is False
    assert outcome.decision["resolver_algorithm_version"] == "phased-er-v2"
    assert "score" not in outcome.decision["selected"]
    assert "margin" not in outcome.decision["selected"]
    for candidate in outcome.ordered:
        families = [item["family"] for item in candidate.evidence]
        assert len(families) == len(set(families))
        assert {item["state"] for item in candidate.evidence} <= {
            "AGREE",
            "DISAGREE",
            "UNKNOWN",
        }


def test_best_guess_uses_explicit_order_without_weights_and_merges_all_titles():
    policy = _policy()
    policy["guess_selection"] = {
        "base": -9999,
        "index_penalty": -9999,
        "year_bonus": -9999,
        "season_bonus": -9999,
        "parser_high_bonus": -9999,
    }

    guessed = best_guess(
        ["Objetivo", "Titulo alternativo (2024)"], "movie", policy
    )

    assert guessed["title"] == "Objetivo"
    assert guessed["year"] == 2024
    evidence_values = {
        str(item["value"]) for item in guessed["_title_evidence"]
    }
    assert {"Objetivo", "Titulo alternativo"} <= evidence_values
    assert len({item["group_id"] for item in guessed["_title_evidence"]}) >= 2


def test_explicit_year_tiebreaker_honours_movie_tolerance():
    policy = _policy()
    guessed = {"title": "Objetivo", "year": 2024}
    within_tolerance = _movie(8, year=2023, popularity=1)
    no_year = _movie(7, year=None, popularity=999)

    outcome = adjudicate_candidates(
        [no_year, within_tolerance], guessed, "movie", policy, source="search"
    )

    assert outcome.status == "ACCEPTED_FALLBACK"
    assert outcome.selected.tmdb_id == 8


def test_ffprobe_respects_the_shared_deadline(tmp_path):
    for index in range(3):
        (tmp_path / f"video-{index}.mkv").write_bytes(b"media")
    policy = _policy()
    now = [0.0]
    timeouts = []

    def runner(*_args, **kwargs):
        timeout = float(kwargs["timeout"])
        timeouts.append(timeout)
        now[0] += timeout
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    result = probe_media_runtimes(
        tmp_path,
        policy,
        runner,
        deadline=6.0,
        clock=lambda: now[0],
    )

    assert result == []
    assert timeouts == [5.0, 1.0]


def test_adjudication_statuses_cover_confident_fallback_and_hard_block():
    guessed = {"title": "Objetivo", "year": 2024}
    policy = _policy()

    confident = adjudicate_candidates(
        [_movie(1)], guessed, "movie", policy, source="search"
    )
    assert confident.status == "ACCEPTED_CONFIDENT"
    assert confident.decision["confidence"] == "high"

    limited = adjudicate_candidates(
        [_movie(1)],
        guessed,
        "movie",
        policy,
        source="search",
        coverage_limited=True,
    )
    assert limited.status == "ACCEPTED_FALLBACK"
    assert limited.decision["fallback_reason"] == "coverage_limited"

    contradicted = adjudicate_candidates(
        [_movie(2, title="Otra cosa", year=1990)],
        guessed,
        "movie",
        policy,
        source="search",
    )
    assert contradicted.status == "BLOCKED_HARD"
    assert contradicted.selected is None
    assert set(contradicted.ordered[0].elimination_reasons) == {
        "title_conflict",
        "year_conflict",
    }

    immutable_fallback = copy.deepcopy(policy)
    immutable_fallback["adjudication"]["fallback_on_ambiguity"] = False
    ambiguous = adjudicate_candidates(
        [_movie(3), _movie(4)], guessed, "movie", immutable_fallback, source="search"
    )
    assert ambiguous.status == "ACCEPTED_FALLBACK"
    assert ambiguous.selected is not None
    assert ambiguous.decision["fallback_reason"] == "ambiguity_adjudicated"


def test_movie_release_timeline_and_runtime_are_independent_evidence_families():
    policy = _policy()
    candidate = _movie(1, year=2020, runtime=100)
    candidate.release_years = [2024]
    candidate.release_timeline = [{"year": 2024, "country": "ES"}]

    accepted = adjudicate_candidates(
        [candidate],
        {"title": "Objetivo", "year": 2024},
        "movie",
        policy,
        source="search",
        runtime_evidence=[{"source": "video.mkv", "runtime_minutes": 101}],
    )
    verdicts = {item["family"]: item["state"] for item in candidate.evidence}
    assert accepted.status == "ACCEPTED_CONFIDENT"
    assert verdicts["year"] == "AGREE"
    assert verdicts["runtime"] == "AGREE"

    wrong_class = _movie(2, runtime=100)
    blocked = adjudicate_candidates(
        [wrong_class],
        {"title": "Objetivo", "year": 2024},
        "movie",
        policy,
        source="search",
        runtime_evidence=[{"source": "corto.mkv", "runtime_minutes": 20}],
    )
    assert blocked.status == "BLOCKED_HARD"
    assert wrong_class.elimination_reasons == ["runtime_class_conflict"]


def test_tv_evidence_validates_episode_special_pack_and_absolute_numbering():
    policy = _policy("tv")
    candidate = ResolverCandidate(
        10,
        "tv",
        "Serie",
        "Serie",
        2024,
        ["Serie"],
        season_count=2,
        season_episode_counts={0: 2, 1: 20, 2: 20},
        known_episodes={0: [1, 2], 1: list(range(1, 21)), 2: list(range(1, 21))},
        episode_runtime_minutes=[42],
    )
    guessed = {
        "title": "Serie",
        "year": 2024,
        "_episode_intents": [
            {
                "source": "Serie.S01E01-E02.mkv",
                "season": 1,
                "episodes": [1, 2],
                "absolute_episode": None,
                "is_season_pack": False,
            },
            {
                "source": "Serie.34.mkv",
                "season": None,
                "episodes": [],
                "absolute_episode": 34,
                "is_season_pack": False,
            },
            {
                "source": "Serie.S02.pack",
                "season": 2,
                "episodes": [],
                "absolute_episode": None,
                "is_season_pack": True,
            },
        ],
    }

    outcome = adjudicate_candidates(
        [candidate],
        guessed,
        "tv",
        policy,
        source="search",
        runtime_evidence=[
            {"source": "Serie.S01E01-E02.mkv", "runtime_minutes": 84}
        ],
    )
    verdicts = {item["family"]: item["state"] for item in candidate.evidence}

    assert outcome.status == "ACCEPTED_CONFIDENT"
    assert verdicts["episode"] == "AGREE"
    assert verdicts["runtime"] == "AGREE"
    assert len([item for item in candidate.evidence if item["family"] == "episode"]) == 1
    episode_value = next(
        item["value"] for item in candidate.evidence if item["family"] == "episode"
    )
    assert {item["name"] for item in episode_value["subchecks"]} == {
        "season",
        "numbered_episode",
        "multi_episode",
        "absolute_episode",
        "special",
        "season_pack",
    }

    disabled = copy.deepcopy(policy)
    disabled["tv"]["allow_absolute_episode"] = False
    blocked_candidate = copy.deepcopy(candidate)
    blocked = adjudicate_candidates(
        [blocked_candidate], guessed, "tv", disabled, source="search"
    )
    assert blocked.status == "BLOCKED_HARD"
    assert "absolute_episode_disabled" in blocked_candidate.elimination_reasons


def test_tv_episode_family_keeps_unknown_as_unknown_without_false_conflict():
    policy = _policy("tv")
    candidate = ResolverCandidate(
        11,
        "tv",
        "Serie",
        "Serie",
        2024,
        ["Serie"],
        season_count=1,
    )
    guessed = {
        "title": "Serie",
        "_episode_intents": [
            {
                "source": "Serie.S01E01.mkv",
                "season": 1,
                "episodes": [1],
                "absolute_episode": None,
                "is_season_pack": False,
            }
        ],
    }

    outcome = adjudicate_candidates(
        [candidate], guessed, "tv", policy, source="search"
    )

    episode = next(item for item in candidate.evidence if item["family"] == "episode")
    assert outcome.status == "ACCEPTED_CONFIDENT"
    assert episode["state"] == "AGREE"
    states = {item["name"]: item["state"] for item in episode["value"]["subchecks"]}
    assert states["season"] == "AGREE"
    assert states["numbered_episode"] == "UNKNOWN"


def test_tv_episode_family_preserves_all_hard_reasons_without_extra_votes():
    policy = _policy("tv")
    policy["tv"]["allow_multi_episode"] = False
    policy["tv"]["allow_absolute_episode"] = False
    candidate = ResolverCandidate(12, "tv", "Serie", "Serie", 2024, ["Serie"])
    guessed = {
        "title": "Serie",
        "_episode_intents": [
            {
                "source": "Serie.S01E01E02.mkv",
                "season": 1,
                "episodes": [1, 2],
                "absolute_episode": 2,
                "is_season_pack": False,
            }
        ],
    }

    outcome = adjudicate_candidates(
        [candidate], guessed, "tv", policy, source="search"
    )

    assert outcome.status == "BLOCKED_HARD"
    assert set(candidate.elimination_reasons) >= {
        "multi_episode_disabled",
        "absolute_episode_disabled",
    }
    assert len([item for item in candidate.evidence if item["family"] == "episode"]) == 1
