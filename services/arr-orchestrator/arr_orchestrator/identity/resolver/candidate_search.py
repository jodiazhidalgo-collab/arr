"""Construcción de consultas TMDb, búsqueda progresiva y enriquecimiento."""

from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .candidate_data import candidate_from_payload, merge_search_payload
from .models import ResolverCandidate, ResolverUnavailable
from .title_matching import resolved_title_evidence
from .text import (
    as_int,
    normalize_title,
    search_query_variants,
    spanish_missing_c_variants,
    strip_query_tail_noise,
    unique,
)


MAX_TMDB_SEARCHES = 8
MAX_DETAIL_CANDIDATES = 3
SCORE_TIE_EPSILON = 1e-9
PROVENANCE_TRACE_LIMIT = 24
SEARCH_PHASES = ("primary", "composite", "alternate")
SEARCH_SOURCES = frozenset(
    {"primary", "configured", "composite", "alternate", "legacy"}
)
STRONG_SEARCH_SOURCES = frozenset(
    {"primary", "configured", "composite"}
)
TITLE_EVIDENCE_ROLES = (
    "configured_primary",
    "primary",
    "derived_primary",
    "composite",
    "alternate",
)

GetPayload = Callable[[str, Dict[str, object]], Dict[str, object]]
Details = Callable[[str, int, Optional[str]], ResolverCandidate]
Ranker = Callable[
    [Sequence[ResolverCandidate], Dict[str, object], Sequence[str], bool],
    List[ResolverCandidate],
]


@dataclass(frozen=True)
class _QueryInput:
    value: str
    phase: str
    source: str


@dataclass(frozen=True)
class _SearchPlanItem:
    query: str
    year: Optional[int]
    language: str
    phase: str
    source: str


def search_candidates(
    media_type: str,
    query: str,
    guessed: Dict[str, object],
    language: str,
    region: str,
    policy: Dict[str, object],
    get_payload: GetPayload,
    details: Details,
    ranker: Ranker,
    selection_trace: Optional[Dict[str, object]] = None,
) -> List[ResolverCandidate]:
    variant_rules = (
        policy.get("query_variants")
        if isinstance(policy.get("query_variants"), dict)
        else {}
    )
    limits = policy.get("search_limits") if isinstance(policy.get("search_limits"), dict) else {}
    acceptance = policy.get("acceptance") if isinstance(policy.get("acceptance"), dict) else {}
    fallback_language = str(policy.get("fallback_language") or "en-US")
    use_fallback = bool(policy.get("use_fallback_language", True))
    max_searches = min(
        MAX_TMDB_SEARCHES,
        max(1, int(limits.get("max_searches", MAX_TMDB_SEARCHES))),
    )
    result_limit = max(1, int(limits.get("results_per_search", 10)))
    detail_limit = min(
        MAX_DETAIL_CANDIDATES,
        max(1, int(limits.get("detail_candidates", MAX_DETAIL_CANDIDATES))),
    )
    initial_limit = min(
        detail_limit,
        max(1, int(limits.get("initial_candidates", 2))),
    )
    early_stop_score = max(
        float(acceptance.get("early_stop_score", 75)),
        float(acceptance.get("min_score", 75)),
    )
    early_stop_margin = max(
        float(acceptance.get("early_stop_margin", 12)),
        float(acceptance.get("min_margin", 12)),
    )
    year = as_int(guessed.get("year"))
    search_guess = dict(guessed)
    search_guess["_title_evidence"] = resolved_title_evidence(
        guessed,
        policy.get("title_matching"),
    )
    query_inputs = _build_query_inputs(query, search_guess, variant_rules)
    searches = _build_search_plan(
        query_inputs,
        year,
        language,
        fallback_language,
        use_fallback,
        variant_rules,
    )

    raw: Dict[int, Dict[str, object]] = {}
    search_count = 0
    phase_calls = {phase: 0 for phase in SEARCH_PHASES}
    early_stop_phase: Optional[str] = None
    early_stop_reason: Optional[str] = None
    detail_cache: Dict[int, ResolverCandidate] = {}
    detail_attempted_ids: set[int] = set()
    detail_requests = 0
    detail_incomplete = False
    early_detail_attempted = False
    early_detail_reused = False
    atomic_title_group_ids = _atomic_title_group_ids(search_guess)

    def ranked_with_cached_details() -> List[ResolverCandidate]:
        candidates = []
        for item in raw.values():
            candidate = candidate_from_payload(media_type, item)
            cached = detail_cache.get(candidate.tmdb_id)
            candidates.append(
                _merge_detailed_candidate(cached, candidate)
                if cached is not None
                else candidate
            )
        return ranker(candidates, guessed, [], False)

    def safe_stop(ranked: Sequence[ResolverCandidate]) -> Optional[str]:
        if not ranked:
            return None
        top = ranked[0]
        margin = top.score - (ranked[1].score if len(ranked) > 1 else 0)
        has_required_movie_year = (
            media_type != "movie" or year is None or top.year == year
        )
        require_exact_year = bool(
            acceptance.get("early_stop_require_exact_movie_year", True)
        )
        reason = _early_stop_reason(
            top,
            raw.get(top.tmdb_id),
            search_guess,
        )
        if (
            top.score >= early_stop_score
            and margin >= early_stop_margin
            and (has_required_movie_year or not require_exact_year)
        ):
            return reason
        return None

    for search in searches:
        if search_count >= max_searches:
            break
        endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
        params: Dict[str, object] = {
            "query": search.query,
            "language": search.language,
        }
        if media_type == "movie":
            params["region"] = region
            if search.year:
                params["year"] = search.year
        elif search.year:
            params["first_air_date_year"] = search.year
        payload = get_payload(endpoint, params)
        search_count += 1
        phase_calls[search.phase] += 1
        for item in list(payload.get("results") or [])[:result_limit]:
            candidate_id = as_int(item.get("id"))
            if candidate_id:
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
        if raw:
            ranked = ranked_with_cached_details()
            margin = ranked[0].score - (ranked[1].score if len(ranked) > 1 else 0)
            top = ranked[0]
            safe_early_stop_reason = safe_stop(ranked)
            if safe_early_stop_reason is not None:
                early_stop_phase = search.phase
                early_stop_reason = safe_early_stop_reason
                break

            cross_candidates = [
                candidate
                for candidate in ranked
                if _has_exact_alternate_in_groups(
                    candidate,
                    atomic_title_group_ids,
                )
            ]
            probe_candidate = cross_candidates[0] if cross_candidates else None
            cross_margin = (
                probe_candidate.score - cross_candidates[1].score
                if probe_candidate is not None and len(cross_candidates) > 1
                else float("inf")
            )
            should_probe_detail = bool(
                search_count == 1
                and atomic_title_group_ids
                and probe_candidate is not None
                and detail_requests < detail_limit
                and probe_candidate.tmdb_id not in detail_attempted_ids
                and search.source in {"primary", "configured", "composite"}
                and cross_margin >= early_stop_margin
            )
            if should_probe_detail:
                early_detail_attempted = True
                detail_attempted_ids.add(probe_candidate.tmdb_id)
                detail_requests += 1
                try:
                    detailed = details(media_type, probe_candidate.tmdb_id, language)
                    detail_cache[probe_candidate.tmdb_id] = _merge_detailed_candidate(
                        detailed,
                        probe_candidate,
                    )
                    ranked = ranked_with_cached_details()
                    safe_early_stop_reason = safe_stop(ranked)
                    if safe_early_stop_reason is not None:
                        early_stop_phase = search.phase
                        early_stop_reason = safe_early_stop_reason
                        break
                except ResolverUnavailable:
                    detail_incomplete = True

    initial = ranked_with_cached_details() if raw else []
    oldest_exact_selection = _oldest_exact_title_search_selection(
        media_type,
        initial,
        guessed,
        acceptance,
        policy.get("original_language_preference"),
    )
    if selection_trace is not None:
        selection_trace["oldest_exact_title_search"] = dict(oldest_exact_selection)
    strong_candidates = [
        candidate
        for candidate in initial
        if _candidate_sources(candidate, raw) & STRONG_SEARCH_SOURCES
    ]
    selected = list(initial[:initial_limit])
    protected_ids = {
        candidate.tmdb_id
        for candidate in selected
        if _candidate_sources(candidate, raw) & STRONG_SEARCH_SOURCES
    }
    represented_sources: set[str] = set()
    for candidate in selected:
        represented_sources.update(
            _candidate_sources(candidate, raw) & STRONG_SEARCH_SOURCES
        )
    # El tercer hueco sigue siendo reserva. Solo se usa si aporta una fuente
    # fuerte nueva; si un alternate ocupa sitio mientras queda evidencia fuerte,
    # se sustituye sin aumentar las fichas normales de un titulo simple.
    for candidate in strong_candidates:
        if any(item.tmdb_id == candidate.tmdb_id for item in selected):
            protected_ids.add(candidate.tmdb_id)
            continue
        sources = _candidate_sources(candidate, raw) & STRONG_SEARCH_SOURCES
        adds_source = bool(sources - represented_sources)
        alternate_index = next(
            (
                index
                for index in range(len(selected) - 1, -1, -1)
                if not (
                    _candidate_sources(selected[index], raw)
                    & STRONG_SEARCH_SOURCES
                )
            ),
            None,
        )
        if len(selected) < detail_limit and adds_source:
            selected.append(candidate)
        elif alternate_index is not None:
            selected[alternate_index] = candidate
        else:
            continue
        protected_ids.add(candidate.tmdb_id)
        represented_sources.update(sources)
    if (
        media_type == "movie"
        and year is not None
        and bool(limits.get("include_exact_year_candidate", True))
    ):
        exact_year = next(
            (
                candidate
                for candidate in initial
                if candidate.year == year
                and all(candidate.tmdb_id != item.tmdb_id for item in selected)
            ),
            None,
        )
        if exact_year is not None:
            exact_is_strong = bool(
                _candidate_sources(exact_year, raw) & STRONG_SEARCH_SOURCES
            )
            _reserve_candidate(
                selected,
                exact_year,
                detail_limit,
                protected_ids,
                protect=exact_is_strong,
                replace_protected=exact_is_strong,
            )
    oldest_exact_id = as_int(oldest_exact_selection.get("tmdb_id"))
    oldest_not_selected = (
        oldest_exact_selection.get("eligible") is True
        and oldest_exact_id is not None
        and all(candidate.tmdb_id != oldest_exact_id for candidate in selected)
    )
    if oldest_not_selected:
        oldest_exact = next(
            (candidate for candidate in initial if candidate.tmdb_id == oldest_exact_id),
            None,
        )
        if oldest_exact is not None and len(selected) < detail_limit:
            # Solo usa un hueco libre: nunca expulsa un candidato no exacto ni
            # altera el conjunto que necesita la preferencia de idioma.
            selected.append(oldest_exact)
        elif oldest_exact is not None:
            oldest_is_strong = bool(
                _candidate_sources(oldest_exact, raw) & STRONG_SEARCH_SOURCES
            )
            tied_ids = {
                int(value)
                for value in oldest_exact_selection.get("tied_tmdb_ids") or []
            }
            preferred_ids = {
                int(value)
                for value in oldest_exact_selection.get("preferred_tmdb_ids") or []
            }
            for index in range(len(selected) - 1, -1, -1):
                current = selected[index]
                if current.tmdb_id in protected_ids and not oldest_is_strong:
                    continue
                if current.tmdb_id not in tied_ids:
                    continue
                proposed = [*selected[:index], oldest_exact, *selected[index + 1 :]]
                preferred_after = sum(
                    candidate.tmdb_id in preferred_ids for candidate in proposed
                )
                if preferred_after == 1:
                    continue
                # Sustituye empate exacto por empate exacto: conserva cualquier
                # candidato no exacto y al menos otro testigo de la ambiguedad.
                if sum(candidate.tmdb_id in tied_ids for candidate in proposed) >= 2:
                    selected[index] = oldest_exact
                    break
    for candidate in initial:
        if len(selected) >= detail_limit:
            break
        if all(candidate.tmdb_id != item.tmdb_id for item in selected):
            selected.append(candidate)

    # La reserva decide pertenencia, no presentación: conserva ranking global.
    ranked_position = {
        candidate.tmdb_id: index for index, candidate in enumerate(initial)
    }
    selected.sort(key=lambda candidate: ranked_position[candidate.tmdb_id])

    if selection_trace is not None:
        selected_ids = {candidate.tmdb_id for candidate in selected[:detail_limit]}
        provenance, omitted = _candidate_provenance(initial, raw, selected_ids)
        selection_trace["candidate_provenance"] = provenance
        if omitted:
            selection_trace["candidate_provenance_omitted"] = omitted
        selection_trace["raw_exact_candidate_counts"] = _raw_exact_candidate_counts(raw)
        policy_resolved_ids = (
            {
                int(value)
                for value in oldest_exact_selection.get("tied_tmdb_ids") or []
            }
            if oldest_exact_selection.get("eligible") is True
            else set()
        )
        uncertain_ids = _selection_uncertain_ids(
            initial,
            selected_ids,
            raw,
            acceptance,
            policy_resolved_ids,
        )
        selection_trace["selection_uncertain"] = bool(uncertain_ids) or detail_incomplete
        selection_trace["selection_uncertainty_alternate_only"] = bool(
            uncertain_ids
        ) and not detail_incomplete and all(
            _candidate_sources_by_id(candidate_id, raw) == {"alternate"}
            for candidate_id in uncertain_ids
        )
        selection_trace["search_strategy"] = {
            "mode": "phased_round_robin",
            "max_searches": max_searches,
            "executed_searches": search_count,
            "planned_searches": min(len(searches), max_searches),
            "phase_calls": phase_calls,
            "early_stop_phase": early_stop_phase,
            "early_stop_reason": early_stop_reason,
            "detail_limit": detail_limit,
            "early_detail_attempted": early_detail_attempted,
            "early_detail_reused": early_detail_reused,
            "detail_requests": detail_requests,
        }
        if detail_incomplete:
            selection_trace["search_strategy"]["detail_incomplete"] = True

    enriched: List[ResolverCandidate] = []
    for candidate in selected[:detail_limit]:
        cached = detail_cache.get(candidate.tmdb_id)
        if cached is not None:
            early_detail_reused = True
            enriched.append(_merge_detailed_candidate(cached, candidate))
            continue
        if candidate.tmdb_id in detail_attempted_ids or detail_requests >= detail_limit:
            detail_incomplete = True
            if not enriched:
                enriched.append(candidate)
            break
        detail_attempted_ids.add(candidate.tmdb_id)
        detail_requests += 1
        try:
            detailed = details(media_type, candidate.tmdb_id, language)
            enriched.append(_merge_detailed_candidate(detailed, candidate))
        except ResolverUnavailable:
            detail_incomplete = True
            if not enriched:
                enriched.append(candidate)
            break
    if selection_trace is not None:
        strategy = selection_trace.get("search_strategy")
        if isinstance(strategy, dict):
            strategy["early_detail_reused"] = early_detail_reused
            strategy["detail_requests"] = detail_requests
            if detail_incomplete:
                strategy["detail_incomplete"] = True
        if detail_incomplete:
            selection_trace["selection_uncertain"] = True
            selection_trace["selection_uncertainty_alternate_only"] = False
    return enriched or initial


def _build_query_inputs(
    query: str,
    guessed: Dict[str, object],
    variant_rules: Dict[str, object],
) -> Dict[str, List[_QueryInput]]:
    grouped: Dict[str, List[_QueryInput]] = {phase: [] for phase in SEARCH_PHASES}
    seen = set()
    primary = str(query or guessed.get("title") or "").strip()
    structured_evidence = _structured_title_evidence(guessed)

    def add(value: object, phase: str, source: str) -> None:
        text = str(value or "").strip()
        key = normalize_title(text) or text.casefold()
        if not text or phase not in grouped or key in seen:
            return
        seen.add(key)
        grouped[phase].append(_QueryInput(text, phase, source))

    if structured_evidence:
        # El rol del parser es canónico. El alias configurado va primero porque
        # es una decisión humana explícita, pero comparte la fase primaria.
        for role in TITLE_EVIDENCE_ROLES:
            if role in {"composite", "alternate"} and not bool(
                variant_rules.get("use_parser_candidates", True)
            ):
                continue
            phase = (
                "primary"
                if role in {"primary", "derived_primary", "configured_primary"}
                else role
            )
            source = "configured" if role == "configured_primary" else phase
            for item in structured_evidence:
                if item["role"] == role:
                    add(item["value"], phase, source)
        add(primary, "primary", "primary")
    else:
        # Compatibilidad con snapshots previos: la lista plana solo se usa si
        # todavía no existe evidencia estructurada.
        for value in guessed.get("_rule_query_aliases") or []:
            add(value, "primary", "configured")
        add(primary, "primary", "primary")
        if bool(variant_rules.get("use_parser_candidates", True)):
            for value in guessed.get("_title_candidates") or []:
                text = str(value or "").strip()
                if normalize_title(text) == normalize_title(primary):
                    continue
                phase = (
                    "composite" if _is_composite_title(text, primary) else "alternate"
                )
                add(text, phase, phase)
        if bool(variant_rules.get("use_guessit", True)):
            add(guessed.get("title"), "primary", "legacy")

    expanded: Dict[str, List[_QueryInput]] = {phase: [] for phase in SEARCH_PHASES}
    expanded_seen = set()
    for phase in SEARCH_PHASES:
        for item in grouped[phase]:
            for value in _expand_query_value(item.value, variant_rules):
                key = value.casefold()
                if key in expanded_seen:
                    continue
                expanded_seen.add(key)
                expanded[phase].append(_QueryInput(value, phase, item.source))
    return expanded


def _structured_title_evidence(
    guessed: Mapping[str, object],
) -> List[Dict[str, str]]:
    supplied = guessed.get("_title_evidence")
    if not isinstance(supplied, Sequence) or isinstance(supplied, (str, bytes)):
        return []
    result: List[Dict[str, str]] = []
    for item in supplied:
        if isinstance(item, Mapping):
            value = item.get("value")
            role = item.get("role")
            group_id = item.get("group_id")
        else:
            value = getattr(item, "value", None)
            role = getattr(item, "role", None)
            group_id = getattr(item, "group_id", None)
        text = str(value or "").strip()
        role_text = str(role or "").strip()
        if text and role_text in TITLE_EVIDENCE_ROLES:
            result.append(
                {
                    "value": text,
                    "role": role_text,
                    "group_id": str(group_id or "legacy:0"),
                }
            )
    return result


def _atomic_title_group_ids(guessed: Mapping[str, object]) -> set[str]:
    grouped: Dict[str, set[str]] = {}
    for item in _structured_title_evidence(guessed):
        grouped.setdefault(item["group_id"], set()).add(item["role"])
    return {
        group_id
        for group_id, roles in grouped.items()
        if bool(roles & {"primary", "derived_primary"}) and "alternate" in roles
    }


def _has_exact_alternate_in_groups(
    candidate: ResolverCandidate,
    group_ids: set[str],
) -> bool:
    return any(
        str(item.get("role") or "") == "alternate"
        and bool(item.get("identity_exact"))
        and str(item.get("group_id") or "") in group_ids
        for item in candidate.title_matches
        if isinstance(item, Mapping)
    )


def _expand_query_value(value: str, variant_rules: Dict[str, object]) -> List[str]:
    use_tail = bool(variant_rules.get("use_tail_cleanup", True))
    use_spanish = bool(variant_rules.get("use_spanish_correction", True))
    if use_tail and use_spanish:
        return search_query_variants([value])
    result = [value]
    if use_tail:
        result.append(strip_query_tail_noise(value))
    if use_spanish:
        result.extend(spanish_missing_c_variants(value))
    return unique(item for item in result if str(item or "").strip())


def _is_composite_title(value: str, primary: str) -> bool:
    normalized_value = normalize_title(value)
    normalized_primary = normalize_title(primary)
    if (
        not normalized_value
        or not normalized_primary
        or normalized_value == normalized_primary
    ):
        return False
    primary_tokens = set(normalized_primary.split())
    value_tokens = set(normalized_value.split())
    return len(value_tokens) > len(primary_tokens) and primary_tokens.issubset(value_tokens)


def _build_search_plan(
    grouped: Dict[str, List[_QueryInput]],
    year: Optional[int],
    language: str,
    fallback_language: str,
    use_fallback: bool,
    variant_rules: Dict[str, object],
) -> List[_SearchPlanItem]:
    years: List[Optional[int]] = []
    if bool(variant_rules.get("with_year", True)) and year is not None:
        years.append(year)
    if bool(variant_rules.get("without_year", True)):
        years.append(None)
    if not years and bool(variant_rules.get("with_year", True)):
        years.append(None)

    languages = [language]
    if use_fallback and language.casefold() != fallback_language.casefold():
        languages.append(fallback_language)

    result: List[_SearchPlanItem] = []
    seen = set()
    ordered_inputs = _round_robin_query_inputs(grouped)
    # Cada contexto prueba primero las formas primarias esenciales y después
    # alterna fases. Así los derivados o variantes no consumen por sí solos el
    # tope global antes de llegar al compuesto y al alternativo.
    for search_year in years:
        for search_language in languages:
            for item in ordered_inputs:
                key = (
                    item.value.casefold(),
                    search_year,
                    search_language.casefold(),
                )
                if key in seen:
                    continue
                seen.add(key)
                result.append(
                    _SearchPlanItem(
                        item.value,
                        search_year,
                        search_language,
                        item.phase,
                        item.source,
                    )
                )
    return result


def _round_robin_query_inputs(
    grouped: Dict[str, List[_QueryInput]],
) -> List[_QueryInput]:
    queues = {phase: list(grouped.get(phase, [])) for phase in SEARCH_PHASES}
    result: List[_QueryInput] = []
    # Alias configurado + principal caben juntos en la primera fase habitual.
    # Con una sola forma, el segundo hueco puede ser su variante más cercana.
    for _ in range(min(2, len(queues["primary"]))):
        result.append(queues["primary"].pop(0))
    while any(queues[phase] for phase in SEARCH_PHASES):
        for phase in SEARCH_PHASES:
            if queues[phase]:
                result.append(queues[phase].pop(0))
    return result


def _early_stop_reason(
    candidate: ResolverCandidate,
    payload: Optional[Dict[str, object]],
    guessed: Dict[str, object],
) -> Optional[str]:
    sources = _metadata_set((payload or {}).get("_search_sources"))
    guessed_year = as_int(guessed.get("year"))
    requested_season = as_int(guessed.get("season"))
    tv_season_unconfirmed = bool(
        candidate.media_type == "tv"
        and requested_season is not None
        and (
            candidate.season_count is None
            or not 0 <= requested_season <= candidate.season_count
        )
    )
    configured_only = bool(
        candidate.title_match_level == "configured" or sources == {"configured"}
    )
    if (
        configured_only
        and (
            (guessed_year is not None and candidate.year != guessed_year)
            or tv_season_unconfirmed
        )
    ):
        return None

    structured = _structured_title_evidence(guessed)
    if structured:
        if any(item["role"] in {"alternate", "composite"} for item in structured):
            # Con dos títulos atómicos, solo el mismo TMDb que confirma ambos
            # puede cortar la búsqueda. Encontrarlo por el alternate no basta.
            if tv_season_unconfirmed:
                return None
            return (
                "all_atomic_titles_confirmed"
                if candidate.title_match_level == "corroborated"
                else None
            )
        if candidate.title_match_level in {"primary", "configured", "corroborated"}:
            return (
                "configured_primary_confirmed"
                if candidate.title_match_level == "configured"
                else "single_primary_confirmed"
            )

    # Fallback legacy: exige coincidencia exacta con el título principal; una
    # coincidencia aislada de un candidato plano alternativo nunca corta.
    primary_values = [guessed.get("title"), guessed.get("_display_title")]
    primary_titles = {
        normalize_title(str(value or ""))
        for value in primary_values
        if str(value or "").strip()
    }
    candidate_titles = {
        normalize_title(value)
        for value in [candidate.title, candidate.original_title, *candidate.aliases]
        if str(value or "").strip()
    }
    if not primary_titles & candidate_titles:
        return None
    flat_alternates = {
        normalize_title(str(value or ""))
        for value in guessed.get("_title_candidates") or []
        if str(value or "").strip()
        and normalize_title(str(value or "")) not in primary_titles
    }
    if flat_alternates:
        return (
            "all_atomic_titles_confirmed"
            if candidate.title_match_level == "corroborated"
            else None
        )
    if sources == {"alternate"}:
        return None
    if candidate.title_match_level in {"none", "primary", "configured", "corroborated"}:
        return "single_primary_confirmed"
    return None


def _has_early_stop_evidence(
    candidate: ResolverCandidate,
    payload: Optional[Dict[str, object]],
    guessed: Dict[str, object],
) -> bool:
    """Compatibilidad interna para pruebas y llamadas anteriores."""

    return _early_stop_reason(candidate, payload, guessed) is not None


def _metadata_set(value: object) -> set[str]:
    items = value if isinstance(value, (list, tuple, set)) else []
    return {str(item or "").strip().lower() for item in items if str(item or "").strip()}


def _candidate_sources(
    candidate: ResolverCandidate,
    raw: Dict[int, Dict[str, object]],
) -> set[str]:
    return _metadata_set(raw.get(candidate.tmdb_id, {}).get("_search_sources"))


def _reserve_candidate(
    selected: List[ResolverCandidate],
    candidate: Optional[ResolverCandidate],
    limit: int,
    protected_ids: set[int],
    *,
    protect: bool,
    replace_protected: bool = False,
) -> bool:
    if candidate is None or limit <= 0:
        return False
    if any(item.tmdb_id == candidate.tmdb_id for item in selected):
        if protect:
            protected_ids.add(candidate.tmdb_id)
        return True
    if len(selected) < limit:
        selected.append(candidate)
    else:
        replace_index = next(
            (
                index
                for index in range(len(selected) - 1, -1, -1)
                if replace_protected or selected[index].tmdb_id not in protected_ids
            ),
            None,
        )
        if replace_index is None:
            return False
        protected_ids.discard(selected[replace_index].tmdb_id)
        selected[replace_index] = candidate
    if protect:
        protected_ids.add(candidate.tmdb_id)
    return True


def _copy_search_provenance(value: object) -> Dict[str, object]:
    payload = value if isinstance(value, Mapping) else {}
    return {
        "sources": sorted(
            _metadata_set(payload.get("sources")) & SEARCH_SOURCES
        ),
        "phases": sorted(_metadata_set(payload.get("phases")) & set(SEARCH_PHASES)),
        "exact_sources": sorted(
            _metadata_set(payload.get("exact_sources"))
            & SEARCH_SOURCES
        ),
        "exact_phases": sorted(
            _metadata_set(payload.get("exact_phases")) & set(SEARCH_PHASES)
        ),
        "hits": _positive_int(payload.get("hits")),
    }


def _merge_detailed_candidate(
    detailed: ResolverCandidate,
    search_candidate: ResolverCandidate,
) -> ResolverCandidate:
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
    detailed.search_provenance = _copy_search_provenance(
        search_candidate.search_provenance
    )
    return detailed


def _candidate_provenance(
    ranked: Sequence[ResolverCandidate],
    raw: Dict[int, Dict[str, object]],
    selected_ids: set[int],
) -> Tuple[List[Dict[str, object]], int]:
    result: List[Dict[str, object]] = []
    for candidate in ranked[:PROVENANCE_TRACE_LIMIT]:
        payload = raw.get(candidate.tmdb_id, {})
        sources = sorted(_metadata_set(payload.get("_search_sources")))
        phases = sorted(_metadata_set(payload.get("_search_phases")))
        exact_sources = sorted(_metadata_set(payload.get("_search_exact_sources")))
        exact_phases = sorted(_metadata_set(payload.get("_search_exact_phases")))
        result.append(
            {
                "tmdb_id": candidate.tmdb_id,
                "sources": sources,
                "phases": phases,
                "exact_sources": exact_sources,
                "exact_phases": exact_phases,
                "hits": _positive_int(payload.get("_search_hit_count")),
                "selected_for_detail": candidate.tmdb_id in selected_ids,
                "alternate_only": bool(sources) and set(sources) == {"alternate"},
            }
        )
    return result, max(0, len(ranked) - len(result))


def _raw_exact_candidate_counts(
    raw: Dict[int, Dict[str, object]],
) -> Dict[str, object]:
    by_phase = {
        phase: sum(
            phase in _metadata_set(payload.get("_search_exact_phases"))
            for payload in raw.values()
        )
        for phase in SEARCH_PHASES
    }
    roles = ("primary", "configured", "composite", "alternate", "legacy")
    by_role = {
        role: sum(
            role in _metadata_set(payload.get("_search_exact_sources"))
            for payload in raw.values()
        )
        for role in roles
    }
    exact_ids = {
        candidate_id
        for candidate_id, payload in raw.items()
        if _metadata_set(payload.get("_search_exact_sources"))
    }
    return {"total": len(exact_ids), "by_phase": by_phase, "by_role": by_role}


def _selection_uncertain(
    ranked: Sequence[ResolverCandidate],
    selected_ids: set[int],
    raw: Dict[int, Dict[str, object]],
    acceptance: Dict[str, object],
    policy_resolved_ids: Optional[set[int]] = None,
) -> bool:
    return bool(
        _selection_uncertain_ids(
            ranked,
            selected_ids,
            raw,
            acceptance,
            policy_resolved_ids,
        )
    )


def _selection_uncertain_ids(
    ranked: Sequence[ResolverCandidate],
    selected_ids: set[int],
    raw: Dict[int, Dict[str, object]],
    acceptance: Dict[str, object],
    policy_resolved_ids: Optional[set[int]] = None,
) -> set[int]:
    if not ranked:
        return set()
    min_score = float(acceptance.get("min_score", 75))
    min_margin = float(acceptance.get("min_margin", 12))
    best_score = ranked[0].score
    ambiguous_ids = {
        candidate.tmdb_id
        for candidate in ranked
        if candidate.score >= min_score and best_score - candidate.score < min_margin
    }
    exact_ids = {
        candidate_id
        for candidate_id, payload in raw.items()
        if _metadata_set(payload.get("_search_exact_sources"))
    }
    resolved_ids = policy_resolved_ids or set()
    return (ambiguous_ids | exact_ids) - selected_ids - resolved_ids


def _candidate_sources_by_id(
    candidate_id: int, raw: Dict[int, Dict[str, object]]
) -> set[str]:
    return _metadata_set(raw.get(candidate_id, {}).get("_search_sources"))


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _oldest_exact_title_search_selection(
    media_type: str,
    ranked: Sequence[ResolverCandidate],
    guessed: Dict[str, object],
    acceptance: Dict[str, object],
    original_language_preference: object,
) -> Dict[str, object]:
    """Identifica un unico minimo seguro usando todo el resultado de busqueda."""

    result: Dict[str, object] = {
        "eligible": False,
        "tmdb_id": None,
        "tied_tmdb_ids": [],
        "preferred_tmdb_ids": [],
    }
    if (
        media_type != "movie"
        or not bool(acceptance.get("prefer_oldest_exact_title_without_year", False))
        or as_int(guessed.get("year")) is not None
        or len(ranked) < 2
    ):
        return result

    min_score = float(acceptance.get("min_score", 75))
    min_margin = float(acceptance.get("min_margin", 12))
    best_score = ranked[0].score
    normal_margin = best_score - ranked[1].score
    if (
        best_score < min_score
        or normal_margin >= min_margin
        or abs(normal_margin) > SCORE_TIE_EPSILON
    ):
        return result

    ambiguous = [
        candidate
        for candidate in ranked
        if candidate.score >= min_score
        and best_score - candidate.score < min_margin
    ]
    query_normalized = normalize_title(str(guessed.get("title") or ""))
    if (
        len(ambiguous) < 2
        or not query_normalized
        or any(
            abs(best_score - candidate.score) > SCORE_TIE_EPSILON
            for candidate in ambiguous
        )
        or any(
            normalize_title(candidate.title) != query_normalized
            for candidate in ambiguous
        )
        or any(candidate.year is None for candidate in ambiguous)
    ):
        return result

    oldest_year = min(
        int(candidate.year) for candidate in ambiguous if candidate.year is not None
    )
    oldest = [candidate for candidate in ambiguous if candidate.year == oldest_year]
    if len(oldest) != 1:
        return result

    preference = (
        original_language_preference
        if isinstance(original_language_preference, dict)
        else {}
    )
    preferred_language = _base_language(str(preference.get("language") or "en"))
    preferred = (
        [
            candidate
            for candidate in ambiguous
            if _base_language(candidate.original_language) == preferred_language
        ]
        if bool(preference.get("enabled", True)) and preferred_language
        else []
    )
    if len(preferred) == 1:
        # La preferencia de idioma tiene prioridad; si su unico candidato no
        # cabe en detalle, no se permite que la regla de año la adelante.
        return result
    return {
        "eligible": True,
        "tmdb_id": oldest[0].tmdb_id,
        "tied_tmdb_ids": [candidate.tmdb_id for candidate in ambiguous],
        "preferred_tmdb_ids": [candidate.tmdb_id for candidate in preferred],
    }


def _base_language(value: str) -> str:
    return str(value or "").strip().split("-", 1)[0].casefold()


def find_imdb(
    media_type: str,
    imdb_id: str,
    language: Optional[str],
    get_payload: GetPayload,
    details: Details,
) -> List[ResolverCandidate]:
    payload = get_payload(f"/find/{imdb_id}", {"external_source": "imdb_id"})
    key = "movie_results" if media_type == "movie" else "tv_results"
    candidates = [
        candidate_from_payload(media_type, dict(item))
        for item in list(payload.get(key) or [])
    ]
    if not candidates:
        return []
    return [details(media_type, candidates[0].tmdb_id, language)]


def fetch_details(
    media_type: str,
    tmdb_id: int,
    language: Optional[str],
    default_language: str,
    get_payload: GetPayload,
) -> ResolverCandidate:
    endpoint = f"/movie/{tmdb_id}" if media_type == "movie" else f"/tv/{tmdb_id}"
    payload = get_payload(
        endpoint,
        {
            "language": str(language or default_language),
            "append_to_response": "translations,alternative_titles",
        },
    )
    return candidate_from_payload(media_type, payload)
