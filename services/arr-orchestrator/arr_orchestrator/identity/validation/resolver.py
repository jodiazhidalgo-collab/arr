"""Validacion estricta de busqueda, scoring y seguridad del resolver."""

from __future__ import annotations

import copy
from typing import Dict, Tuple

from ..resolver_defaults import (
    DEFAULT_SERIES_CANDIDATES,
    DEFAULT_SOURCE_TITLE_FALLBACK,
    DEFAULT_TITLE_MATCHING,
)
from .common import (
    IdentityRulesValidationError,
    aliases,
    boolean,
    exact_keys,
    expect_object,
    forced_matches,
    integer,
    language,
    number,
    region,
)


def _booleans(value: object, label: str, keys: Tuple[str, ...]) -> Dict[str, bool]:
    block = expect_object(value, label)
    exact_keys(block, set(keys), label)
    return {key: boolean(block.get(key), f"{label}.{key}") for key in keys}


def _integers(
    value: object,
    label: str,
    bounds: Dict[str, Tuple[int, int]],
) -> Dict[str, int]:
    block = expect_object(value, label)
    exact_keys(block, set(bounds), label)
    return {
        key: integer(block.get(key), f"{label}.{key}", minimum, maximum)
        for key, (minimum, maximum) in bounds.items()
    }


def normalize_resolver(value: object) -> Dict[str, object]:
    resolver = dict(expect_object(value, "rules.resolver"))
    # Los documentos v1 anteriores a estas opciones se completan durante la
    # normalizacion. El resto del contrato continua siendo estricto.
    resolver.setdefault(
        "original_language_preference",
        {"enabled": True, "language": "en"},
    )
    resolver.setdefault(
        "series_candidates",
        copy.deepcopy(DEFAULT_SERIES_CANDIDATES),
    )
    resolver.setdefault(
        "title_matching",
        copy.deepcopy(DEFAULT_TITLE_MATCHING),
    )
    resolver.setdefault(
        "source_title_fallback",
        copy.deepcopy(DEFAULT_SOURCE_TITLE_FALLBACK),
    )
    exact_keys(
        resolver,
        {
            "locales",
            "original_language_preference",
            "aliases",
            "forced_matches",
            "evidence",
            "guess_selection",
            "series_candidates",
            "query_variants",
            "title_matching",
            "source_title_fallback",
            "search_limits",
            "scoring",
            "acceptance",
            "forced_validation",
            "http",
            "retry",
            "cache",
            "output_validation",
        },
        "rules.resolver",
    )

    locales = expect_object(resolver.get("locales"), "rules.resolver.locales")
    exact_keys(
        locales,
        {"movies", "tv", "fallback_language", "use_fallback"},
        "rules.resolver.locales",
    )
    movies_locale = expect_object(
        locales.get("movies"), "rules.resolver.locales.movies"
    )
    tv_locale = expect_object(locales.get("tv"), "rules.resolver.locales.tv")
    exact_keys(
        movies_locale, {"language", "region"}, "rules.resolver.locales.movies"
    )
    exact_keys(tv_locale, {"language"}, "rules.resolver.locales.tv")
    normalized_locales = {
        "movies": {
            "language": language(
                movies_locale.get("language"),
                "rules.resolver.locales.movies.language",
            ),
            "region": region(
                movies_locale.get("region"), "rules.resolver.locales.movies.region"
            ),
        },
        "tv": {
            "language": language(
                tv_locale.get("language"), "rules.resolver.locales.tv.language"
            )
        },
        "fallback_language": language(
            locales.get("fallback_language"),
            "rules.resolver.locales.fallback_language",
        ),
        "use_fallback": boolean(
            locales.get("use_fallback"), "rules.resolver.locales.use_fallback"
        ),
    }

    original_language_preference = expect_object(
        resolver.get("original_language_preference"),
        "rules.resolver.original_language_preference",
    )
    exact_keys(
        original_language_preference,
        {"enabled", "language"},
        "rules.resolver.original_language_preference",
    )
    normalized_original_language_preference = {
        "enabled": boolean(
            original_language_preference.get("enabled"),
            "rules.resolver.original_language_preference.enabled",
        ),
        "language": language(
            original_language_preference.get("language"),
            "rules.resolver.original_language_preference.language",
        ),
    }

    alias_rules = expect_object(resolver.get("aliases"), "rules.resolver.aliases")
    forced_rules = expect_object(
        resolver.get("forced_matches"), "rules.resolver.forced_matches"
    )
    exact_keys(alias_rules, {"movies", "tv"}, "rules.resolver.aliases")
    exact_keys(forced_rules, {"movies", "tv"}, "rules.resolver.forced_matches")
    normalized_aliases = {
        "movies": aliases(
            alias_rules.get("movies"), "rules.resolver.aliases.movies"
        ),
        "tv": aliases(alias_rules.get("tv"), "rules.resolver.aliases.tv"),
    }
    normalized_forced = {
        "movies": forced_matches(
            forced_rules.get("movies"),
            "rules.resolver.forced_matches.movies",
            "movies",
        ),
        "tv": forced_matches(
            forced_rules.get("tv"), "rules.resolver.forced_matches.tv", "tv"
        ),
    }

    evidence = expect_object(resolver.get("evidence"), "rules.resolver.evidence")
    exact_keys(
        evidence,
        {
            "use_job_name",
            "use_folder_name",
            "use_media_files",
            "max_media_files",
            "sort_largest_first",
        },
        "rules.resolver.evidence",
    )
    normalized_evidence = {
        "use_job_name": boolean(
            evidence.get("use_job_name"), "rules.resolver.evidence.use_job_name"
        ),
        "use_folder_name": boolean(
            evidence.get("use_folder_name"), "rules.resolver.evidence.use_folder_name"
        ),
        "use_media_files": boolean(
            evidence.get("use_media_files"), "rules.resolver.evidence.use_media_files"
        ),
        "max_media_files": integer(
            evidence.get("max_media_files"),
            "rules.resolver.evidence.max_media_files",
            0,
            1000,
        ),
        "sort_largest_first": boolean(
            evidence.get("sort_largest_first"),
            "rules.resolver.evidence.sort_largest_first",
        ),
    }
    has_effective_evidence = bool(
        normalized_evidence["use_job_name"]
        or normalized_evidence["use_folder_name"]
        or (
            normalized_evidence["use_media_files"]
            and normalized_evidence["max_media_files"] > 0
        )
    )
    if not has_effective_evidence:
        raise IdentityRulesValidationError(
            "rules.resolver.evidence requiere al menos una fuente activa."
        )

    normalized_guess = _integers(
        resolver.get("guess_selection"),
        "rules.resolver.guess_selection",
        {
            "base": (0, 1000),
            "index_penalty": (0, 100),
            "year_bonus": (-500, 500),
            "season_bonus": (-500, 500),
            "parser_high_bonus": (-500, 500),
        },
    )

    series_candidates = expect_object(
        resolver.get("series_candidates"),
        "rules.resolver.series_candidates",
    )
    exact_keys(
        series_candidates,
        {"title_before_episode_marker", "min_title_words"},
        "rules.resolver.series_candidates",
    )
    normalized_series_candidates = {
        "title_before_episode_marker": boolean(
            series_candidates.get("title_before_episode_marker"),
            "rules.resolver.series_candidates.title_before_episode_marker",
        ),
        "min_title_words": integer(
            series_candidates.get("min_title_words"),
            "rules.resolver.series_candidates.min_title_words",
            1,
            20,
        ),
    }

    query_keys = (
        "with_year",
        "without_year",
        "use_parser_candidates",
        "use_guessit",
        "use_tail_cleanup",
        "use_spanish_correction",
    )
    normalized_queries = _booleans(
        resolver.get("query_variants"),
        "rules.resolver.query_variants",
        query_keys,
    )
    if not normalized_queries["with_year"] and not normalized_queries["without_year"]:
        raise IdentityRulesValidationError(
            "rules.resolver.query_variants requiere buscar con año o sin año."
        )

    title_matching = expect_object(
        resolver.get("title_matching"),
        "rules.resolver.title_matching",
    )
    exact_keys(
        title_matching,
        {
            "score_parser_candidates",
            "roman_arabic_equivalence",
            "allow_omitted_part_number",
            "omitted_part_min_words",
            "supplemental_min_chars",
        },
        "rules.resolver.title_matching",
    )
    normalized_title_matching = {
        "score_parser_candidates": boolean(
            title_matching.get("score_parser_candidates"),
            "rules.resolver.title_matching.score_parser_candidates",
        ),
        "roman_arabic_equivalence": boolean(
            title_matching.get("roman_arabic_equivalence"),
            "rules.resolver.title_matching.roman_arabic_equivalence",
        ),
        "allow_omitted_part_number": boolean(
            title_matching.get("allow_omitted_part_number"),
            "rules.resolver.title_matching.allow_omitted_part_number",
        ),
        "omitted_part_min_words": integer(
            title_matching.get("omitted_part_min_words"),
            "rules.resolver.title_matching.omitted_part_min_words",
            1,
            20,
        ),
        "supplemental_min_chars": integer(
            title_matching.get("supplemental_min_chars"),
            "rules.resolver.title_matching.supplemental_min_chars",
            1,
            100,
        ),
    }

    source_title_fallback = expect_object(
        resolver.get("source_title_fallback"),
        "rules.resolver.source_title_fallback",
    )
    exact_keys(
        source_title_fallback,
        {
            "enabled",
            "movies",
            "tv",
            "score_bonus",
            "min_similarity",
            "require_compatible_year_for_fuzzy",
        },
        "rules.resolver.source_title_fallback",
    )
    normalized_source_title_fallback = {
        "enabled": boolean(
            source_title_fallback.get("enabled"),
            "rules.resolver.source_title_fallback.enabled",
        ),
        "movies": boolean(
            source_title_fallback.get("movies"),
            "rules.resolver.source_title_fallback.movies",
        ),
        "tv": boolean(
            source_title_fallback.get("tv"),
            "rules.resolver.source_title_fallback.tv",
        ),
        "score_bonus": integer(
            source_title_fallback.get("score_bonus"),
            "rules.resolver.source_title_fallback.score_bonus",
            0,
            100,
        ),
        "min_similarity": number(
            source_title_fallback.get("min_similarity"),
            "rules.resolver.source_title_fallback.min_similarity",
            0.5,
            1,
        ),
        "require_compatible_year_for_fuzzy": boolean(
            source_title_fallback.get("require_compatible_year_for_fuzzy"),
            "rules.resolver.source_title_fallback.require_compatible_year_for_fuzzy",
        ),
    }

    limits = expect_object(
        resolver.get("search_limits"), "rules.resolver.search_limits"
    )
    exact_keys(
        limits,
        {
            "max_searches",
            "results_per_search",
            "detail_candidates",
            "initial_candidates",
            "include_exact_year_candidate",
        },
        "rules.resolver.search_limits",
    )
    normalized_limits = {
        "max_searches": integer(
            limits.get("max_searches"),
            "rules.resolver.search_limits.max_searches",
            1,
            32,
        ),
        "results_per_search": integer(
            limits.get("results_per_search"),
            "rules.resolver.search_limits.results_per_search",
            1,
            100,
        ),
        "detail_candidates": integer(
            limits.get("detail_candidates"),
            "rules.resolver.search_limits.detail_candidates",
            1,
            20,
        ),
        "initial_candidates": integer(
            limits.get("initial_candidates"),
            "rules.resolver.search_limits.initial_candidates",
            1,
            20,
        ),
        "include_exact_year_candidate": boolean(
            limits.get("include_exact_year_candidate"),
            "rules.resolver.search_limits.include_exact_year_candidate",
        ),
    }
    if normalized_limits["initial_candidates"] > normalized_limits["detail_candidates"]:
        raise IdentityRulesValidationError(
            "rules.resolver.search_limits.initial_candidates no puede superar detail_candidates."
        )

    scoring = expect_object(resolver.get("scoring"), "rules.resolver.scoring")
    scoring_keys = {
        "direct_identity",
        "title_exact",
        "title_similarity_max",
        "token_overlap_max",
        "spanish_correction",
        "parser_exact",
        "parser_near",
        "parser_near_min",
        "configured_alias",
        "year_exact",
        "year_near",
        "year_tolerance",
        "year_contradiction",
        "missing_movie_year",
        "category",
        "origin_evidence",
        "season_valid",
        "season_invalid",
    }
    exact_keys(scoring, scoring_keys, "rules.resolver.scoring")
    positive_scores = (
        "title_exact",
        "title_similarity_max",
        "token_overlap_max",
        "spanish_correction",
        "parser_exact",
        "parser_near",
        "configured_alias",
        "year_exact",
        "year_near",
        "category",
        "origin_evidence",
        "season_valid",
    )
    normalized_scoring: Dict[str, object] = {
        "direct_identity": integer(
            scoring.get("direct_identity"),
            "rules.resolver.scoring.direct_identity",
            0,
            1000,
        ),
        **{
            key: integer(
                scoring.get(key), f"rules.resolver.scoring.{key}", 0, 500
            )
            for key in positive_scores
        },
        "parser_near_min": number(
            scoring.get("parser_near_min"),
            "rules.resolver.scoring.parser_near_min",
            0,
            1,
        ),
        "year_tolerance": integer(
            scoring.get("year_tolerance"),
            "rules.resolver.scoring.year_tolerance",
            0,
            10,
        ),
        "year_contradiction": integer(
            scoring.get("year_contradiction"),
            "rules.resolver.scoring.year_contradiction",
            -1000,
            0,
        ),
        "missing_movie_year": integer(
            scoring.get("missing_movie_year"),
            "rules.resolver.scoring.missing_movie_year",
            -1000,
            0,
        ),
        "season_invalid": integer(
            scoring.get("season_invalid"),
            "rules.resolver.scoring.season_invalid",
            -1000,
            0,
        ),
    }

    acceptance = dict(
        expect_object(resolver.get("acceptance"), "rules.resolver.acceptance")
    )
    acceptance.setdefault("prefer_oldest_exact_title_without_year", True)
    exact_keys(
        acceptance,
        {
            "min_score",
            "min_margin",
            "early_stop_score",
            "early_stop_margin",
            "early_stop_require_exact_movie_year",
            "direct_ids_bypass",
            "forced_bypass",
            "prefer_oldest_exact_title_without_year",
        },
        "rules.resolver.acceptance",
    )
    normalized_acceptance = {
        "min_score": integer(
            acceptance.get("min_score"),
            "rules.resolver.acceptance.min_score",
            -1000,
            1000,
        ),
        "min_margin": integer(
            acceptance.get("min_margin"),
            "rules.resolver.acceptance.min_margin",
            0,
            1000,
        ),
        "early_stop_score": integer(
            acceptance.get("early_stop_score"),
            "rules.resolver.acceptance.early_stop_score",
            -1000,
            1000,
        ),
        "early_stop_margin": integer(
            acceptance.get("early_stop_margin"),
            "rules.resolver.acceptance.early_stop_margin",
            0,
            1000,
        ),
        "early_stop_require_exact_movie_year": boolean(
            acceptance.get("early_stop_require_exact_movie_year"),
            "rules.resolver.acceptance.early_stop_require_exact_movie_year",
        ),
        "direct_ids_bypass": boolean(
            acceptance.get("direct_ids_bypass"),
            "rules.resolver.acceptance.direct_ids_bypass",
        ),
        "forced_bypass": boolean(
            acceptance.get("forced_bypass"),
            "rules.resolver.acceptance.forced_bypass",
        ),
        "prefer_oldest_exact_title_without_year": boolean(
            acceptance.get("prefer_oldest_exact_title_without_year"),
            "rules.resolver.acceptance.prefer_oldest_exact_title_without_year",
        ),
    }

    forced_validation = expect_object(
        resolver.get("forced_validation"), "rules.resolver.forced_validation"
    )
    exact_keys(
        forced_validation,
        {"min_title_similarity", "require_year"},
        "rules.resolver.forced_validation",
    )
    normalized_forced_validation = {
        "min_title_similarity": number(
            forced_validation.get("min_title_similarity"),
            "rules.resolver.forced_validation.min_title_similarity",
            0,
            1,
        ),
        "require_year": boolean(
            forced_validation.get("require_year"),
            "rules.resolver.forced_validation.require_year",
        ),
    }

    normalized_http = _integers(
        resolver.get("http"),
        "rules.resolver.http",
        {"timeout_ms": (100, 60_000), "total_budget_ms": (100, 300_000)},
    )
    if normalized_http["total_budget_ms"] < normalized_http["timeout_ms"]:
        raise IdentityRulesValidationError(
            "rules.resolver.http.total_budget_ms no puede ser menor que timeout_ms."
        )

    normalized_retry = _integers(
        resolver.get("retry"),
        "rules.resolver.retry",
        {
            "base_seconds": (1, 86_400),
            "multiplier": (1, 10),
            "max_exponent": (0, 16),
            "max_seconds": (1, 604_800),
        },
    )
    if normalized_retry["max_seconds"] < normalized_retry["base_seconds"]:
        raise IdentityRulesValidationError(
            "rules.resolver.retry.max_seconds no puede ser menor que base_seconds."
        )

    cache = expect_object(resolver.get("cache"), "rules.resolver.cache")
    exact_keys(
        cache,
        {"enabled", "ttl_seconds", "read_enabled", "write_enabled"},
        "rules.resolver.cache",
    )
    normalized_cache = {
        "enabled": boolean(cache.get("enabled"), "rules.resolver.cache.enabled"),
        "ttl_seconds": integer(
            cache.get("ttl_seconds"),
            "rules.resolver.cache.ttl_seconds",
            60,
            31_536_000,
        ),
        "read_enabled": boolean(
            cache.get("read_enabled"), "rules.resolver.cache.read_enabled"
        ),
        "write_enabled": boolean(
            cache.get("write_enabled"), "rules.resolver.cache.write_enabled"
        ),
    }

    output = expect_object(
        resolver.get("output_validation"), "rules.resolver.output_validation"
    )
    exact_keys(
        output,
        {"require_title_alias", "year_tolerance"},
        "rules.resolver.output_validation",
    )
    normalized_output = {
        "require_title_alias": boolean(
            output.get("require_title_alias"),
            "rules.resolver.output_validation.require_title_alias",
        ),
        "year_tolerance": integer(
            output.get("year_tolerance"),
            "rules.resolver.output_validation.year_tolerance",
            0,
            10,
        ),
    }

    return {
        "locales": normalized_locales,
        "original_language_preference": normalized_original_language_preference,
        "aliases": normalized_aliases,
        "forced_matches": normalized_forced,
        "evidence": normalized_evidence,
        "guess_selection": normalized_guess,
        "series_candidates": normalized_series_candidates,
        "query_variants": normalized_queries,
        "title_matching": normalized_title_matching,
        "source_title_fallback": normalized_source_title_fallback,
        "search_limits": normalized_limits,
        "scoring": normalized_scoring,
        "acceptance": normalized_acceptance,
        "forced_validation": normalized_forced_validation,
        "http": normalized_http,
        "retry": normalized_retry,
        "cache": normalized_cache,
        "output_validation": normalized_output,
    }
