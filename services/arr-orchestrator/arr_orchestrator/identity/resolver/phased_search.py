"""Descubrimiento y enriquecimiento amplios para ``phased-er-v2``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .candidate_data import candidate_from_payload, merge_search_payload
from .candidate_search import _SearchPlanItem, _build_query_inputs, _build_search_plan
from .models import ResolutionError, ResolverCandidate, ResolverUnavailable
from .title_matching import analyze_candidate_title_evidence, resolved_title_evidence
from .text import as_int, normalize_title, unique


MAX_TMDB_SEARCHES_V2 = 12
MAX_CANDIDATE_IDS_V2 = 60
MAX_DETAIL_REQUESTS_V2 = 40
MAX_DETAIL_BATCH_V2 = 8

GetPayload = Callable[[str, Dict[str, object]], Dict[str, object]]
Details = Callable[[str, int, Optional[str]], ResolverCandidate]


@dataclass
class SearchCoverage:
    candidates: List[ResolverCandidate] = field(default_factory=list)
    discovered: int = 0
    enriched: int = 0
    search_requests: int = 0
    detail_requests: int = 0
    provider_failures: int = 0
    coverage_limited: bool = False
    limit_reasons: List[str] = field(default_factory=list)
    max_searches: int = MAX_TMDB_SEARCHES_V2
    max_candidates: int = MAX_CANDIDATE_IDS_V2
    max_details: int = MAX_DETAIL_REQUESTS_V2
    batch_size: int = MAX_DETAIL_BATCH_V2

    def trace(self) -> Dict[str, object]:
        return {
            "mode": "phased-er-v2",
            "max_searches": self.max_searches,
            "max_candidates": self.max_candidates,
            "max_details": self.max_details,
            "batch_size": self.batch_size,
            "executed_searches": self.search_requests,
            "detail_requests": self.detail_requests,
            "discovered": self.discovered,
            "enriched": self.enriched,
            "provider_failures": self.provider_failures,
            "coverage_limited": self.coverage_limited,
            "limit_reasons": list(self.limit_reasons),
        }


def discover_and_enrich(
    media_type: str,
    query: str,
    guessed: Dict[str, object],
    language: str,
    region: str,
    policy: Dict[str, object],
    get_payload: GetPayload,
    details: Details,
) -> SearchCoverage:
    coverage_rules = (
        policy.get("coverage") if isinstance(policy.get("coverage"), dict) else {}
    )
    max_searches = min(
        MAX_TMDB_SEARCHES_V2,
        max(1, int(coverage_rules.get("max_searches", MAX_TMDB_SEARCHES_V2))),
    )
    max_candidates = min(
        MAX_CANDIDATE_IDS_V2,
        max(1, int(coverage_rules.get("max_candidates", MAX_CANDIDATE_IDS_V2))),
    )
    max_details = min(
        MAX_DETAIL_REQUESTS_V2,
        max(1, int(coverage_rules.get("max_details", MAX_DETAIL_REQUESTS_V2))),
    )
    batch_size = min(
        MAX_DETAIL_BATCH_V2,
        max(1, int(coverage_rules.get("batch_size", MAX_DETAIL_BATCH_V2))),
    )
    variant_rules = (
        policy.get("query_variants")
        if isinstance(policy.get("query_variants"), dict)
        else {}
    )
    search_guess = dict(guessed)
    search_guess["_title_evidence"] = resolved_title_evidence(
        guessed, policy.get("title_matching")
    )
    query_inputs = _build_query_inputs(query, search_guess, variant_rules)
    plan = _build_search_plan(
        query_inputs,
        as_int(guessed.get("year")),
        language,
        str(policy.get("fallback_language") or "en-US"),
        bool(policy.get("use_fallback_language", True)),
        variant_rules,
    )
    result = SearchCoverage(
        max_searches=max_searches,
        max_candidates=max_candidates,
        max_details=max_details,
        batch_size=batch_size,
    )
    raw: Dict[int, Dict[str, object]] = {}
    queue: List[tuple[_SearchPlanItem, int]] = [(search, 1) for search in plan]
    candidate_cap_reached = False
    while queue and result.search_requests < max_searches and not candidate_cap_reached:
        search, page = queue.pop(0)
        endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
        params: Dict[str, object] = {"query": search.query, "language": search.language}
        if page > 1:
            params["page"] = page
        if media_type == "movie":
            params["region"] = region
            if search.year:
                params["year"] = search.year
        elif search.year:
            params["first_air_date_year"] = search.year
        result.search_requests += 1
        try:
            payload = get_payload(endpoint, params)
        except (ResolverUnavailable, ResolutionError):
            result.provider_failures += 1
            _limit(result, "provider_partial")
            continue
        items = [
            dict(item)
            for item in payload.get("results") or []
            if isinstance(item, dict)
            and (as_int(item.get("id")) or 0) > 0
        ]
        items.sort(
            key=lambda item: _discovery_order(
                candidate_from_payload(media_type, item), guessed, policy
            )
        )
        # TMDb publica ``total_pages`` de forma autoritativa. No se deduce a
        # partir del tamano de la ultima pagina: una pagina final corta haria
        # inventar paginas inexistentes y marcar falsamente la cobertura.
        total_pages = max(page, as_int(payload.get("total_pages")) or page)
        for index, item in enumerate(items):
            candidate_id = int(item["id"])
            raw[candidate_id] = merge_search_payload(
                media_type,
                raw.get(candidate_id),
                dict(item),
                provenance={
                    "phase": search.phase,
                    "source": search.source,
                    "query": search.query,
                },
            )
            if len(raw) >= max_candidates:
                more_unique_here = any(
                    int(following["id"]) not in raw for following in items[index + 1 :]
                )
                if more_unique_here or page < total_pages or bool(queue):
                    _limit(result, "candidate_cap")
                candidate_cap_reached = True
                break
        if not candidate_cap_reached and page < total_pages:
            queue.append((search, page + 1))
    if queue and not candidate_cap_reached:
        _limit(result, "search_cap")
    result.discovered = len(raw)
    candidates = [candidate_from_payload(media_type, item) for item in raw.values()]
    candidates.sort(key=lambda item: _discovery_order(item, guessed, policy))

    enriched_by_id: Dict[int, ResolverCandidate] = {}
    invalid_detail_ids: set[int] = set()
    detail_budget = max_details
    attempted_candidates = 0
    for start in range(0, min(len(candidates), max_details), batch_size):
        batch = candidates[start : start + batch_size]
        for candidate in batch:
            if detail_budget <= 0:
                _limit(result, "detail_cap")
                break
            detail_budget -= 1
            attempted_candidates += 1
            result.detail_requests += 1
            try:
                detailed = details(media_type, candidate.tmdb_id, language)
            except ResolverUnavailable:
                result.provider_failures += 1
                _limit(result, "provider_partial")
                continue
            except ResolutionError:
                # Un ID que TMDb ya no reconoce no es una ficha parcial ni un
                # candidato plausible. Se descarta sin convertirlo en caída
                # del proveedor.
                invalid_detail_ids.add(candidate.tmdb_id)
                continue
            _merge_search_context(detailed, candidate)
            if media_type == "tv":
                used, failures, seasons_truncated = _enrich_tv_seasons(
                    detailed,
                    guessed,
                    policy,
                    language,
                    get_payload,
                    detail_budget,
                )
                detail_budget -= used
                result.detail_requests += used
                result.provider_failures += failures
                if failures:
                    _limit(result, "provider_partial")
                if seasons_truncated:
                    _limit(result, "tv_season_cap")
            enriched_by_id[candidate.tmdb_id] = detailed
            result.enriched += 1
        if detail_budget <= 0:
            break
    if attempted_candidates < len(candidates):
        _limit(result, "detail_cap")
    result.candidates = [
        enriched_by_id.get(candidate.tmdb_id, candidate)
        for candidate in candidates
        if candidate.tmdb_id not in invalid_detail_ids
    ]
    return result


def _discovery_order(
    candidate: ResolverCandidate,
    guessed: Dict[str, object],
    policy: Dict[str, object],
) -> tuple[object, ...]:
    analysis = analyze_candidate_title_evidence(
        [candidate.title, candidate.original_title, *candidate.aliases],
        guessed,
        policy.get("title_matching"),
    )
    exact_title = any(
        bool(item.get("identity_exact")) for item in analysis.get("matches") or []
    )
    guessed_year = as_int(guessed.get("year"))
    exact_year = guessed_year is not None and candidate.year == guessed_year
    return (
        -int(exact_year),
        -int(exact_title),
        -float(candidate.popularity),
        -int(candidate.vote_count),
        -(candidate.year or 0),
        candidate.tmdb_id,
    )


def _merge_search_context(
    detailed: ResolverCandidate, search_candidate: ResolverCandidate
) -> None:
    if not detailed.original_language:
        detailed.original_language = search_candidate.original_language
    detailed.aliases = unique(
        [
            detailed.title,
            detailed.original_title,
            *detailed.aliases,
            search_candidate.title,
            search_candidate.original_title,
            *search_candidate.aliases,
        ]
    )
    detailed.search_provenance = dict(search_candidate.search_provenance)
    if not detailed.popularity:
        detailed.popularity = search_candidate.popularity
    if not detailed.vote_count:
        detailed.vote_count = search_candidate.vote_count


def _enrich_tv_seasons(
    candidate: ResolverCandidate,
    guessed: Dict[str, object],
    policy: Dict[str, object],
    language: str,
    get_payload: GetPayload,
    budget: int,
) -> tuple[int, int, bool]:
    rules = policy.get("tv") if isinstance(policy.get("tv"), dict) else {}
    if not bool(rules.get("validate_season", True)):
        return 0, 0, False
    seasons = {
        season
        for item in guessed.get("_episode_intents") or []
        if isinstance(item, dict)
        and (season := as_int(item.get("season"))) is not None
    }
    fallback_season = as_int(guessed.get("season"))
    if fallback_season is not None:
        seasons.add(fallback_season)
    ordered_seasons = sorted(seasons)
    request_budget = max(0, int(budget))
    seasons_truncated = len(ordered_seasons) > request_budget
    requests = 0
    failures = 0
    for season in ordered_seasons:
        if requests >= request_budget:
            break
        requests += 1
        try:
            payload = get_payload(
                f"/tv/{candidate.tmdb_id}/season/{season}", {"language": language}
            )
        except ResolverUnavailable:
            failures += 1
            continue
        except ResolutionError:
            candidate.known_episodes[season] = []
            candidate.season_episode_counts[season] = 0
            continue
        episodes = [
            item for item in payload.get("episodes") or [] if isinstance(item, dict)
        ]
        numbers = sorted(
            {
                episode
                for item in episodes
                if (episode := as_int(item.get("episode_number"))) is not None
            }
        )
        candidate.known_episodes[season] = numbers
        candidate.season_episode_counts[season] = len(numbers)
        runtimes = [
            runtime
            for item in episodes
            if (runtime := as_int(item.get("runtime"))) is not None and runtime > 0
        ]
        candidate.episode_runtime_minutes = sorted(
            set([*candidate.episode_runtime_minutes, *runtimes])
        )
    return requests, failures, seasons_truncated


def _limit(result: SearchCoverage, reason: str) -> None:
    result.coverage_limited = True
    if reason not in result.limit_reasons:
        result.limit_reasons.append(reason)


__all__ = [
    "MAX_CANDIDATE_IDS_V2",
    "MAX_DETAIL_BATCH_V2",
    "MAX_DETAIL_REQUESTS_V2",
    "MAX_TMDB_SEARCHES_V2",
    "SearchCoverage",
    "discover_and_enrich",
]
