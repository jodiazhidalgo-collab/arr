"""Fachada compatible del parser de nombres ARR.

El motor vive en ``arr_orchestrator.identity.parser_*`` para que reglas,
normalización, detección TV, títulos, decisión y traza se prueben por separado.
"""

import re

from .identity.parser_engine import decide_media, parse_release_name, parse_with_trace, test_parser_title
from .identity.parser_models import MediaDecision, ParsedName
from .identity.parser_rules import (
    DEFAULT_PARSER_RULES,
    factory_parser_rules,
    regex_items,
    resolve_parser_rules,
)


KNOWN_EXTENSIONS = set(DEFAULT_PARSER_RULES["extensions"])
SITE_WORDS = tuple(DEFAULT_PARSER_RULES["site_words"])
TECH_TOKENS_RE = re.compile(
    rf"(?i)\b(?:{regex_items(DEFAULT_PARSER_RULES['technical_tokens'])})\b"
)
YEAR_RE = re.compile(str(DEFAULT_PARSER_RULES["year"]["pattern"]))


__all__ = [
    "DEFAULT_PARSER_RULES",
    "KNOWN_EXTENSIONS",
    "MediaDecision",
    "ParsedName",
    "SITE_WORDS",
    "TECH_TOKENS_RE",
    "YEAR_RE",
    "decide_media",
    "factory_parser_rules",
    "parse_release_name",
    "parse_with_trace",
    "resolve_parser_rules",
    "test_parser_title",
]
