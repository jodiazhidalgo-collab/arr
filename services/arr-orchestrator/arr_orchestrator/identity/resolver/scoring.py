"""Acumulacion de puntos y desglose estructurado de candidatos TMDb."""

from typing import Dict, List, Sequence, Tuple

from guessit import guessit

from .models import ResolverCandidate
from .title_matching import (
    best_title_match,
    matching_rules_for_pairs,
    merge_matching_rules,
    parser_candidate_rules,
    parser_candidate_rules_for_pairs,
    resolve_title_matching,
    scoring_title_values,
    supplemental_title_candidates,
    unique_title_values,
)
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
    title_matching: Dict[str, object] | None = None,
) -> Tuple[float, List[Dict[str, object]]]:
    weights = dict(DEFAULT_SCORING)
    for key, value in dict(settings or {}).items():
        if key in weights and isinstance(value, (int, float)) and not isinstance(value, bool):
            weights[key] = float(value)

    matching_settings = resolve_title_matching(title_matching)
    contributions: List[Tuple[str, float, float]] = []
    applied_matching_rules: List[List[Dict[str, str]]] = []
    candidate.matching_rules = []

    def add(key: str, applied: float) -> bool:
        value = float(applied)
        if abs(value) < 1e-12:
            return False
        contributions.append((key, float(weights[key]), value))
        return True

    if direct_identity:
        add("direct_identity", weights["direct_identity"])
        return _finalize_breakdown(contributions)

    query = str(guessed.get("title") or "")
    title_candidates = supplemental_title_candidates(
        guessed.get("_title_candidates") or [], matching_settings
    )
    query_values = scoring_title_values(query, title_candidates, matching_settings)
    alias_values = unique_title_values(candidate.aliases)
    title_match = best_title_match(query_values, alias_values, matching_settings)
    title_pairs = []
    if title_match.exact:
        if add("title_exact", weights["title_exact"]):
            title_pairs.append(title_match.exact_pair)
    if add(
        "title_similarity_max",
        title_match.ratio * weights["title_similarity_max"],
    ):
        title_pairs.append(title_match.ratio_pair)
    if add(
        "token_overlap_max",
        title_match.token_overlap * weights["token_overlap_max"],
    ):
        title_pairs.append(title_match.token_overlap_pair)
    if title_pairs:
        applied_matching_rules.append(matching_rules_for_pairs(title_pairs))
        applied_matching_rules.append(
            parser_candidate_rules_for_pairs(
                title_pairs,
                title_candidates,
                query,
            )
        )

    spanish_variant_sources: Dict[str, List[str]] = {}
    spanish_variants = []
    for value in query_values:
        for variant in spanish_missing_c_variants(value):
            spanish_variants.append(variant)
            spanish_variant_sources.setdefault(normalize_title(variant), []).append(value)
    spanish_match = best_title_match(
        spanish_variants, alias_values, matching_settings
    )
    if (
        not title_match.exact
        and spanish_match.exact
        and add("spanish_correction", weights["spanish_correction"])
    ):
        spanish_pairs = [spanish_match.exact_pair]
        applied_matching_rules.append(matching_rules_for_pairs(spanish_pairs))
        spanish_sources = (
            spanish_variant_sources.get(
                normalize_title(spanish_match.exact_pair.left_value),
                [],
            )
            if spanish_match.exact_pair is not None
            else []
        )
        if any(
            normalize_title(value) == normalize_title(query)
            for value in spanish_sources
        ):
            spanish_sources = [query]
        applied_matching_rules.append(
            parser_candidate_rules(spanish_sources, title_candidates, query)
        )

    if bool(matching_settings["score_parser_candidates"]):
        parser_match = best_title_match(
            title_candidates,
            alias_values,
            matching_settings,
        )
        parser_pairs = []
        if parser_match.exact:
            if add("parser_exact", weights["parser_exact"]):
                parser_pairs.append(parser_match.exact_pair)
        elif parser_match.ratio >= weights["parser_near_min"]:
            if add("parser_near", weights["parser_near"]):
                parser_pairs.append(parser_match.ratio_pair)
        if parser_pairs:
            applied_matching_rules.append(matching_rules_for_pairs(parser_pairs))
            applied_matching_rules.append(
                parser_candidate_rules_for_pairs(
                    parser_pairs,
                    title_candidates,
                    query,
                )
            )

    configured_aliases = [
        str(value)
        for value in guessed.get("_rule_query_aliases") or []
        if str(value or "").strip()
    ]
    configured_alias_match = best_title_match(
        configured_aliases, alias_values, matching_settings
    )
    if configured_alias_match.exact and add(
        "configured_alias", weights["configured_alias"]
    ):
        applied_matching_rules.append(
            matching_rules_for_pairs([configured_alias_match.exact_pair])
        )

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
    candidate.matching_rules = merge_matching_rules(*applied_matching_rules)
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
