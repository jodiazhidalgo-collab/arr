"""Validación segura de reglas que fijan una identidad TMDb."""

from difflib import SequenceMatcher
from typing import Callable, Dict, Optional, Tuple

from .models import ResolutionError, ResolverAmbiguous, ResolverCandidate, ResolverUnavailable
from .text import normalize_title, unique


FORCED_TITLE_SIMILARITY = 0.92


def validate_forced_candidate(
    media_type: str,
    forced_match: Tuple[str, Optional[int], int],
    language: str,
    details: Callable[[str, int, Optional[str]], ResolverCandidate],
    policy: Dict[str, object],
) -> ResolverCandidate:
    rule_title, expected_year, tmdb_id = forced_match
    try:
        candidate = details(media_type, tmdb_id, language)
    except ResolverUnavailable:
        raise
    except ResolutionError as error:
        raise ResolverAmbiguous(
            "La regla forzada apunta a una identidad TMDb no valida",
            {
                "rule_title": rule_title,
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "error": str(error),
            },
        ) from error
    validation = policy.get("forced_validation") if isinstance(policy.get("forced_validation"), dict) else {}
    if candidate.tmdb_id != tmdb_id or candidate.media_type != media_type:
        raise ResolverAmbiguous(
            "La regla forzada no coincide con el tipo o ID consultado",
            {
                "rule_title": rule_title,
                "tmdb_id": tmdb_id,
                "returned_tmdb_id": candidate.tmdb_id,
                "media_type": media_type,
                "returned_media_type": candidate.media_type,
            },
        )
    real_titles = unique([candidate.title, candidate.original_title, *candidate.aliases])
    normalized_rule_title = normalize_title(rule_title)
    normalized_real_titles = [
        normalize_title(value) for value in real_titles if normalize_title(value)
    ]
    best_title_similarity = max(
        (
            SequenceMatcher(None, normalized_rule_title, real_title).ratio()
            for real_title in normalized_real_titles
        ),
        default=0.0,
    )
    min_similarity = float(validation.get("min_title_similarity", FORCED_TITLE_SIMILARITY))
    if normalized_rule_title not in normalized_real_titles and best_title_similarity < min_similarity:
        raise ResolverAmbiguous(
            "La regla forzada no coincide con los titulos reales de TMDb",
            {
                "rule_title": rule_title,
                "tmdb_id": tmdb_id,
                "best_title_similarity": round(best_title_similarity, 3),
            },
        )
    if (
        bool(validation.get("require_year", True))
        and expected_year is not None
        and candidate.year != expected_year
    ):
        raise ResolverAmbiguous(
            "La regla forzada no coincide con el ano real de TMDb",
            {
                "rule_title": rule_title,
                "tmdb_id": tmdb_id,
                "expected_year": expected_year,
                "returned_year": candidate.year,
            },
        )
    return candidate
