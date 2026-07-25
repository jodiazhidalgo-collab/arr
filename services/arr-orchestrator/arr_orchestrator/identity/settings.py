"""Fachada estable de configuracion para consumidores del orquestador."""

from .defaults import factory_identity_rules
from .fingerprint import identity_fingerprint
from .store import IdentitySettingsStore, SettingsDatabase
from .validation import IdentityRulesValidationError, normalize_identity_rules


__all__ = [
    "IdentityRulesValidationError",
    "IdentitySettingsStore",
    "SettingsDatabase",
    "factory_identity_rules",
    "identity_fingerprint",
    "normalize_identity_rules",
]
