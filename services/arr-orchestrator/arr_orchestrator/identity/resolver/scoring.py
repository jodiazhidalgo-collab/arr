"""Puntuacion pura y explicable de candidatos TMDb."""

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
) -> Tuple[float, List[str]]:
    weights = dict(DEFAULT_SCORING)
    for key, value in dict(settings or {}).items():
        if key in weights and isinstance(value, (int, float)) and not isinstance(value, bool):
            weights[key] = float(value)

    if direct_identity:
        return weights["direct_identity"], ["identificador externo confirmado"]

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
    score = (weights["title_exact"] if exact else 0.0)
    score += ratio * weights["title_similarity_max"]
    score += token_overlap * weights["token_overlap_max"]
    reasons = [f"titulo ratio={ratio:.2f}", f"tokens={token_overlap:.2f}"]
    if exact:
        reasons.append("titulo exacto")
    elif any(normalize_title(value) in aliases for value in spanish_missing_c_variants(query)):
        score += weights["spanish_correction"]
        reasons.append("titulo corregido exacto")

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
        score += weights["parser_exact"]
        reasons.append("alias del parser exacto")
    elif best_candidate_ratio >= weights["parser_near_min"]:
        score += weights["parser_near"]
        reasons.append("alias del parser cercano")

    configured_aliases = {
        normalize_title(str(value))
        for value in guessed.get("_rule_query_aliases") or []
        if str(value or "").strip()
    }
    if configured_aliases.intersection(aliases):
        score += weights["configured_alias"]
        reasons.append("alias configurado exacto")

    guessed_year = as_int(guessed.get("year"))
    if guessed_year and candidate.year:
        difference = abs(guessed_year - candidate.year)
        if difference == 0:
            score += weights["year_exact"]
            reasons.append("ano exacto")
        elif difference <= max(0, int(weights["year_tolerance"])):
            score += weights["year_near"]
            reasons.append("ano +/-1")
        else:
            score += weights["year_contradiction"]
            reasons.append("ano contradictorio")
    elif guessed_year and candidate.media_type == "movie":
        score += weights["missing_movie_year"]
        reasons.append("ano ausente")

    score += weights["category"]
    reasons.append("categoria correcta")

    if evidence and any(
        normalize_title(str(dict(guessit(clean_release_name(value))).get("title") or "")) in aliases
        for value in evidence
    ):
        score += weights["origin_evidence"]
        reasons.append("evidencia de origen")

    if candidate.media_type == "tv":
        season = as_int(guessed.get("season"))
        if season is not None and candidate.season_count is not None:
            if 0 <= season <= candidate.season_count:
                score += weights["season_valid"]
                reasons.append("temporada existente")
            else:
                score += weights["season_invalid"]
                reasons.append("temporada inexistente")
    return round(score, 2), reasons
