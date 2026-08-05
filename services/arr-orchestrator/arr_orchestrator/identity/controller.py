"""Fachada de aplicacion del pipeline de identidad.

Engine y la API solo delegan aqui; persistencia, parser, resolver y pruebas no
se mezclan con el bucle principal del orquestador.
"""

import copy
import json
import logging
import re
from typing import Dict, Iterable, Optional

from ..name_parser import MediaDecision, decide_media, test_parser_title
from ..name_resolver import NameResolver
from .defaults import (
    IDENTITY_PROFILES,
    IDENTITY_PROFILE_SETTING_KEYS,
    factory_identity_rules,
    identity_profile_setting_key,
)
from .scopes import (
    compose_identity_scopes,
    identity_scope_fingerprint,
    normalize_scoped_identity_rules,
    scope_identity_rules,
)
from .settings import (
    IdentityRulesValidationError,
    IdentitySettingsStore,
    identity_fingerprint,
    normalize_identity_rules,
)


class IdentityController:
    def __init__(
        self,
        config: object,
        database: object,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.log = logger or logging.getLogger("arr-orchestrator.identity")
        default_language = str(getattr(config, "resolver_language", "es-ES"))
        default_region = str(getattr(config, "resolver_region", "ES"))
        resolver_http_timeout_ms = int(
            getattr(config, "resolver_http_timeout_ms", 2500)
        )
        resolver_total_budget_ms = int(
            getattr(config, "resolver_total_budget_ms", 20_000)
        )
        resolver_retry_seconds = int(
            getattr(config, "resolver_retry_seconds", 60)
        )
        defaults = factory_identity_rules(
            default_language,
            default_region,
            resolver_http_timeout_ms,
            resolver_total_budget_ms,
            resolver_retry_seconds,
        )
        _seed_profile_settings(database, defaults)
        self.database = database
        self.stores = {
            profile: IdentitySettingsStore(
                database,
                default_language=default_language,
                default_region=default_region,
                logger=self.log,
                resolver_http_timeout_ms=resolver_http_timeout_ms,
                resolver_total_budget_ms=resolver_total_budget_ms,
                resolver_retry_seconds=resolver_retry_seconds,
                profile=profile,
                setting_key=identity_profile_setting_key(profile),
            )
            for profile in IDENTITY_PROFILES
        }
        self.resolver = NameResolver(
            str(getattr(config, "tmdb_api_token", "")),
            str(getattr(config, "resolver_language", "es-ES")),
            str(getattr(config, "resolver_region", "ES")),
            int(getattr(config, "resolver_http_timeout_ms", 2500)),
            int(getattr(config, "resolver_total_budget_ms", 20_000)),
            database,
            self.log,
        )

    @property
    def enabled(self) -> bool:
        return self.resolver.enabled

    def payload(self, profile: str = "common") -> Dict[str, object]:
        normalized = _identity_profile(profile)
        return self._with_effective_metadata(
            self.stores[normalized].payload(), normalized
        )

    def update(
        self, payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        normalized = _identity_profile(profile)
        return self._with_effective_metadata(
            self.stores[normalized].update(payload), normalized
        )

    def reset(
        self, payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        normalized = _identity_profile(profile)
        return self._with_effective_metadata(
            self.stores[normalized].reset(payload), normalized
        )

    def clear_cache(self, profile: str = "common") -> Dict[str, object]:
        normalized_profile = _identity_profile(profile)
        result = self.stores[normalized_profile].clear_cache()
        result["profile"] = normalized_profile
        return result

    def job_snapshot(self, profile: str = "common") -> Dict[str, object]:
        normalized_profile = _identity_profile(profile)
        if normalized_profile in {"movies", "tv"}:
            return self.job_snapshot_for_category(normalized_profile)
        result = self.stores[normalized_profile].job_snapshot()
        result["profile"] = normalized_profile
        return result

    def classification_snapshot(self) -> Dict[str, object]:
        """Snapshot common usado exclusivamente antes de conocer la categoria."""

        return self.job_snapshot("common")

    def job_snapshot_for_category(self, category: object) -> Dict[str, object]:
        profile = _profile_for_category(category)
        if profile == "common":
            return self.job_snapshot("common")
        keys = [
            identity_profile_setting_key("common"),
            identity_profile_setting_key(profile),
        ]
        reader = getattr(self.database, "get_settings", None)
        if callable(reader):
            stored = reader(keys)
            common = self.stores["common"].job_snapshot_from_raw(stored.get(keys[0]))
            category_snapshot = self.stores[profile].job_snapshot_from_raw(
                stored.get(keys[1])
            )
        else:
            common = self.stores["common"].job_snapshot()
            category_snapshot = self.stores[profile].job_snapshot()
        return _compose_profile_snapshots(common, category_snapshot, profile)

    def _store(self, profile: str) -> IdentitySettingsStore:
        return self.stores[_identity_profile(profile)]

    def _with_effective_metadata(
        self, result: Dict[str, object], profile: str
    ) -> Dict[str, object]:
        payload = dict(result)
        effective = (
            self.job_snapshot_for_category(profile)
            if profile in {"movies", "tv"}
            else self.job_snapshot("common")
        )
        payload["effective_fingerprint"] = effective.get("fingerprint")
        payload["effective_revisions"] = copy.deepcopy(
            effective.get("revisions")
            or {"common": int(effective.get("revision") or 0)}
        )
        return payload

    def rules_for_job(self, job: Dict[str, object]) -> Dict[str, object]:
        meta = _source_meta(job.get("source_meta_json"))
        stored = meta.get("identity_rules")
        if isinstance(stored, dict) and isinstance(stored.get("rules"), dict):
            try:
                snapshot_profile = _snapshot_profile(stored, job)
                rules = _normalize_job_identity_rules(
                    stored["rules"], snapshot_profile
                )
                result = {
                    "rules": rules,
                    "revision": int(stored.get("revision") or 0),
                    "saved_at": stored.get("saved_at"),
                    "fingerprint": _job_rules_fingerprint(
                        rules, snapshot_profile
                    ),
                    "profile": snapshot_profile,
                    "source": "job_snapshot",
                }
                for key in ("revisions", "fingerprints", "combined_fingerprint"):
                    if isinstance(stored.get(key), dict) or isinstance(stored.get(key), str):
                        result[key] = copy.deepcopy(stored[key])
                return result
            except (IdentityRulesValidationError, TypeError, ValueError) as error:
                raise IdentityRulesValidationError(
                    "El snapshot de identidad del trabajo no es ejecutable por phased-er-v2."
                ) from error

        legacy = meta.get("filebot_rules")
        if isinstance(legacy, dict) and isinstance(legacy.get("rules"), dict):
            raise IdentityRulesValidationError(
                "El snapshot FileBot heredado es historico y no puede ejecutar decisiones v1."
            )

        raise IdentityRulesValidationError(
            "El trabajo no contiene la politica de identidad v2 congelada."
        )

    def configure_for_job(self, job: Dict[str, object]) -> Dict[str, object]:
        context = self.rules_for_job(job)
        self.resolver.configure_rules(dict(context.get("rules") or {}))
        return context

    def decide_sources(
        self,
        sources: Iterable[str],
        category: str,
        rules: Optional[Dict[str, object]] = None,
    ) -> MediaDecision:
        active_rules = (
            rules
            if isinstance(rules, dict)
            else self.job_snapshot_for_category(category)["rules"]
        )
        parser_rules = active_rules.get("parser") if isinstance(active_rules.get("parser"), dict) else None
        decisions = [
            decide_media(source, category, rules=parser_rules)
            for source in sources
            if str(source or "").strip()
        ]
        if not decisions:
            return decide_media("", category, rules=parser_rules)
        for decision in decisions:
            if decision.block_reason == "category_conflict":
                return decision
        for decision in decisions:
            if decision.media_type == category and decision.confidence in {"high", "medium"}:
                return decision
        for decision in decisions:
            if decision.media_type in {"movies", "tv"}:
                return decision
        return decisions[0]

    def test_parser(
        self, payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        normalized_profile = _identity_profile(profile)
        try:
            name, category, rules = self._test_request(
                payload, allow_auto=True, profile=normalized_profile
            )
        except IdentityRulesValidationError as error:
            return {
                "ok": False,
                "profile": normalized_profile,
                "error": "invalid_rules",
                "message": str(error),
            }
        try:
            result = test_parser_title(name, category, rules=rules.get("parser"))
        except (IndexError, TypeError, ValueError, re.error):
            self.log.exception("Las reglas del parser fallaron durante la prueba")
            return {
                "ok": False,
                "profile": normalized_profile,
                "error": "parser_execution_failed",
                "message": "Las reglas del parser no pudieron aplicarse.",
            }
        return {
            "ok": True,
            "profile": normalized_profile,
            "status": _parser_status(result),
            "result": result,
        }

    def test_resolver(
        self, payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        normalized_profile = _identity_profile(profile)
        try:
            name, category, rules = self._test_request(
                payload, allow_auto=False, profile=normalized_profile
            )
        except IdentityRulesValidationError as error:
            return {
                "ok": False,
                "profile": normalized_profile,
                "status": "INVALID_RULES",
                "error": "invalid_rules",
                "message": str(error),
                "decision": _test_decision("INVALID_RULES"),
            }
        try:
            parser = test_parser_title(name, category, rules=rules.get("parser"))
        except (IndexError, TypeError, ValueError, re.error):
            self.log.exception("Las reglas del parser fallaron durante la prueba del resolver")
            return {
                "ok": False,
                "profile": normalized_profile,
                "status": "PARSER_ERROR",
                "error": "parser_execution_failed",
                "message": "Las reglas del parser no pudieron aplicarse.",
                "decision": _test_decision("PARSER_ERROR"),
            }
        result = self.resolver.preview(name, category, rules)
        result["profile"] = normalized_profile
        result["parser_test"] = parser
        return result

    def retry_delay(self, job: Dict[str, object], retry_number: int) -> int:
        context = self.rules_for_job(job)
        resolver = context.get("rules", {}).get("resolver", {})  # type: ignore[union-attr]
        retry = resolver.get("retry", {}) if isinstance(resolver, dict) else {}
        base = max(1, int(retry.get("base_seconds", 60)))
        multiplier = max(1, int(retry.get("multiplier", 2)))
        exponent = min(max(0, retry_number - 1), max(0, int(retry.get("max_exponent", 3))))
        maximum = max(base, int(retry.get("max_seconds", 300)))
        return min(maximum, base * (multiplier**exponent))

    def _test_request(
        self,
        payload: Dict[str, object],
        *,
        allow_auto: bool,
        profile: str = "common",
    ) -> tuple[str, str, Dict[str, object]]:
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 2_000 or "\x00" in name:
            raise IdentityRulesValidationError("name debe contener un titulo valido.")
        default_category = (
            "auto"
            if allow_auto
            else (profile if profile in {"movies", "tv"} else "movies")
        )
        category = str(payload.get("category") or default_category).strip().lower()
        aliases = {"movie": "movies", "pelicula": "movies", "series": "tv", "serie": "tv"}
        category = aliases.get(category, category)
        allowed = {"movies", "tv", "auto"} if allow_auto else {"movies", "tv"}
        if category not in allowed:
            raise IdentityRulesValidationError("category debe ser movies, tv o auto.")
        explicit_category = "" if category == "auto" else category
        supplied = payload.get("rules")
        if supplied is None:
            rules = (
                self.job_snapshot_for_category(category)["rules"]
                if category in {"movies", "tv"}
                else self.stores["common"].snapshot()
            )
        else:
            if category in {"movies", "tv"}:
                if profile == "common":
                    normalized_supplied = normalize_scoped_identity_rules(
                        supplied,
                        "common",
                        full_defaults=self.stores["common"]._full_defaults,
                    )
                    common_snapshot = {
                        "rules": normalized_supplied,
                        "revision": 0,
                        "fingerprint": identity_scope_fingerprint(
                            normalized_supplied, "common"
                        ),
                    }
                    category_snapshot = self.stores[category].job_snapshot()
                else:
                    normalized_supplied = normalize_scoped_identity_rules(
                        supplied,
                        profile,
                        full_defaults=self.stores[profile]._full_defaults,
                    )
                    common_snapshot = self.stores["common"].job_snapshot()
                    category_snapshot = {
                        "rules": normalized_supplied,
                        "revision": 0,
                        "fingerprint": identity_scope_fingerprint(
                            normalized_supplied, profile
                        ),
                    }
                rules = _compose_profile_snapshots(
                    common_snapshot, category_snapshot, category
                )["rules"]
            else:
                rules = normalize_scoped_identity_rules(
                    supplied,
                    "common",
                    full_defaults=self.stores["common"]._full_defaults,
                )
        return name, explicit_category, rules


def _identity_profile(value: object) -> str:
    profile = str(value or "").strip().lower()
    if profile not in IDENTITY_PROFILES:
        raise ValueError("profile debe ser common, movies o tv")
    return profile


def _profile_for_category(value: object) -> str:
    category = str(value or "").strip().lower()
    return category if category in {"movies", "tv"} else "common"


def _snapshot_profile(
    snapshot: Dict[str, object], job: Dict[str, object]
) -> str:
    profile = str(snapshot.get("profile") or "").strip().lower()
    if profile in IDENTITY_PROFILES:
        return profile
    return _profile_for_category(job.get("category"))


def _seed_profile_settings(database: object, defaults: Dict[str, object]) -> None:
    """Si la instalacion es nueva, crea juntos los tres scopes v2.

    No lee, convierte ni ejecuta configuracion v1. Una instalacion parcial se
    detiene para evitar completar silenciosamente una politica incompleta.
    """

    keys = tuple(IDENTITY_PROFILE_SETTING_KEYS.values())
    reader = getattr(database, "get_settings", None)
    writer = getattr(database, "compare_and_set_settings", None)
    if not callable(reader) or not callable(writer):
        raise RuntimeError("La base de datos no admite settings v2 atomicos.")
    current = reader(keys)
    present = [current.get(key) is not None for key in keys]
    if all(present):
        return
    if any(present):
        raise RuntimeError(
            "Configuracion identity.pipeline.v2 parcial: deben existir common, movies y tv."
        )
    documents = {}
    for profile, key in IDENTITY_PROFILE_SETTING_KEYS.items():
        documents[key] = json.dumps(
            {
                "rules": scope_identity_rules(defaults, profile),
                "revision": 0,
                "saved_at": None,
                "history": [],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    if writer(
        {key: None for key in keys},
        documents,
        clear_resolver_cache=True,
    ):
        return
    refreshed = reader(keys)
    if not all(refreshed.get(key) is not None for key in keys):
        raise RuntimeError("No se pudieron crear atomicamente los scopes de identidad v2.")


def _compose_profile_snapshots(
    common: Dict[str, object],
    category_snapshot: Dict[str, object],
    profile: str,
) -> Dict[str, object]:
    """Compila Common + override de categoria sin heredar copias redundantes."""

    normalized = compose_identity_scopes(
        common.get("rules"),
        category_snapshot.get("rules"),
        profile,
    )
    common_revision = int(common.get("revision") or 0)
    category_revision = int(category_snapshot.get("revision") or 0)
    fingerprints = {
        "common": str(
            common.get("fingerprint")
            or identity_scope_fingerprint(common["rules"], "common")
        ),
        profile: str(
            category_snapshot.get("fingerprint")
            or identity_scope_fingerprint(category_snapshot["rules"], profile)
        ),
    }
    combined_fingerprint = identity_fingerprint(normalized)
    return {
        "rules": normalized,
        "revision": category_revision,
        "revisions": {"common": common_revision, profile: category_revision},
        "saved_at": category_snapshot.get("saved_at") or common.get("saved_at"),
        "fingerprint": combined_fingerprint,
        "combined_fingerprint": combined_fingerprint,
        "fingerprints": fingerprints,
        "profile": profile,
    }

def _normalize_job_identity_rules(
    value: Dict[str, object], profile: str
) -> Dict[str, object]:
    """Acepta solo snapshots v2; los v1 quedan como historico pasivo."""

    rules = copy.deepcopy(value)
    if rules.get("schema_version") != 2:
        raise IdentityRulesValidationError(
            "El snapshot de identidad no usa schema_version 2."
        )
    try:
        return normalize_identity_rules(rules)
    except IdentityRulesValidationError:
        if profile != "common":
            raise
        return normalize_scoped_identity_rules(rules, "common")

def _source_meta(value: object) -> Dict[str, object]:
    if isinstance(value, dict):
        return value
    try:
        payload = json.loads(str(value)) if value else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parser_status(result: Dict[str, object]) -> str:
    if result.get("category_conflict"):
        return "CATEGORY_CONFLICT"
    if not result.get("title"):
        return "EMPTY_TITLE"
    if result.get("category") == "manual":
        return "MANUAL"
    return "CLEAN"


def _job_rules_fingerprint(rules: Dict[str, object], profile: str) -> str:
    try:
        return identity_fingerprint(rules)
    except IdentityRulesValidationError:
        return identity_scope_fingerprint(rules, profile)


def _test_decision(status: str) -> Dict[str, object]:
    return {
        "status": status,
        "accepted": False,
        "has_scoring": False,
        "bypass": False,
    }
