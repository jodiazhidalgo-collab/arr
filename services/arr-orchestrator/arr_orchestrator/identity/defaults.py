"""Valores seguros y versionados del pipeline de identidad ARR.

Los valores reproducen el comportamiento que tenia el parser/resolver antes de
ser configurables. La fabrica devuelve siempre una copia independiente para que
un formulario o un trabajo no pueda mutar los defaults del proceso.
"""

from __future__ import annotations

import copy
import re
from typing import Dict

from .parser_rules import factory_parser_rules
from .resolver_defaults import DEFAULT_SERIES_CANDIDATES, DEFAULT_TITLE_MATCHING


IDENTITY_SETTING_KEY = "identity.pipeline"
IDENTITY_RULES_PATH = f"settings/{IDENTITY_SETTING_KEY}"
IDENTITY_PROFILES = ("common", "movies", "tv")
IDENTITY_PROFILE_SETTING_KEYS = {
    profile: f"{IDENTITY_SETTING_KEY}.{profile}" for profile in IDENTITY_PROFILES
}
IDENTITY_SCHEMA_VERSION = 1
IDENTITY_HISTORY_LIMIT = 12


def identity_profile_setting_key(profile: str) -> str:
    """Devuelve la clave persistida de un perfil valido."""

    normalized = str(profile or "").strip().lower()
    try:
        return IDENTITY_PROFILE_SETTING_KEYS[normalized]
    except KeyError as error:
        raise ValueError("profile debe ser common, movies o tv") from error

_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2})?$")
_REGION_RE = re.compile(r"^[A-Za-z]{2}$")


_IDENTITY_RULES_TEMPLATE: Dict[str, object] = {
    "schema_version": IDENTITY_SCHEMA_VERSION,
    "resolver": {
        "locales": {
            "movies": {"language": "es-ES", "region": "ES"},
            "tv": {"language": "es-ES"},
            "fallback_language": "en-US",
            "use_fallback": True,
        },
        "original_language_preference": {
            "enabled": True,
            "language": "en",
        },
        "aliases": {"movies": [], "tv": []},
        "forced_matches": {"movies": [], "tv": []},
        "evidence": {
            "use_job_name": True,
            "use_folder_name": True,
            "use_media_files": True,
            "max_media_files": 20,
            "sort_largest_first": True,
        },
        "guess_selection": {
            "base": 100,
            "index_penalty": 1,
            "year_bonus": 20,
            "season_bonus": 15,
            "parser_high_bonus": 10,
        },
        "series_candidates": copy.deepcopy(DEFAULT_SERIES_CANDIDATES),
        "query_variants": {
            "with_year": True,
            "without_year": True,
            "use_parser_candidates": True,
            "use_guessit": True,
            "use_tail_cleanup": True,
            "use_spanish_correction": True,
        },
        "title_matching": copy.deepcopy(DEFAULT_TITLE_MATCHING),
        "search_limits": {
            "max_searches": 8,
            "results_per_search": 10,
            "detail_candidates": 3,
            "initial_candidates": 2,
            "include_exact_year_candidate": True,
        },
        "scoring": {
            "direct_identity": 200,
            "title_exact": 35,
            "title_similarity_max": 20,
            "token_overlap_max": 5,
            "spanish_correction": 20,
            "parser_exact": 20,
            "parser_near": 12,
            "parser_near_min": 0.86,
            "configured_alias": 30,
            "year_exact": 20,
            "year_near": 8,
            "year_tolerance": 1,
            "year_contradiction": -25,
            "missing_movie_year": -18,
            "category": 10,
            "origin_evidence": 15,
            "season_valid": 20,
            "season_invalid": -100,
        },
        "acceptance": {
            "min_score": 75,
            "min_margin": 12,
            "early_stop_score": 75,
            "early_stop_margin": 12,
            "early_stop_require_exact_movie_year": True,
            "direct_ids_bypass": True,
            "forced_bypass": True,
            "prefer_oldest_exact_title_without_year": True,
        },
        "forced_validation": {
            "min_title_similarity": 0.92,
            "require_year": True,
        },
        "http": {"timeout_ms": 2500, "total_budget_ms": 5000},
        "retry": {
            "base_seconds": 60,
            "multiplier": 2,
            "max_exponent": 3,
            "max_seconds": 300,
        },
        "cache": {
            "enabled": True,
            "ttl_seconds": 30 * 24 * 3600,
            "read_enabled": True,
            "write_enabled": True,
        },
        "output_validation": {"require_title_alias": True, "year_tolerance": 1},
    },
}


def _language(value: str) -> str:
    text = str(value or "").strip()
    if not _LANGUAGE_RE.fullmatch(text):
        raise ValueError("default_language no es valido")
    parts = text.split("-", 1)
    return parts[0].lower() + (f"-{parts[1].upper()}" if len(parts) == 2 else "")


def _region(value: str) -> str:
    text = str(value or "").strip()
    if not _REGION_RE.fullmatch(text):
        raise ValueError("default_region no es valido")
    return text.upper()


def factory_identity_rules(
    default_language: str = "es-ES",
    default_region: str = "ES",
    resolver_http_timeout_ms: int = 2500,
    resolver_total_budget_ms: int = 5000,
    resolver_retry_seconds: int = 60,
) -> Dict[str, object]:
    """Crea los defaults completos adaptados a la configuracion de runtime."""

    rules = copy.deepcopy(_IDENTITY_RULES_TEMPLATE)
    # El parser modular es la unica fuente de verdad. Esta asignacion evita que
    # los defaults persistidos se separen de los que usa parse_release_name.
    rules["parser"] = factory_parser_rules()
    language = _language(default_language)
    region = _region(default_region)
    locales = rules["resolver"]["locales"]  # type: ignore[index]
    locales["movies"]["language"] = language  # type: ignore[index]
    locales["movies"]["region"] = region  # type: ignore[index]
    locales["tv"]["language"] = language  # type: ignore[index]
    timeout_ms = _runtime_integer(
        resolver_http_timeout_ms,
        "resolver_http_timeout_ms",
        100,
        60_000,
    )
    total_budget_ms = _runtime_integer(
        resolver_total_budget_ms,
        "resolver_total_budget_ms",
        100,
        300_000,
    )
    if total_budget_ms < timeout_ms:
        raise ValueError(
            "resolver_total_budget_ms no puede ser menor que "
            "resolver_http_timeout_ms"
        )
    retry_seconds = _runtime_integer(
        resolver_retry_seconds,
        "resolver_retry_seconds",
        1,
        86_400,
    )
    resolver = rules["resolver"]  # type: ignore[index]
    resolver["http"]["timeout_ms"] = timeout_ms  # type: ignore[index]
    resolver["http"]["total_budget_ms"] = total_budget_ms  # type: ignore[index]
    resolver["retry"]["base_seconds"] = retry_seconds  # type: ignore[index]
    resolver["retry"]["max_seconds"] = max(  # type: ignore[index]
        int(resolver["retry"]["max_seconds"]),  # type: ignore[index]
        retry_seconds,
    )
    return rules


def _runtime_integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} debe ser un entero")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} debe estar entre {minimum} y {maximum}")
    return value


DEFAULT_IDENTITY_RULES = factory_identity_rules()
