"""Contrato persistente de los tres ambitos de identidad v2.

Los documentos guardados son deliberadamente parciales: ``common`` contiene
el parser y el resolver compartido; ``movies`` y ``tv`` contienen unicamente
su bloque especializado. Solo la composicion produce unas reglas completas
aptas para ejecutar el resolver.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Dict

from .defaults import IDENTITY_PROFILES, factory_identity_rules
from .validation import IdentityRulesValidationError, normalize_identity_rules


def scope_identity_rules(
    rules: object,
    profile: str,
) -> Dict[str, object]:
    """Proyecta reglas completas y validas al documento de ``profile``."""

    normalized_profile = _profile(profile)
    normalized = normalize_identity_rules(rules)
    resolver = normalized["resolver"]
    if normalized_profile == "common":
        shared = {
            key: copy.deepcopy(value)
            for key, value in resolver.items()
            if key not in {"movies", "tv"}
        }
        return {
            "schema_version": normalized["schema_version"],
            "parser": copy.deepcopy(normalized["parser"]),
            "resolver": shared,
        }
    return {
        "schema_version": normalized["schema_version"],
        "resolver": {
            normalized_profile: copy.deepcopy(resolver[normalized_profile]),
        },
    }


def normalize_scoped_identity_rules(
    value: object,
    profile: str,
    *,
    full_defaults: object | None = None,
) -> Dict[str, object]:
    """Valida un documento parcial sin admitir campos de otro ambito."""

    normalized_profile = _profile(profile)
    document = _object(value, f"identity.pipeline.v2.{normalized_profile}")
    expected_root = (
        {"schema_version", "parser", "resolver"}
        if normalized_profile == "common"
        else {"schema_version", "resolver"}
    )
    _exact_keys(document, expected_root, f"identity.pipeline.v2.{normalized_profile}")
    if document.get("schema_version") != 2:
        raise IdentityRulesValidationError(
            f"identity.pipeline.v2.{normalized_profile}.schema_version debe ser 2."
        )
    resolver = _object(
        document.get("resolver"),
        f"identity.pipeline.v2.{normalized_profile}.resolver",
    )

    defaults = normalize_identity_rules(
        copy.deepcopy(full_defaults)
        if full_defaults is not None
        else factory_identity_rules()
    )
    if normalized_profile == "common":
        expected_resolver = set(defaults["resolver"]) - {"movies", "tv"}
        _exact_keys(
            resolver,
            expected_resolver,
            "identity.pipeline.v2.common.resolver",
        )
        complete = copy.deepcopy(defaults)
        complete["parser"] = copy.deepcopy(document["parser"])
        complete_resolver = complete["resolver"]
        for key in expected_resolver:
            complete_resolver[key] = copy.deepcopy(resolver[key])
    else:
        _exact_keys(
            resolver,
            {normalized_profile},
            f"identity.pipeline.v2.{normalized_profile}.resolver",
        )
        complete = copy.deepcopy(defaults)
        complete["resolver"][normalized_profile] = copy.deepcopy(
            resolver[normalized_profile]
        )

    return scope_identity_rules(normalize_identity_rules(complete), normalized_profile)


def compose_identity_scopes(
    common: object,
    category: object,
    profile: str,
    *,
    full_defaults: object | None = None,
) -> Dict[str, object]:
    """Compila Common + Movies/TV y devuelve reglas ejecutables completas."""

    normalized_profile = _profile(profile)
    if normalized_profile == "common":
        raise ValueError("compose_identity_scopes requiere movies o tv")
    defaults = normalize_identity_rules(
        copy.deepcopy(full_defaults)
        if full_defaults is not None
        else factory_identity_rules()
    )
    common_rules = normalize_scoped_identity_rules(
        common,
        "common",
        full_defaults=defaults,
    )
    category_rules = normalize_scoped_identity_rules(
        category,
        normalized_profile,
        full_defaults=defaults,
    )
    complete = copy.deepcopy(defaults)
    complete["parser"] = copy.deepcopy(common_rules["parser"])
    for key, block in common_rules["resolver"].items():
        complete["resolver"][key] = copy.deepcopy(block)
    complete["resolver"][normalized_profile] = copy.deepcopy(
        category_rules["resolver"][normalized_profile]
    )
    return normalize_identity_rules(complete)


def identity_scope_fingerprint(value: object, profile: str) -> str:
    """Huella canonica de un scope persistido (no de la politica efectiva)."""

    normalized = normalize_scoped_identity_rules(value, profile)
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _profile(value: object) -> str:
    profile = str(value or "").strip().lower()
    if profile not in IDENTITY_PROFILES:
        raise ValueError("profile debe ser common, movies o tv")
    return profile


def _object(value: object, path: str) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise IdentityRulesValidationError(f"{path} debe ser un objeto.")
    return value


def _exact_keys(value: Dict[str, object], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details = []
    if missing:
        details.append(f"faltan {', '.join(missing)}")
    if extra:
        details.append(f"sobran {', '.join(extra)}")
    raise IdentityRulesValidationError(f"{path}: {'; '.join(details)}.")


__all__ = [
    "compose_identity_scopes",
    "identity_scope_fingerprint",
    "normalize_scoped_identity_rules",
    "scope_identity_rules",
]
