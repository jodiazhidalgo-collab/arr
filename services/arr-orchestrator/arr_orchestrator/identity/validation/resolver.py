"""Validacion estricta del contrato resolver ``phased-er-v2``."""

from __future__ import annotations

from typing import Dict, Tuple

from .common import (
    IdentityRulesValidationError,
    aliases,
    boolean,
    choice,
    exact_keys,
    expect_object,
    forced_matches,
    integer,
    language,
    region,
    string_list,
)


TIE_BREAKERS = (
    "explicit_year",
    "agreements",
    "disagreements",
    "popularity",
    "vote_count",
    "newest_year",
    "lowest_tmdb_id",
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
    expected = {
        "algorithm",
        "locales",
        "aliases",
        "forced_matches",
        "evidence",
        "query_variants",
        "title_matching",
        "coverage",
        "adjudication",
        "movies",
        "tv",
        "http",
        "retry",
        "cache",
        "output_validation",
    }
    exact_keys(resolver, expected, "rules.resolver")
    algorithm = choice(
        resolver.get("algorithm"),
        "rules.resolver.algorithm",
        ("phased-er-v2",),
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
    exact_keys(movies_locale, {"language", "region"}, "rules.resolver.locales.movies")
    exact_keys(tv_locale, {"language"}, "rules.resolver.locales.tv")
    normalized_locales = {
        "movies": {
            "language": language(
                movies_locale.get("language"),
                "rules.resolver.locales.movies.language",
            ),
            "region": region(
                movies_locale.get("region"),
                "rules.resolver.locales.movies.region",
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

    alias_rules = expect_object(resolver.get("aliases"), "rules.resolver.aliases")
    forced_rules = expect_object(
        resolver.get("forced_matches"), "rules.resolver.forced_matches"
    )
    exact_keys(alias_rules, {"movies", "tv"}, "rules.resolver.aliases")
    exact_keys(forced_rules, {"movies", "tv"}, "rules.resolver.forced_matches")
    normalized_aliases = {
        "movies": aliases(alias_rules.get("movies"), "rules.resolver.aliases.movies"),
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
    evidence_keys = (
        "use_job_name",
        "use_folder_name",
        "use_media_files",
        "sort_largest_first",
    )
    exact_keys(evidence, {*evidence_keys, "max_media_files"}, "rules.resolver.evidence")
    normalized_evidence: Dict[str, object] = {
        **{
            key: boolean(evidence.get(key), f"rules.resolver.evidence.{key}")
            for key in evidence_keys
        },
        "max_media_files": integer(
            evidence.get("max_media_files"),
            "rules.resolver.evidence.max_media_files",
            0,
            1000,
        ),
    }
    if not (
        normalized_evidence["use_job_name"]
        or normalized_evidence["use_folder_name"]
        or (
            normalized_evidence["use_media_files"]
            and int(normalized_evidence["max_media_files"]) > 0
        )
    ):
        raise IdentityRulesValidationError(
            "rules.resolver.evidence requiere al menos una fuente activa."
        )

    query_keys = (
        "with_year",
        "without_year",
        "use_parser_candidates",
        "use_guessit",
        "use_tail_cleanup",
        "use_spanish_correction",
    )
    normalized_queries = _booleans(
        resolver.get("query_variants"), "rules.resolver.query_variants", query_keys
    )
    if not normalized_queries["with_year"] and not normalized_queries["without_year"]:
        raise IdentityRulesValidationError(
            "rules.resolver.query_variants requiere buscar con año o sin año."
        )

    title_matching = expect_object(
        resolver.get("title_matching"), "rules.resolver.title_matching"
    )
    title_keys = {
        "roman_arabic_equivalence",
        "allow_omitted_part_number",
        "omitted_part_min_words",
        "supplemental_min_chars",
    }
    exact_keys(title_matching, title_keys, "rules.resolver.title_matching")
    normalized_title_matching = {
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

    normalized_coverage = _integers(
        resolver.get("coverage"),
        "rules.resolver.coverage",
        {
            "max_searches": (1, 12),
            "max_candidates": (1, 60),
            "batch_size": (1, 8),
            "max_details": (1, 40),
            "total_budget_ms": (100, 300_000),
        },
    )
    if normalized_coverage["batch_size"] > normalized_coverage["max_details"]:
        raise IdentityRulesValidationError(
            "rules.resolver.coverage.batch_size no puede superar max_details."
        )

    adjudication = expect_object(
        resolver.get("adjudication"), "rules.resolver.adjudication"
    )
    exact_keys(
        adjudication,
        {"mode", "tie_breakers"},
        "rules.resolver.adjudication",
    )
    tie_breakers = string_list(
        adjudication.get("tie_breakers"),
        "rules.resolver.adjudication.tie_breakers",
    )
    if tuple(tie_breakers) != TIE_BREAKERS:
        raise IdentityRulesValidationError(
            "rules.resolver.adjudication.tie_breakers debe conservar el orden v2."
        )
    normalized_adjudication = {
        "mode": choice(
            adjudication.get("mode"),
            "rules.resolver.adjudication.mode",
            ("most_probable",),
        ),
        "tie_breakers": tie_breakers,
    }

    movie_rules = expect_object(resolver.get("movies"), "rules.resolver.movies")
    movie_boolean_keys = ("use_release_timeline", "hard_year_conflict")
    movie_integer_bounds = {
        "year_tolerance": (0, 5),
        "runtime_tolerance_minutes": (0, 120),
        "runtime_tolerance_percent": (0, 100),
        "short_runtime_minutes": (1, 180),
        "feature_runtime_minutes": (1, 300),
    }
    exact_keys(
        movie_rules,
        {*movie_boolean_keys, *movie_integer_bounds},
        "rules.resolver.movies",
    )
    normalized_movies = {
        **{
            key: boolean(movie_rules.get(key), f"rules.resolver.movies.{key}")
            for key in movie_boolean_keys
        },
        **{
            key: integer(movie_rules.get(key), f"rules.resolver.movies.{key}", *bounds)
            for key, bounds in movie_integer_bounds.items()
        },
    }
    if normalized_movies["short_runtime_minutes"] >= normalized_movies["feature_runtime_minutes"]:
        raise IdentityRulesValidationError(
            "rules.resolver.movies.short_runtime_minutes debe ser menor que feature_runtime_minutes."
        )

    tv_rules = expect_object(resolver.get("tv"), "rules.resolver.tv")
    tv_boolean_keys = (
        "validate_season",
        "validate_episode",
        "allow_absolute_episode",
        "allow_specials",
        "allow_season_packs",
        "allow_multi_episode",
    )
    tv_integer_bounds = {
        "runtime_tolerance_minutes": (0, 120),
        "runtime_tolerance_percent": (0, 100),
    }
    exact_keys(tv_rules, {*tv_boolean_keys, *tv_integer_bounds}, "rules.resolver.tv")
    normalized_tv = {
        **{
            key: boolean(tv_rules.get(key), f"rules.resolver.tv.{key}")
            for key in tv_boolean_keys
        },
        **{
            key: integer(tv_rules.get(key), f"rules.resolver.tv.{key}", *bounds)
            for key, bounds in tv_integer_bounds.items()
        },
    }

    normalized_http = _integers(
        resolver.get("http"),
        "rules.resolver.http",
        {"timeout_ms": (100, 60_000)},
    )
    if normalized_coverage["total_budget_ms"] < normalized_http["timeout_ms"]:
        raise IdentityRulesValidationError(
            "rules.resolver.coverage.total_budget_ms no puede ser menor que "
            "rules.resolver.http.timeout_ms."
        )
    normalized_retry = _integers(
        resolver.get("retry"),
        "rules.resolver.retry",
        {
            "base_seconds": (1, 86_400),
            "multiplier": (1, 10),
            "max_exponent": (0, 16),
            "max_seconds": (1, 604_800),
            "max_attempts": (1, 10),
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
            cache.get("ttl_seconds"), "rules.resolver.cache.ttl_seconds", 60, 31_536_000
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
    exact_keys(output, {"require_title_alias", "year_tolerance"}, "rules.resolver.output_validation")
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
        "algorithm": algorithm,
        "locales": normalized_locales,
        "aliases": normalized_aliases,
        "forced_matches": normalized_forced,
        "evidence": normalized_evidence,
        "query_variants": normalized_queries,
        "title_matching": normalized_title_matching,
        "coverage": normalized_coverage,
        "adjudication": normalized_adjudication,
        "movies": normalized_movies,
        "tv": normalized_tv,
        "http": normalized_http,
        "retry": normalized_retry,
        "cache": normalized_cache,
        "output_validation": normalized_output,
    }
