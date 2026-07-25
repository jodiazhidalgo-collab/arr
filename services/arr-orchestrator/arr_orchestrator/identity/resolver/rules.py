"""Reglas editables de alias, coincidencias forzadas e IDs externos."""

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .text import as_int, normalize_title, unique


TMDB_ID_PATTERN = re.compile(r"(?:tmdb|themoviedb)[-_. ]?(\d+)", re.IGNORECASE)
IMDB_ID_PATTERN = re.compile(r"\b(tt\d{7,10})\b", re.IGNORECASE)


def parse_query_aliases(value: object) -> List[Tuple[str, str]]:
    items = value if isinstance(value, list) else []
    result: List[Tuple[str, str]] = []
    seen = set()
    for item in items:
        source = ""
        destination = ""
        if isinstance(item, str):
            parts = [part.strip() for part in item.split("|", 1)]
            if len(parts) == 2:
                source, destination = parts
        elif isinstance(item, dict):
            source = str(item.get("source") or item.get("origin") or "").strip()
            destination = str(item.get("destination") or item.get("target") or "").strip()
        key = (normalize_title(source), normalize_title(destination))
        if source and destination and key not in seen:
            seen.add(key)
            result.append((source, destination))
    return result


def parse_forced_matches(value: object) -> List[Tuple[str, Optional[int], int]]:
    items = value if isinstance(value, list) else []
    result: List[Tuple[str, Optional[int], int]] = []
    seen = set()
    for item in items:
        title = ""
        expected_year: Optional[int] = None
        tmdb_id: Optional[int] = None
        if isinstance(item, str):
            parts = [part.strip() for part in item.split("|")]
            if len(parts) == 2:
                title, raw_tmdb_id = parts
                tmdb_id = as_int(raw_tmdb_id)
            elif len(parts) == 3:
                title, raw_year, raw_tmdb_id = parts
                expected_year = as_int(raw_year)
                tmdb_id = as_int(raw_tmdb_id)
        elif isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            expected_year = as_int(item.get("year"))
            tmdb_id = as_int(item.get("tmdb_id"))
        if not title or not tmdb_id or tmdb_id <= 0:
            continue
        if expected_year is not None and not 1870 <= expected_year <= 2200:
            continue
        key = (normalize_title(title), expected_year, tmdb_id)
        if key not in seen:
            seen.add(key)
            result.append((title, expected_year, tmdb_id))
    return result


def apply_query_aliases(
    guessed: Dict[str, object], query_aliases: object
) -> Dict[str, object]:
    aliases = list(query_aliases) if isinstance(query_aliases, list) else []
    if not aliases:
        return guessed
    updated = dict(guessed)
    title_candidates = [
        str(value).strip()
        for value in guessed.get("_title_candidates") or []
        if str(value or "").strip()
    ]
    matchable = {
        normalize_title(value)
        for value in [
            str(guessed.get("title") or ""),
            str(guessed.get("_display_title") or ""),
            *title_candidates,
        ]
        if value
    }
    applied: List[str] = []
    for item in aliases:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        source, destination = str(item[0]).strip(), str(item[1]).strip()
        if source and destination and normalize_title(source) in matchable:
            title_candidates.append(destination)
            applied.append(destination)
    updated["_title_candidates"] = unique(title_candidates)
    if applied:
        updated["_rule_query_aliases"] = unique(applied)
    return updated


def matching_forced_rule(
    guessed: Dict[str, object], forced_matches: object
) -> Optional[Tuple[str, Optional[int], int]]:
    rules = list(forced_matches) if isinstance(forced_matches, list) else []
    titles = {
        normalize_title(str(value))
        for value in [
            guessed.get("title"),
            guessed.get("_display_title"),
            *(guessed.get("_title_candidates") or []),
        ]
        if str(value or "").strip()
    }
    guessed_year = as_int(guessed.get("year"))
    generic_match: Optional[Tuple[str, Optional[int], int]] = None
    for item in rules:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        title = str(item[0]).strip()
        expected_year = as_int(item[1])
        tmdb_id = as_int(item[2])
        if not title or not tmdb_id or normalize_title(title) not in titles:
            continue
        if expected_year is not None:
            if expected_year == guessed_year:
                return title, expected_year, tmdb_id
            continue
        if generic_match is None:
            generic_match = (title, expected_year, tmdb_id)
    return generic_match


def first_match(pattern: re.Pattern[str], values: Sequence[str]) -> Optional[str]:
    for value in values:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return None
