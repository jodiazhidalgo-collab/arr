"""Construcción de consultas TMDb, búsqueda progresiva y enriquecimiento."""

from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .candidate_data import candidate_from_payload, merge_search_payload
from .models import ResolverCandidate, ResolverUnavailable
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

GetPayload = Callable[[str, Dict[str, object]], Dict[str, object]]
Details = Callable[[str, int, Optional[str]], ResolverCandidate]
Ranker = Callable[
    [Sequence[ResolverCandidate], Dict[str, object], Sequence[str], bool],
    List[ResolverCandidate],
]


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
    max_searches = max(1, int(limits.get("max_searches", MAX_TMDB_SEARCHES)))
    result_limit = max(1, int(limits.get("results_per_search", 10)))
    detail_limit = max(1, int(limits.get("detail_candidates", MAX_DETAIL_CANDIDATES)))
    initial_limit = max(1, int(limits.get("initial_candidates", 2)))
    early_stop_score = max(
        float(acceptance.get("early_stop_score", 75)),
        float(acceptance.get("min_score", 75)),
    )
    early_stop_margin = max(
        float(acceptance.get("early_stop_margin", 12)),
        float(acceptance.get("min_margin", 12)),
    )
    year = as_int(guessed.get("year"))
    title_candidates = [str(value) for value in guessed.get("_title_candidates") or []]
    guessit_title = str(guessed.get("title") or "")
    configured_aliases = [
        str(value)
        for value in guessed.get("_rule_query_aliases") or []
        if str(value or "").strip()
    ]
    query_inputs = [*configured_aliases, query]
    if bool(variant_rules.get("use_parser_candidates", True)):
        query_inputs.extend(title_candidates)
    if bool(variant_rules.get("use_guessit", True)):
        query_inputs.append(guessit_title)
    if bool(variant_rules.get("use_tail_cleanup", True)) and bool(
        variant_rules.get("use_spanish_correction", True)
    ):
        queries = search_query_variants(query_inputs)
    else:
        queries = unique(query_inputs)
        expanded: List[str] = []
        for value in queries:
            expanded.append(value)
            if bool(variant_rules.get("use_tail_cleanup", True)):
                expanded.append(strip_query_tail_noise(value))
            if bool(variant_rules.get("use_spanish_correction", True)):
                expanded.extend(spanish_missing_c_variants(value))
        queries = unique(expanded)

    searches: List[Tuple[str, Optional[int], str]] = []
    for search_query in queries:
        search_years: List[Optional[int]] = []
        if bool(variant_rules.get("with_year", True)) and year is not None:
            search_years.append(year)
        if bool(variant_rules.get("without_year", True)):
            search_years.append(None)
        # Sin año extraído, "con año" degrada de forma útil a una consulta
        # normal; evita que esa combinación deje el resolver sin búsquedas.
        if not search_years and bool(variant_rules.get("with_year", True)):
            search_years.append(None)
        languages = [language]
        if use_fallback and language.casefold() != fallback_language.casefold():
            languages.append(fallback_language)
        for search_language in languages:
            for search_year in search_years:
                item = (search_query, search_year, search_language)
                if item not in searches:
                    searches.append(item)
    searches = searches[:max_searches]

    raw: Dict[int, Dict[str, object]] = {}
    search_count = 0
    for search_query, search_year, search_language in searches:
        if search_count >= max_searches:
            break
        endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
        params: Dict[str, object] = {"query": search_query, "language": search_language}
        if media_type == "movie":
            params["region"] = region
            if search_year:
                params["year"] = search_year
        elif search_year:
            params["first_air_date_year"] = search_year
        payload = get_payload(endpoint, params)
        search_count += 1
        for item in list(payload.get("results") or [])[:result_limit]:
            candidate_id = as_int(item.get("id"))
            if candidate_id:
                raw[candidate_id] = merge_search_payload(
                    media_type,
                    raw.get(candidate_id),
                    dict(item),
                )
        if raw:
            ranked = ranker(
                [candidate_from_payload(media_type, item) for item in raw.values()],
                guessed,
                [],
                False,
            )
            margin = ranked[0].score - (ranked[1].score if len(ranked) > 1 else 0)
            top = ranked[0]
            has_required_movie_year = media_type != "movie" or year is None or top.year == year
            require_exact_year = bool(
                acceptance.get("early_stop_require_exact_movie_year", True)
            )
            if (
                top.score >= early_stop_score
                and margin >= early_stop_margin
                and (has_required_movie_year or not require_exact_year)
            ):
                break

    initial = [candidate_from_payload(media_type, item) for item in raw.values()]
    initial = ranker(initial, guessed, [], False)
    oldest_exact_selection = _oldest_exact_title_search_selection(
        media_type,
        initial,
        guessed,
        acceptance,
        policy.get("original_language_preference"),
    )
    if selection_trace is not None:
        selection_trace["oldest_exact_title_search"] = dict(oldest_exact_selection)
    selected = list(initial[: min(initial_limit, detail_limit)])
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
            if len(selected) >= detail_limit:
                selected[-1] = exact_year
            else:
                selected.append(exact_year)
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

    enriched: List[ResolverCandidate] = []
    for candidate in selected[:detail_limit]:
        try:
            detailed = details(media_type, candidate.tmdb_id, language)
            if not detailed.original_language:
                detailed.original_language = candidate.original_language
            detailed.aliases = unique(
                [
                    detailed.title,
                    detailed.original_title,
                    *detailed.aliases,
                    candidate.title,
                    candidate.original_title,
                    *candidate.aliases,
                ]
            )
            enriched.append(detailed)
        except ResolverUnavailable:
            if not enriched:
                enriched.append(candidate)
            break
    return enriched or initial


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
