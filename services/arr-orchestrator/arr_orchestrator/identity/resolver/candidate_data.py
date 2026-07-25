"""Conversión, fusión y puntuación de candidatos TMDb."""

from typing import Dict, Optional, Sequence

from .models import ResolverCandidate
from .scoring import score_candidate
from .text import as_int, date_year, unique


def merge_search_payload(
    media_type: str,
    existing: Optional[Dict[str, object]],
    incoming: Dict[str, object],
) -> Dict[str, object]:
    title_key = "title" if media_type == "movie" else "name"
    original_key = "original_title" if media_type == "movie" else "original_name"
    date_key = "release_date" if media_type == "movie" else "first_air_date"
    merged = dict(existing or incoming)
    aliases = []
    for payload in (existing or {}, incoming):
        aliases.extend(
            [
                str(payload.get(title_key) or ""),
                str(payload.get(original_key) or ""),
                *(str(item) for item in payload.get("_search_aliases") or []),
            ]
        )
    merged["_search_aliases"] = unique(aliases)
    for key in (title_key, original_key, date_key, "number_of_seasons"):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    return merged


def candidate_from_payload(
    media_type: str, payload: Dict[str, object]
) -> ResolverCandidate:
    title_key = "title" if media_type == "movie" else "name"
    original_key = "original_title" if media_type == "movie" else "original_name"
    date_key = "release_date" if media_type == "movie" else "first_air_date"
    aliases = [str(payload.get(title_key) or ""), str(payload.get(original_key) or "")]
    alternatives = payload.get("alternative_titles") or {}
    alternative_items = alternatives.get("titles") or alternatives.get("results") or []
    aliases.extend(str(item.get("title") or "") for item in alternative_items)
    translations = (payload.get("translations") or {}).get("translations") or []
    for item in translations:
        data = item.get("data") or {}
        aliases.extend(str(data.get(key) or "") for key in ("title", "name") if data.get(key))
    aliases.extend(str(value) for value in payload.get("_search_aliases") or [])
    return ResolverCandidate(
        tmdb_id=int(payload["id"]),
        media_type=media_type,
        title=str(payload.get(title_key) or payload.get(original_key) or ""),
        original_title=str(payload.get(original_key) or payload.get(title_key) or ""),
        year=date_year(payload.get(date_key)),
        aliases=unique(value for value in aliases if value),
        season_count=as_int(payload.get("number_of_seasons")),
    )


def rank_candidates(
    candidates: Sequence[ResolverCandidate],
    guessed: Dict[str, object],
    evidence: Sequence[str],
    direct_identity: bool,
    scoring: object,
) -> list[ResolverCandidate]:
    settings = scoring if isinstance(scoring, dict) else None
    for candidate in candidates:
        candidate.score, candidate.reasons = score_candidate(
            candidate, guessed, evidence, direct_identity, settings
        )
    return sorted(candidates, key=lambda item: item.score, reverse=True)
