"""Comparacion, eliminacion de contradicciones y adjudicacion v2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .models import ResolverCandidate
from .title_matching import analyze_candidate_title_evidence
from .text import as_int


AGREE = "AGREE"
DISAGREE = "DISAGREE"
UNKNOWN = "UNKNOWN"


@dataclass
class Adjudication:
    status: str
    selected: Optional[ResolverCandidate]
    ordered: List[ResolverCandidate]
    decision: Dict[str, object]


def adjudicate_candidates(
    candidates: Sequence[ResolverCandidate],
    guessed: Dict[str, object],
    media_type: str,
    policy: Dict[str, object],
    *,
    source: str,
    runtime_evidence: Sequence[Dict[str, object]] = (),
    discovered: Optional[int] = None,
    enriched: Optional[int] = None,
    coverage_limited: bool = False,
    provider_failures: int = 0,
) -> Adjudication:
    direct_identity = source in {"tmdb_id", "imdb_id", "forced_match"}
    compared: List[ResolverCandidate] = []
    for candidate in candidates:
        _compare_candidate(
            candidate,
            guessed,
            media_type,
            policy,
            runtime_evidence,
            direct_identity=direct_identity,
        )
        compared.append(candidate)
    plausible = [candidate for candidate in compared if not candidate.eliminated]
    plausible.sort(key=lambda item: _adjudication_key(item, guessed))
    eliminated = sorted(
        (candidate for candidate in compared if candidate.eliminated),
        key=lambda item: _adjudication_key(item, guessed),
    )
    ordered = [*plausible, *eliminated]
    phase_counts = {
        "discovered": int(discovered if discovered is not None else len(candidates)),
        "enriched": int(enriched if enriched is not None else len(candidates)),
        "eliminated": len(eliminated),
        "plausible": len(plausible),
    }
    evidence = [
        {"tmdb_id": candidate.tmdb_id, "families": list(candidate.evidence)}
        for candidate in ordered
    ]
    alternatives = [_candidate_summary(candidate) for candidate in ordered[:20]]
    selected: Optional[ResolverCandidate] = plausible[0] if plausible else None
    if provider_failures:
        # Una respuesta parcial no es evidencia suficiente para fijar y cachear
        # identidad. Los topes internos de cobertura si permiten fallback; una
        # caida real del proveedor queda pendiente para reintento.
        status = "RETRY_PROVIDER"
        selected = None
        fallback_reason = "provider_unavailable"
        confidence = "none"
    elif selected is None:
        status = "BLOCKED_HARD"
        fallback_reason = "all_candidates_contradicted"
        confidence = "none"
    elif len(plausible) == 1 and not coverage_limited:
        status = "ACCEPTED_CONFIDENT"
        fallback_reason = None
        confidence = "high"
    else:
        status = "ACCEPTED_FALLBACK"
        fallback_reason = (
            "coverage_limited" if coverage_limited else "ambiguity_adjudicated"
        )
        confidence = "low"
    accepted = status.startswith("ACCEPTED_")
    selected_summary = _candidate_summary(selected) if selected is not None else None
    decision: Dict[str, object] = {
        "status": status,
        "accepted": accepted,
        "confidence": confidence,
        "fallback_reason": fallback_reason,
        "coverage_limited": bool(coverage_limited),
        "selected": selected_summary,
        "selected_tmdb_id": selected.tmdb_id if selected is not None else None,
        "alternatives": alternatives,
        "evidence": evidence,
        "phase_counts": phase_counts,
        "counters": {
            **phase_counts,
            "candidates_discovered": phase_counts["discovered"],
            "candidates_enriched": phase_counts["enriched"],
            "candidates_eliminated": phase_counts["eliminated"],
            "candidates_plausible": phase_counts["plausible"],
            "agree": selected.agree_count if selected is not None else 0,
            "disagree": selected.disagree_count if selected is not None else 0,
            "unknown": selected.unknown_count if selected is not None else 0,
        },
        "provider_failures": int(provider_failures),
        "has_scoring": False,
        "resolver_algorithm_version": "phased-er-v2",
    }
    return Adjudication(status, selected, ordered, decision)


def _compare_candidate(
    candidate: ResolverCandidate,
    guessed: Dict[str, object],
    media_type: str,
    policy: Dict[str, object],
    runtime_evidence: Sequence[Dict[str, object]],
    *,
    direct_identity: bool,
) -> None:
    candidate.evidence = []
    candidate.eliminated = False
    candidate.elimination_reasons = []
    _add(candidate, "explicit_id", AGREE if direct_identity else UNKNOWN, "explicit" if direct_identity else "absent")
    _add(
        candidate,
        "media_type",
        AGREE if candidate.media_type == media_type else DISAGREE,
        candidate.media_type,
        hard=candidate.media_type != media_type,
        reason="media_type_conflict",
    )
    title_analysis = analyze_candidate_title_evidence(
        [candidate.title, candidate.original_title, *candidate.aliases],
        guessed,
        policy.get("title_matching"),
    )
    candidate.title_match_level = str(title_analysis.get("level") or "none")
    candidate.title_matches = [
        dict(item) for item in title_analysis.get("matches") or [] if isinstance(item, dict)
    ]
    candidate.title_identity_exact_roles = [
        str(item) for item in title_analysis.get("identity_exact_roles") or []
    ]
    title_supported = candidate.title_match_level != "none"
    _add(
        candidate,
        "title",
        AGREE if title_supported else DISAGREE,
        candidate.title_match_level,
        hard=not title_supported and not direct_identity,
        reason="title_conflict",
    )
    if media_type == "movie":
        _compare_movie(candidate, guessed, policy, runtime_evidence)
    else:
        _compare_tv(candidate, guessed, policy, runtime_evidence)
    candidate.agree_count = sum(item["state"] == AGREE for item in candidate.evidence)
    candidate.disagree_count = sum(item["state"] == DISAGREE for item in candidate.evidence)
    candidate.unknown_count = sum(item["state"] == UNKNOWN for item in candidate.evidence)


def _compare_movie(
    candidate: ResolverCandidate,
    guessed: Dict[str, object],
    policy: Dict[str, object],
    runtime_evidence: Sequence[Dict[str, object]],
) -> None:
    rules = policy.get("movies") if isinstance(policy.get("movies"), dict) else {}
    guessed_year = as_int(guessed.get("year"))
    timeline_years = (
        candidate.release_years if bool(rules.get("use_release_timeline", True)) else []
    )
    years = sorted({year for year in [candidate.year, *timeline_years] if year})
    tolerance = max(0, int(rules.get("year_tolerance", 1)))
    year_state = _year_state(guessed_year, years, tolerance)
    hard_year = year_state == DISAGREE and bool(rules.get("hard_year_conflict", True))
    _add(
        candidate,
        "year",
        year_state,
        {
            "expected": guessed_year,
            "candidate": years[:12],
            "release_timeline": candidate.release_timeline[:24],
        },
        hard=hard_year,
        reason="year_conflict",
    )
    observed = _runtime_values(runtime_evidence)
    runtime_state = _runtime_state(
        observed,
        [candidate.runtime_minutes] if candidate.runtime_minutes else [],
        int(rules.get("runtime_tolerance_minutes", 10)),
        int(rules.get("runtime_tolerance_percent", 15)),
    )
    short_limit = int(rules.get("short_runtime_minutes", 40))
    feature_limit = int(rules.get("feature_runtime_minutes", 60))
    runtime_hard = bool(
        observed
        and candidate.runtime_minutes
        and (
            (max(observed) <= short_limit and candidate.runtime_minutes >= feature_limit)
            or (min(observed) >= feature_limit and candidate.runtime_minutes <= short_limit)
        )
    )
    _add(
        candidate,
        "runtime",
        runtime_state,
        {"observed_minutes": observed[:20], "candidate_minutes": candidate.runtime_minutes},
        hard=runtime_state == DISAGREE and runtime_hard,
        reason="runtime_class_conflict",
    )


def _compare_tv(
    candidate: ResolverCandidate,
    guessed: Dict[str, object],
    policy: Dict[str, object],
    runtime_evidence: Sequence[Dict[str, object]],
) -> None:
    rules = policy.get("tv") if isinstance(policy.get("tv"), dict) else {}
    defer_episode_conflicts = bool(guessed.get("_defer_episode_conflicts", False))
    guessed_year = as_int(guessed.get("year"))
    tv_year_state = _year_state(
        guessed_year, [candidate.year] if candidate.year else [], 1
    )
    _add(
        candidate,
        "year",
        tv_year_state,
        {"expected": guessed_year, "candidate": candidate.year},
        hard=tv_year_state == DISAGREE,
        reason="year_conflict",
    )
    intents = [
        item for item in guessed.get("_episode_intents") or [] if isinstance(item, dict)
    ]
    if not intents:
        fallback = {
            "season": as_int(guessed.get("season")),
            "episodes": _int_list(guessed.get("episode")),
            "absolute_episode": as_int(guessed.get("absolute_episode")),
            "is_season_pack": False,
        }
        intents = [fallback]
    seasons = {as_int(item.get("season")) for item in intents}
    seasons.discard(None)
    subchecks: List[Dict[str, object]] = []
    hard_reasons: List[str] = []

    def add_subcheck(name: str, state: str, value: object, reason: str = "") -> None:
        subchecks.append({"name": name, "state": state, "value": value})
        if state == DISAGREE and reason and reason not in hard_reasons:
            hard_reasons.append(reason)

    season_state = UNKNOWN
    if bool(rules.get("validate_season", True)) and seasons:
        season_state = _optional_checks_state(
            [_season_exists(candidate, int(season)) for season in seasons]
        )
    add_subcheck(
        "season",
        season_state,
        {
            "expected": sorted(int(item) for item in seasons),
            "known": candidate.season_episode_counts,
        },
        "season_conflict",
    )
    episode_checks: List[Optional[bool]] = []
    for intent in intents:
        season = as_int(intent.get("season"))
        episodes = _int_list(intent.get("episodes"))
        if season is None or not episodes:
            continue
        episode_checks.append(_episodes_exist(candidate, season, episodes))
    episode_state = UNKNOWN
    if bool(rules.get("validate_episode", True)) and episode_checks:
        episode_state = _optional_checks_state(episode_checks)
    add_subcheck(
        "numbered_episode",
        episode_state,
        {"checked": len(episode_checks)},
        "episode_conflict",
    )
    multi_episode_present = any(
        len(_int_list(item.get("episodes"))) > 1 for item in intents
    )
    multi_episode_state = UNKNOWN
    if multi_episode_present:
        multi_episode_state = (
            AGREE if bool(rules.get("allow_multi_episode", True)) else DISAGREE
        )
    add_subcheck(
        "multi_episode",
        multi_episode_state,
        {"present": multi_episode_present},
        "multi_episode_disabled",
    )
    absolute_present = any(as_int(item.get("absolute_episode")) is not None for item in intents)
    absolute_values = [
        value
        for item in intents
        if (value := as_int(item.get("absolute_episode"))) is not None
    ]
    absolute_state = UNKNOWN
    if absolute_present:
        if not bool(rules.get("allow_absolute_episode", True)):
            absolute_state = DISAGREE
        else:
            absolute_checks = [
                _absolute_episode_exists(candidate, value) for value in absolute_values
            ]
            absolute_state = _optional_checks_state(absolute_checks)
    absolute_reason = (
        "absolute_episode_disabled"
        if absolute_present and not bool(rules.get("allow_absolute_episode", True))
        else "absolute_episode_conflict"
    )
    add_subcheck(
        "absolute_episode",
        absolute_state,
        {"episodes": absolute_values},
        absolute_reason,
    )
    special_present = 0 in seasons
    special_exists = _season_exists(candidate, 0) if special_present else None
    special_state = UNKNOWN
    if special_present:
        if not bool(rules.get("allow_specials", True)):
            special_state = DISAGREE
        elif special_exists is True:
            special_state = AGREE
        elif special_exists is False:
            special_state = DISAGREE
    add_subcheck(
        "special",
        special_state,
        "season_0" if special_present else "absent",
        "special_conflict",
    )
    packs = [item for item in intents if bool(item.get("is_season_pack"))]
    pack_state = UNKNOWN
    if packs:
        if not bool(rules.get("allow_season_packs", True)):
            pack_state = DISAGREE
        elif season_state == AGREE:
            pack_state = AGREE
        elif season_state == DISAGREE:
            pack_state = DISAGREE
    add_subcheck(
        "season_pack",
        pack_state,
        {"count": len(packs)},
        "season_pack_conflict",
    )
    episode_family_state = (
        DISAGREE
        if any(item["state"] == DISAGREE for item in subchecks)
        else AGREE
        if any(item["state"] == AGREE for item in subchecks)
        else UNKNOWN
    )
    _add(
        candidate,
        "episode",
        episode_family_state,
        {"intents": intents[:20], "subchecks": subchecks},
        hard=episode_family_state == DISAGREE and not defer_episode_conflicts,
    )
    if not defer_episode_conflicts:
        for reason in hard_reasons:
            if reason not in candidate.elimination_reasons:
                candidate.elimination_reasons.append(reason)

    observed = _tv_runtime_values(intents, runtime_evidence)
    expected = candidate.episode_runtime_minutes
    runtime_state = _runtime_state(
        observed,
        expected,
        int(rules.get("runtime_tolerance_minutes", 8)),
        int(rules.get("runtime_tolerance_percent", 25)),
    )
    _add(
        candidate,
        "runtime",
        runtime_state,
        {"observed_minutes": observed[:20], "candidate_minutes": expected[:20]},
    )


def _optional_checks_state(checks: Sequence[Optional[bool]]) -> str:
    known = [value for value in checks if value is not None]
    if any(value is False for value in known):
        return DISAGREE
    if any(value is True for value in known):
        return AGREE
    return UNKNOWN


def _tv_runtime_values(
    intents: Sequence[Dict[str, object]],
    runtime_evidence: Sequence[Dict[str, object]],
) -> List[float]:
    """Normaliza la duracion al numero de episodios contenido en cada archivo."""

    by_source: Dict[str, float] = {}
    for item in runtime_evidence:
        source = str(item.get("source") or "").strip().casefold()
        try:
            runtime = float(item.get("runtime_minutes") or 0)
        except (TypeError, ValueError):
            continue
        if source and runtime > 0:
            by_source[source] = runtime
            by_source[source.rsplit(".", 1)[0]] = runtime
    result: List[float] = []
    for intent in intents:
        if bool(intent.get("is_season_pack")):
            continue
        try:
            runtime = float(intent.get("runtime_minutes") or 0)
        except (TypeError, ValueError):
            runtime = 0
        source = str(intent.get("source") or "").strip().casefold()
        if runtime <= 0 and source:
            runtime = by_source.get(source, by_source.get(source.rsplit(".", 1)[0], 0))
        if runtime <= 0:
            continue
        episode_count = max(1, len(_int_list(intent.get("episodes"))))
        result.append(round(runtime / episode_count, 2))
    return result


def _add(
    candidate: ResolverCandidate,
    family: str,
    state: str,
    value: object,
    *,
    hard: bool = False,
    reason: str = "",
) -> None:
    if any(item.get("family") == family for item in candidate.evidence):
        return
    candidate.evidence.append(
        {"family": family, "state": state, "verdict": state, "value": value}
    )
    if hard and state == DISAGREE:
        candidate.eliminated = True
        if reason and reason not in candidate.elimination_reasons:
            candidate.elimination_reasons.append(reason)


def _year_state(expected: Optional[int], years: Sequence[int], tolerance: int) -> str:
    if expected is None:
        return UNKNOWN
    values = [int(year) for year in years if year]
    if not values:
        return UNKNOWN
    return AGREE if any(abs(expected - year) <= tolerance for year in values) else DISAGREE


def _runtime_values(values: Sequence[Dict[str, object]]) -> List[float]:
    result: List[float] = []
    for item in values:
        try:
            value = float(item.get("runtime_minutes") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            result.append(round(value, 2))
    return result


def _runtime_state(
    observed: Sequence[float],
    expected: Sequence[Optional[int]],
    tolerance_minutes: int,
    tolerance_percent: int,
) -> str:
    targets = [float(value) for value in expected if value and value > 0]
    if not observed or not targets:
        return UNKNOWN
    for value in observed:
        if not any(
            abs(value - target)
            <= max(float(tolerance_minutes), target * float(tolerance_percent) / 100.0)
            for target in targets
        ):
            return DISAGREE
    return AGREE


def _season_exists(candidate: ResolverCandidate, season: int) -> Optional[bool]:
    if season in candidate.season_episode_counts:
        return candidate.season_episode_counts[season] > 0
    if season in candidate.known_episodes:
        return bool(candidate.known_episodes[season])
    if season > 0 and candidate.season_count is not None:
        return season <= candidate.season_count
    return None


def _episodes_exist(
    candidate: ResolverCandidate, season: int, episodes: Sequence[int]
) -> Optional[bool]:
    if season in candidate.known_episodes:
        known = set(candidate.known_episodes[season])
        return bool(known) and all(episode in known for episode in episodes)
    count = candidate.season_episode_counts.get(season)
    if count is not None:
        return count > 0 and all(1 <= episode <= count for episode in episodes)
    return None


def _absolute_episode_exists(
    candidate: ResolverCandidate, absolute_episode: int
) -> Optional[bool]:
    counts = {
        int(season): int(count)
        for season, count in candidate.season_episode_counts.items()
        if int(season) > 0 and int(count) >= 0
    }
    if not counts:
        counts = {
            int(season): len(episodes)
            for season, episodes in candidate.known_episodes.items()
            if int(season) > 0
        }
    if not counts:
        return None
    return 1 <= absolute_episode <= sum(counts[season] for season in sorted(counts))


def _int_list(value: object) -> List[int]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [item for raw in values if (item := as_int(raw)) is not None]


def _adjudication_key(
    candidate: ResolverCandidate, guessed: Dict[str, object]
) -> tuple[object, ...]:
    guessed_year = as_int(guessed.get("year"))
    candidate_years = {year for year in [candidate.year, *candidate.release_years] if year}
    exact_year = guessed_year is not None and guessed_year in candidate_years
    return (
        -int(exact_year),
        -candidate.agree_count,
        candidate.disagree_count,
        -float(candidate.popularity),
        -int(candidate.vote_count),
        -max([candidate.year or 0, *candidate.release_years], default=0),
        candidate.tmdb_id,
    )


def _candidate_summary(candidate: Optional[ResolverCandidate]) -> Optional[Dict[str, object]]:
    if candidate is None:
        return None
    return {
        "tmdb_id": candidate.tmdb_id,
        "media_type": candidate.media_type,
        "title": candidate.title,
        "original_title": candidate.original_title,
        "year": candidate.year,
        "popularity": candidate.popularity,
        "vote_count": candidate.vote_count,
        "agreements": candidate.agree_count,
        "disagreements": candidate.disagree_count,
        "unknown": candidate.unknown_count,
        "eliminated": candidate.eliminated,
        "elimination_reasons": list(candidate.elimination_reasons),
        "evidence": list(candidate.evidence),
    }


__all__ = [
    "ACCEPTED_CONFIDENT",
    "AGREE",
    "DISAGREE",
    "UNKNOWN",
    "Adjudication",
    "adjudicate_candidates",
]

# Nombre exportado para consumidores que comparan el literal sin duplicarlo.
ACCEPTED_CONFIDENT = "ACCEPTED_CONFIDENT"
