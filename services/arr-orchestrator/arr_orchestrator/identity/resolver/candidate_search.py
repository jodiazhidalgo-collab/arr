"""Plan puro de consultas y acceso a fichas TMDb para phased-er-v2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from .candidate_data import candidate_from_payload
from .models import ResolverCandidate
from .text import (
    normalize_title,
    search_query_variants,
    spanish_missing_c_variants,
    strip_query_tail_noise,
    unique,
)


# Alias publicos conservados para consumidores antiguos; representan topes v2.
MAX_TMDB_SEARCHES = 12
MAX_DETAIL_CANDIDATES = 40
SEARCH_PHASES = ("primary", "composite", "alternate")
TITLE_EVIDENCE_ROLES = (
    "configured_primary",
    "primary",
    "derived_primary",
    "composite",
    "alternate",
)

GetPayload = Callable[[str, Dict[str, object]], Dict[str, object]]
Details = Callable[[str, int, Optional[str]], ResolverCandidate]


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


def _build_query_inputs(
    query: str,
    guessed: Dict[str, object],
    variant_rules: Dict[str, object],
) -> Dict[str, List[_QueryInput]]:
    grouped: Dict[str, List[_QueryInput]] = {phase: [] for phase in SEARCH_PHASES}
    seen: set[str] = set()
    primary = str(query or guessed.get("title") or "").strip()
    structured = _structured_title_evidence(guessed)

    def add(value: object, phase: str, source: str) -> None:
        text = str(value or "").strip()
        key = normalize_title(text) or text.casefold()
        if not text or phase not in grouped or key in seen:
            return
        seen.add(key)
        grouped[phase].append(_QueryInput(text, phase, source))

    if structured:
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
            for item in structured:
                if item["role"] == role:
                    add(item["value"], phase, source)
        add(primary, "primary", "primary")
    else:
        for value in guessed.get("_rule_query_aliases") or []:
            add(value, "primary", "configured")
        add(primary, "primary", "primary")
        if bool(variant_rules.get("use_parser_candidates", True)):
            for value in guessed.get("_title_candidates") or []:
                text = str(value or "").strip()
                if normalize_title(text) == normalize_title(primary):
                    continue
                phase = "composite" if _is_composite_title(text, primary) else "alternate"
                add(text, phase, phase)
        if bool(variant_rules.get("use_guessit", True)):
            add(guessed.get("title"), "primary", "legacy")

    expanded: Dict[str, List[_QueryInput]] = {phase: [] for phase in SEARCH_PHASES}
    expanded_seen: set[str] = set()
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


def _expand_query_value(value: str, rules: Dict[str, object]) -> List[str]:
    use_tail = bool(rules.get("use_tail_cleanup", True))
    use_spanish = bool(rules.get("use_spanish_correction", True))
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
    if not normalized_value or not normalized_primary or normalized_value == normalized_primary:
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
    seen: set[tuple[str, Optional[int], str]] = set()
    ordered = _round_robin_query_inputs(grouped)
    for search_year in years:
        for search_language in languages:
            for item in ordered:
                key = (item.value.casefold(), search_year, search_language.casefold())
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
    for _ in range(min(2, len(queues["primary"]))):
        result.append(queues["primary"].pop(0))
    while any(queues[phase] for phase in SEARCH_PHASES):
        for phase in SEARCH_PHASES:
            if queues[phase]:
                result.append(queues[phase].pop(0))
    return result


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
        for item in payload.get(key) or []
        if isinstance(item, dict)
    ]
    return [
        details(media_type, candidate.tmdb_id, language)
        for candidate in candidates[:MAX_DETAIL_CANDIDATES]
    ]


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
            "append_to_response": (
                "translations,alternative_titles,release_dates,external_ids"
                if media_type == "movie"
                else "translations,alternative_titles,external_ids,content_ratings"
            ),
        },
    )
    return candidate_from_payload(media_type, payload)


__all__ = [
    "MAX_DETAIL_CANDIDATES",
    "MAX_TMDB_SEARCHES",
    "fetch_details",
    "find_imdb",
]
