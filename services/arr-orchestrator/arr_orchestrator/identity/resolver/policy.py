"""Construccion de la politica efectiva del resolver para cada categoria."""

import copy
import hashlib
import json
from typing import Dict

from .scoring import DEFAULT_SCORING


DEFAULT_POLICY: Dict[str, object] = {
    "language": "es-ES",
    "region": "ES",
    "fallback_language": "en-US",
    "use_fallback_language": True,
    "original_language_preference": {"enabled": True, "language": "en"},
    "query_aliases": [],
    "forced_matches": [],
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
    "query_variants": {
        "with_year": True,
        "without_year": True,
        "use_parser_candidates": True,
        "use_guessit": True,
        "use_tail_cleanup": True,
        "use_spanish_correction": True,
    },
    "search_limits": {
        "max_searches": 8,
        "results_per_search": 10,
        "detail_candidates": 3,
        "initial_candidates": 2,
        "include_exact_year_candidate": True,
    },
    "scoring": dict(DEFAULT_SCORING),
    "acceptance": {
        "min_score": 75,
        "min_margin": 12,
        "early_stop_score": 75,
        "early_stop_margin": 12,
        "early_stop_require_exact_movie_year": True,
        "direct_ids_bypass": True,
        "forced_bypass": True,
        "prefer_oldest_exact_title_without_year": False,
    },
    "forced_validation": {
        "min_title_similarity": 0.92,
        "require_year": True,
    },
    "http": {"timeout_ms": 2500, "total_budget_ms": 5000},
    "cache": {
        "enabled": True,
        "read_enabled": True,
        "write_enabled": True,
        "ttl_seconds": 30 * 24 * 3600,
    },
    "output_validation": {"require_title_alias": True, "year_tolerance": 1},
    "parser": {},
}


def effective_policy(
    snapshot: Dict[str, object] | None,
    category: str,
    *,
    default_language: str = "es-ES",
    default_region: str = "ES",
    default_http_timeout_ms: int = 2500,
    default_total_budget_ms: int = 5000,
) -> Dict[str, object]:
    """Devuelve una copia completa y compatible con snapshots antiguos."""

    policy = copy.deepcopy(DEFAULT_POLICY)
    policy["language"] = default_language or "es-ES"
    policy["region"] = default_region or "ES"
    policy["http"] = {
        "timeout_ms": int(default_http_timeout_ms),
        "total_budget_ms": int(default_total_budget_ms),
    }
    document = snapshot if isinstance(snapshot, dict) else {}
    parser = document.get("parser")
    if isinstance(parser, dict):
        policy["parser"] = copy.deepcopy(parser)

    resolver = document.get("resolver")
    if isinstance(resolver, dict):
        locales = resolver.get("locales")
        if isinstance(locales, dict):
            category_locale = locales.get(category)
            if isinstance(category_locale, dict):
                policy["language"] = str(category_locale.get("language") or policy["language"])
                if category == "movies":
                    policy["region"] = str(category_locale.get("region") or policy["region"])
            policy["fallback_language"] = str(
                locales.get("fallback_language") or policy["fallback_language"]
            )
            fallback_toggle = locales.get("use_fallback_language", locales.get("use_fallback"))
            if isinstance(fallback_toggle, bool):
                policy["use_fallback_language"] = fallback_toggle

        aliases = resolver.get("aliases")
        if isinstance(aliases, dict) and isinstance(aliases.get(category), list):
            policy["query_aliases"] = copy.deepcopy(aliases[category])
        forced = resolver.get("forced_matches")
        if isinstance(forced, dict) and isinstance(forced.get(category), list):
            policy["forced_matches"] = copy.deepcopy(forced[category])
        for key in (
            "evidence",
            "original_language_preference",
            "guess_selection",
            "query_variants",
            "search_limits",
            "scoring",
            "acceptance",
            "forced_validation",
            "http",
            "cache",
            "output_validation",
        ):
            value = resolver.get(key)
            if isinstance(value, dict):
                policy[key] = _merge_dict(dict(policy.get(key) or {}), value)
    else:
        # Snapshots historicos filebot.rules y llamadas directas de pruebas.
        legacy = document.get(category)
        if not isinstance(legacy, dict) and isinstance(document.get("categories"), dict):
            legacy = document["categories"].get(category)  # type: ignore[index]
        if not isinstance(legacy, dict) and any(
            key in document for key in ("language", "region", "query_aliases", "forced_matches")
        ):
            legacy = document
        if isinstance(legacy, dict):
            policy["language"] = str(legacy.get("language") or policy["language"])
            if category == "movies":
                policy["region"] = str(legacy.get("region") or policy["region"])
            if isinstance(legacy.get("query_aliases"), list):
                policy["query_aliases"] = copy.deepcopy(legacy["query_aliases"])
            if isinstance(legacy.get("forced_matches"), list):
                policy["forced_matches"] = copy.deepcopy(legacy["forced_matches"])

    fingerprint_payload = copy.deepcopy(policy)
    serialized = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    policy["fingerprint"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return policy


def _merge_dict(base: Dict[str, object], override: Dict[str, object]) -> Dict[str, object]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(dict(merged[key]), value)  # type: ignore[arg-type]
        else:
            merged[key] = copy.deepcopy(value)
    return merged
