"""Construccion y orden de candidatos de titulo para el resolver."""

import re
from typing import Dict, List, Sequence

from ...name_parser import parse_release_name
from ..parser_rules import parser_pattern, resolve_parser_rules
from ..resolver_defaults import DEFAULT_SERIES_CANDIDATES
from .text import clean_release_name, normalize_title, unique

TV_IDENTITY_PATTERNS = (
    "series_sxe",
    "series_x",
    "explicit_season",
    "season_pack",
    "chapter",
    "episode_word",
)


def ordered_title_candidates(
    parser_candidates: Sequence[str],
    fallback_title: str,
    derived_candidates: Sequence[str] = (),
) -> List[str]:
    """Conserva primero el orden del parser y despues anade los derivados."""

    primary = list(parser_candidates) or [fallback_title]
    return unique([*primary, *derived_candidates])


def series_title_candidates(
    evidence: Sequence[str], policy: Dict[str, object]
) -> List[str]:
    """Extrae el titulo anterior al primer marcador de episodio configurado."""

    settings = (
        policy.get("series_candidates")
        if isinstance(policy.get("series_candidates"), dict)
        else {}
    )
    default_enabled = bool(DEFAULT_SERIES_CANDIDATES["title_before_episode_marker"])
    if not bool(settings.get("title_before_episode_marker", default_enabled)):
        return []
    default_minimum = int(DEFAULT_SERIES_CANDIDATES["min_title_words"])
    minimum_words = _positive_int(
        settings.get("min_title_words", default_minimum), default_minimum
    )

    rules = resolve_parser_rules(rules=policy.get("parser"))
    result: List[str] = []
    for value in evidence:
        text = str(value or "").strip()
        matches = []
        for name in TV_IDENTITY_PATTERNS:
            pattern = parser_pattern(rules, name)
            if pattern and (match := re.search(pattern, text, flags=re.IGNORECASE)):
                matches.append(match)
        if not matches:
            continue
        first = min(matches, key=lambda match: match.start())
        prefix = text[: first.start()].strip(" ._-")
        if not prefix:
            continue
        parsed = parse_release_name(prefix, "tv", rules=rules)
        for candidate in (parsed.display_title, clean_release_name(prefix)):
            candidate = candidate.strip()
            if len(normalize_title(candidate).split()) >= minimum_words:
                result.append(candidate)
    return unique(result)


def _positive_int(value: object, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default
