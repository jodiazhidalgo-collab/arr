"""Construccion de la politica efectiva ``phased-er-v2``."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Dict

from ..defaults import factory_identity_rules


DEFAULT_POLICY: Dict[str, object] = copy.deepcopy(factory_identity_rules()["resolver"])
DEFAULT_POLICY.update(
    {
        "language": "es-ES",
        "region": "ES",
        "fallback_language": "en-US",
        "use_fallback_language": True,
        "query_aliases": [],
        "forced_matches": [],
        "parser": {},
    }
)


def effective_policy(
    snapshot: Dict[str, object] | None,
    category: str,
    *,
    default_language: str = "es-ES",
    default_region: str = "ES",
    default_http_timeout_ms: int = 2500,
    default_total_budget_ms: int = 20_000,
) -> Dict[str, object]:
    """Devuelve politica completa sin reactivar scoring ni margenes v1."""

    policy = copy.deepcopy(DEFAULT_POLICY)
    policy["language"] = default_language or "es-ES"
    policy["region"] = default_region or "ES"
    policy["http"] = {"timeout_ms": int(default_http_timeout_ms)}
    coverage = dict(policy.get("coverage") or {})
    coverage["total_budget_ms"] = int(default_total_budget_ms)
    policy["coverage"] = coverage
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
                policy["language"] = str(
                    category_locale.get("language") or policy["language"]
                )
                if category == "movies":
                    policy["region"] = str(
                        category_locale.get("region") or policy["region"]
                    )
            policy["fallback_language"] = str(
                locales.get("fallback_language") or policy["fallback_language"]
            )
            fallback = locales.get("use_fallback", locales.get("use_fallback_language"))
            if isinstance(fallback, bool):
                policy["use_fallback_language"] = fallback
        aliases = resolver.get("aliases")
        if isinstance(aliases, dict) and isinstance(aliases.get(category), list):
            policy["query_aliases"] = copy.deepcopy(aliases[category])
        forced = resolver.get("forced_matches")
        if isinstance(forced, dict) and isinstance(forced.get(category), list):
            policy["forced_matches"] = copy.deepcopy(forced[category])
        for key in (
            "algorithm",
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
        ):
            value = resolver.get(key)
            if isinstance(value, dict) and isinstance(policy.get(key), dict):
                policy[key] = _merge_dict(dict(policy[key]), value)  # type: ignore[arg-type]
            elif key == "algorithm" and isinstance(value, str):
                policy[key] = value
    else:
        # Llamadas historicas directas: solo locale, alias y match forzado son
        # valores reutilizables; los pesos v1 se descartan deliberadamente.
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

    serialized = json.dumps(
        policy,
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
        if key not in merged:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(dict(merged[key]), value)  # type: ignore[arg-type]
        else:
            merged[key] = copy.deepcopy(value)
    return merged
