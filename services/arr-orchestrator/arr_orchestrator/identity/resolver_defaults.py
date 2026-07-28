"""Valores únicos del contrato configurable del resolver ARR."""

from typing import Dict


DEFAULT_SERIES_CANDIDATES: Dict[str, object] = {
    "title_before_episode_marker": True,
    "min_title_words": 2,
}

DEFAULT_TITLE_MATCHING: Dict[str, object] = {
    "score_parser_candidates": True,
    "roman_arabic_equivalence": True,
    "allow_omitted_part_number": True,
    "omitted_part_min_words": 3,
    "supplemental_min_chars": 3,
}
