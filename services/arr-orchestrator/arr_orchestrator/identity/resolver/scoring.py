"""Puntuacion pura y desglose estructurado de candidatos TMDb."""

from difflib import SequenceMatcher
from typing import Dict, List, Sequence, Tuple

from guessit import guessit

from .models import ResolverCandidate
from .text import as_int, clean_release_name, normalize_title, spanish_missing_c_variants


DEFAULT_SCORING: Dict[str, float] = {
    "direct_identity": 200.0,
    "title_exact": 35.0,
    "title_similarity_max": 20.0,
    "token_overlap_max": 5.0,
    "spanish_correction": 20.0,
    "parser_exact": 20.0,
    "parser_near": 12.0,
    "parser_near_min": 0.86,
    "configured_alias": 30.0,
    "year_exact": 20.0,
    "year_near": 8.0,
    "year_tolerance": 1.0,
    "year_contradiction": -25.0,
    "missing_movie_year": -18.0,
    "category": 10.0,
    "origin_evidence": 15.0,
    "season_valid": 20.0,
    "season_invalid": -100.0,
}

_ORDINAL_TOKENS = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
    **{str(value): value for value in range(1, 11)},
}


def score_candidate(
    candidate: ResolverCandidate,
    guessed: Dict[str, object],
    evidence: Sequence[str],
    direct_identity: bool,
    settings: Dict[str, object] | None = None,
) -> Tuple[float, List[Dict[str, object]]]:
    weights = dict(DEFAULT_SCORING)
    for key, value in dict(settings or {}).items():
        if key in weights and isinstance(value, (int, float)) and not isinstance(value, bool):
            weights[key] = float(value)

    contributions: List[Tuple[str, float, float]] = []

    def add(key: str, applied: float) -> None:
        value = float(applied)
        if abs(value) < 1e-12:
            return
        contributions.append((key, float(weights[key]), value))

    if direct_identity:
        add("direct_identity", weights["direct_identity"])
        return _finalize_breakdown(contributions)

    query = str(guessed.get("title") or "")
    title_candidates = [
        str(value)
        for value in guessed.get("_title_candidates") or []
        if _is_distinctive_supplemental_title(value)
    ]
    query_values = _unique_values([query, *title_candidates])
    alias_values = _unique_values(candidate.aliases)
    exact, ratio, token_overlap = _best_title_match(query_values, alias_values)
    if exact:
        add("title_exact", weights["title_exact"])
    add("title_similarity_max", ratio * weights["title_similarity_max"])
    add("token_overlap_max", token_overlap * weights["token_overlap_max"])
    spanish_variants = [
        variant
        for value in query_values
        for variant in spanish_missing_c_variants(value)
    ]
    if not exact and _best_title_match(spanish_variants, alias_values)[0]:
        add("spanish_correction", weights["spanish_correction"])

    parser_exact, best_candidate_ratio, _ = _best_title_match(
        title_candidates,
        alias_values,
    )
    if parser_exact:
        add("parser_exact", weights["parser_exact"])
    elif best_candidate_ratio >= weights["parser_near_min"]:
        add("parser_near", weights["parser_near"])

    configured_aliases = [
        str(value)
        for value in guessed.get("_rule_query_aliases") or []
        if str(value or "").strip()
    ]
    if _best_title_match(configured_aliases, alias_values)[0]:
        add("configured_alias", weights["configured_alias"])

    guessed_year = as_int(guessed.get("year"))
    if guessed_year and candidate.year:
        difference = abs(guessed_year - candidate.year)
        if difference == 0:
            add("year_exact", weights["year_exact"])
        elif difference <= max(0, int(weights["year_tolerance"])):
            add("year_near", weights["year_near"])
        else:
            add("year_contradiction", weights["year_contradiction"])
    elif guessed_year and candidate.media_type == "movie":
        add("missing_movie_year", weights["missing_movie_year"])

    add("category", weights["category"])

    aliases = {normalize_title(value) for value in alias_values if value}
    if evidence and any(
        normalize_title(str(dict(guessit(clean_release_name(value))).get("title") or "")) in aliases
        for value in evidence
    ):
        add("origin_evidence", weights["origin_evidence"])

    if candidate.media_type == "tv":
        season = as_int(guessed.get("season"))
        if season is not None and candidate.season_count is not None:
            if 0 <= season <= candidate.season_count:
                add("season_valid", weights["season_valid"])
            else:
                add("season_invalid", weights["season_invalid"])
    return _finalize_breakdown(contributions)


TitleForm = Tuple[str, bool, bool]


def _best_title_match(
    left_values: Sequence[str],
    right_values: Sequence[str],
) -> Tuple[bool, float, float]:
    left_forms = [form for value in left_values for form in _title_forms(value)]
    right_forms = [form for value in right_values for form in _title_forms(value)]
    exact = False
    best_ratio = 0.0
    best_overlap = 0.0
    for left in left_forms:
        for right in right_forms:
            if not _compatible_forms(left, right):
                continue
            exact = exact or left[0] == right[0]
            best_ratio = max(best_ratio, SequenceMatcher(None, left[0], right[0]).ratio())
            left_tokens = set(left[0].split())
            right_tokens = set(right[0].split())
            best_overlap = max(
                best_overlap,
                len(left_tokens & right_tokens)
                / max(1, len(left_tokens | right_tokens)),
            )
    return exact, best_ratio, best_overlap


def _title_forms(value: str) -> List[TitleForm]:
    normalized = normalize_title(value)
    if not normalized:
        return []
    tokens = normalized.split()
    ordinal_values = [
        _ORDINAL_TOKENS[token] for token in tokens if token in _ORDINAL_TOKENS
    ]
    canonical = [
        str(_ORDINAL_TOKENS[token]) if token in _ORDINAL_TOKENS else token
        for token in tokens
    ]
    forms: List[TitleForm] = [(" ".join(canonical), bool(ordinal_values), False)]
    without_ordinals = [
        token for token in canonical if token not in {str(value) for value in ordinal_values}
    ]
    if len(ordinal_values) == 1 and len(without_ordinals) >= 3:
        forms.append((" ".join(without_ordinals), True, True))
    expanded: List[TitleForm] = []
    for text, has_ordinal, omitted_ordinal in forms:
        expanded.append((text, has_ordinal, omitted_ordinal))
        tokens = text.split()
        for index, token in enumerate(tokens):
            if token != "s" or index == 0:
                continue
            joined = [*tokens]
            joined[index - 1 : index + 1] = [f"{tokens[index - 1]}s"]
            expanded.append((" ".join(joined), has_ordinal, omitted_ordinal))
    return list(dict.fromkeys(expanded))


def _compatible_forms(left: TitleForm, right: TitleForm) -> bool:
    if left[2] or right[2]:
        return left[2] != right[2] and left[1] != right[1]
    return True


def _unique_values(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _is_distinctive_supplemental_title(value: object) -> bool:
    normalized = normalize_title(str(value or ""))
    return len(normalized.replace(" ", "")) >= 3


def _finalize_breakdown(
    contributions: Sequence[Tuple[str, float, float]],
) -> Tuple[float, List[Dict[str, object]]]:
    """Redondea el total como antes y reparte los centimos sin descuadrarlo."""

    raw_total = 0.0
    displayed_total = 0.0
    breakdown: List[Dict[str, object]] = []
    for key, configured, raw_applied in contributions:
        raw_total += raw_applied
        next_displayed_total = round(raw_total, 2)
        applied = round(next_displayed_total - displayed_total, 2)
        displayed_total = next_displayed_total
        breakdown.append(
            {
                "key": key,
                "path": f"resolver.scoring.{key}",
                "configured": _clean_number(configured),
                "applied": _clean_number(applied),
            }
        )
    return round(raw_total, 2), breakdown


def _clean_number(value: float) -> int | float:
    numeric = round(float(value), 4)
    if abs(numeric) < 1e-12:
        return 0
    if numeric.is_integer():
        return int(numeric)
    return numeric
