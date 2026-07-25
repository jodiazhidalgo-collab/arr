"""Fachada de validacion del contrato versionado de identidad."""

from __future__ import annotations

from typing import Dict

from ..defaults import IDENTITY_SCHEMA_VERSION
from .common import IdentityRulesValidationError, exact_keys, expect_object
from .parser import normalize_parser
from .resolver import normalize_resolver


def normalize_identity_rules(value: object) -> Dict[str, object]:
    """Valida el contrato completo, rechaza extras y devuelve forma canonica."""

    rules = expect_object(value, "rules")
    exact_keys(rules, {"schema_version", "parser", "resolver"}, "rules")
    version = rules.get("schema_version")
    if isinstance(version, bool) or version != IDENTITY_SCHEMA_VERSION:
        raise IdentityRulesValidationError(
            f"rules.schema_version debe ser {IDENTITY_SCHEMA_VERSION}."
        )
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "parser": normalize_parser(rules.get("parser")),
        "resolver": normalize_resolver(rules.get("resolver")),
    }


__all__ = ["IdentityRulesValidationError", "normalize_identity_rules"]
