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
    query_norm = normalize_title(query)
    aliases = [normalize_title(value) for value in candidate.aliases if value]
    ratios = [SequenceMatcher(None, query_norm, alias).ratio() for alias in aliases]
    ratio = max(ratios or [0.0])
    exact = query_norm in aliases
    tokens = set(query_norm.split())
    token_overlap = max(
        (
            len(tokens & set(alias.split())) / max(1, len(tokens | set(alias.split())))
            for alias in aliases
        ),
        default=0.0,
    )
    if exact:
        add("title_exact", weights["title_exact"])
    add("title_similarity_max", ratio * weights["title_similarity_max"])
    add("token_overlap_max", token_overlap * weights["token_overlap_max"])
    if not exact and any(
        normalize_title(value) in aliases for value in spanish_missing_c_variants(query)
    ):
        add("spanish_correction", weights["spanish_correction"])

    title_candidates = [
        normalize_title(str(value))
        for value in guessed.get("_title_candidates") or []
        if str(value or "").strip()
    ]
    candidate_ratios = [
        SequenceMatcher(None, candidate_title, alias).ratio()
        for candidate_title in title_candidates
        for alias in aliases
    ]
    best_candidate_ratio = max(candidate_ratios or [0.0])
    if any(candidate_title in aliases for candidate_title in title_candidates):
        add("parser_exact", weights["parser_exact"])
    elif best_candidate_ratio >= weights["parser_near_min"]:
        add("parser_near", weights["parser_near"])

    configured_aliases = {
        normalize_title(str(value))
        for value in guessed.get("_rule_query_aliases") or []
        if str(value or "").strip()
    }
    if configured_aliases.intersection(aliases):
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
