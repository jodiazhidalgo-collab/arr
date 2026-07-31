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
    IDENTITY_SETTING_KEY,
    factory_identity_rules,
    identity_profile_setting_key,
)
from .settings import (
    IdentityRulesValidationError,
    IdentitySettingsStore,
    identity_fingerprint,
    normalize_identity_rules,
)


_HISTORICAL_TV_PATTERNS = {
    "series_sxe": r"(?i)\bS0?(\d{1,2})\s*E0?(\d{1,3})(?:\s*(?:-|_|E)\s*0?(\d{1,3}))?\b",
    "explicit_season": r"(?i)\b(?:temporada|season)\s*0?(\d{1,2})\b",
    "season_pack": r"(?i)(?:^|\s)T0?(\d{1,2})(?:\b|[- ]|$)",
}


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
            getattr(config, "resolver_total_budget_ms", 5000)
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
        # Alias conservado para consumidores antiguos: siempre representa common.
        self.store = self.stores["common"]
        self.resolver = NameResolver(
            str(getattr(config, "tmdb_api_token", "")),
            str(getattr(config, "resolver_language", "es-ES")),
            str(getattr(config, "resolver_region", "ES")),
            int(getattr(config, "resolver_http_timeout_ms", 2500)),
            int(getattr(config, "resolver_total_budget_ms", 5000)),
            database,
            self.log,
        )

    @property
    def enabled(self) -> bool:
        return self.resolver.enabled

    def payload(self, profile: str = "common") -> Dict[str, object]:
        return self._store(profile).payload()

    def update(
        self, payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        return self._store(profile).update(payload)

    def reset(
        self, payload: Dict[str, object], profile: str = "common"
    ) -> Dict[str, object]:
        return self._store(profile).reset(payload)

    def clear_cache(self, profile: str = "common") -> Dict[str, object]:
        normalized_profile = _identity_profile(profile)
        result = self.stores[normalized_profile].clear_cache()
        result["profile"] = normalized_profile
        return result

    def job_snapshot(self, profile: str = "common") -> Dict[str, object]:
        normalized_profile = _identity_profile(profile)
        result = self.stores[normalized_profile].job_snapshot()
        result["profile"] = normalized_profile
        return result

    def classification_snapshot(self) -> Dict[str, object]:
        """Snapshot common usado exclusivamente antes de conocer la categoria."""

        return self.job_snapshot("common")

    def job_snapshot_for_category(self, category: object) -> Dict[str, object]:
        return self.job_snapshot(_profile_for_category(category))

    def _store(self, profile: str) -> IdentitySettingsStore:
        return self.stores[_identity_profile(profile)]

    def rules_for_job(self, job: Dict[str, object]) -> Dict[str, object]:
        meta = _source_meta(job.get("source_meta_json"))
        stored = meta.get("identity_rules")
        if isinstance(stored, dict) and isinstance(stored.get("rules"), dict):
            try:
                rules = _normalize_job_identity_rules(stored["rules"])
                return {
                    "rules": rules,
                    "revision": int(stored.get("revision") or 0),
                    "saved_at": stored.get("saved_at"),
                    "fingerprint": identity_fingerprint(rules),
                    "profile": _snapshot_profile(stored, job),
                    "source": "job_snapshot",
                }
            except (IdentityRulesValidationError, TypeError, ValueError):
                self.log.warning("Snapshot identity_rules invalido en job; se usa fallback seguro")

        legacy = meta.get("filebot_rules")
        if isinstance(legacy, dict) and isinstance(legacy.get("rules"), dict):
            defaults = copy.deepcopy(self.store.payload()["defaults"])
            _disable_new_identity_behaviors(defaults, only_when_missing=False)
            migrated = _merge_legacy_filebot(defaults, legacy["rules"])
            return {
                "rules": migrated,
                "revision": int(legacy.get("revision") or 0),
                "saved_at": legacy.get("saved_at"),
                "fingerprint": identity_fingerprint(migrated),
                "profile": _profile_for_category(job.get("category")),
                "source": "legacy_filebot_snapshot",
            }

        current = self.job_snapshot_for_category(job.get("category"))
        current["source"] = "current_fallback"
        return current

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
            else self.stores[_profile_for_category(category)].snapshot()
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
        rules = (
            normalize_identity_rules(supplied)
            if supplied is not None
            else self.stores[profile].snapshot()
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
    """Clona una sola vez legacy/defaults sin tocar nunca ``identity.pipeline``."""

    reader = getattr(database, "get_setting")
    legacy_raw = reader(IDENTITY_SETTING_KEY)
    seed_raw = legacy_raw
    if seed_raw is None:
        seed_raw = json.dumps(
            {
                "rules": defaults,
                "revision": 0,
                "saved_at": None,
                "history": [],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    exact_writer = getattr(database, "compare_and_set_setting_value", None)
    writer = getattr(database, "set_setting")
    for profile in IDENTITY_PROFILES:
        setting_key = identity_profile_setting_key(profile)
        if reader(setting_key) is not None:
            continue
        if callable(exact_writer):
            exact_writer(setting_key, None, seed_raw)
        elif reader(setting_key) is None:
            writer(setting_key, seed_raw)

def _merge_legacy_filebot(
    identity_rules: Dict[str, object], legacy_rules: Dict[str, object]
) -> Dict[str, object]:
    rules = copy.deepcopy(identity_rules)
    resolver = rules.get("resolver")
    if not isinstance(resolver, dict):
        return rules
    locales = resolver.get("locales")
    aliases = resolver.get("aliases")
    forced = resolver.get("forced_matches")
    if not all(isinstance(value, dict) for value in (locales, aliases, forced)):
        return rules
    for category in ("movies", "tv"):
        legacy_category = legacy_rules.get(category)
        if not isinstance(legacy_category, dict):
            continue
        locale = locales.get(category)  # type: ignore[union-attr]
        if isinstance(locale, dict):
            if legacy_category.get("language"):
                locale["language"] = legacy_category["language"]
            if category == "movies" and legacy_category.get("region"):
                locale["region"] = legacy_category["region"]
        if isinstance(legacy_category.get("query_aliases"), list):
            aliases[category] = copy.deepcopy(legacy_category["query_aliases"])  # type: ignore[index]
        if isinstance(legacy_category.get("forced_matches"), list):
            forced[category] = copy.deepcopy(legacy_category["forced_matches"])  # type: ignore[index]
    return normalize_identity_rules(rules)


def _normalize_job_identity_rules(value: Dict[str, object]) -> Dict[str, object]:
    """Normaliza snapshots antiguos sin cambiar la decision que capturaron."""

    rules = copy.deepcopy(value)
    _disable_new_identity_behaviors(rules, only_when_missing=True)
    return normalize_identity_rules(rules)


def _disable_new_identity_behaviors(
    rules: Dict[str, object], *, only_when_missing: bool
) -> None:
    """Conserva la semantica de snapshots y configuraciones heredadas."""

    parser = rules.get("parser")
    if isinstance(parser, dict):
        for key in (
            "video_extensions",
            "video_markers",
            "non_video_markers",
            "season_number_words",
        ):
            if not only_when_missing or key not in parser:
                parser[key] = []
        normalization = parser.get("normalization")
        if isinstance(normalization, dict):
            for key in (
                "movie_without_year_from_video",
                "allow_tv_year_range",
            ):
                if not only_when_missing or key not in normalization:
                    normalization[key] = False
        patterns = parser.get("patterns")
        if isinstance(patterns, dict):
            for key, historical_pattern in _HISTORICAL_TV_PATTERNS.items():
                if not only_when_missing or key not in patterns:
                    patterns[key] = historical_pattern

    resolver = rules.get("resolver")
    if not isinstance(resolver, dict):
        return
    if not only_when_missing or "original_language_preference" not in resolver:
        resolver["original_language_preference"] = {
            "enabled": False,
            "language": "en",
        }
    acceptance = resolver.get("acceptance")
    if isinstance(acceptance, dict) and (
        not only_when_missing
        or "prefer_oldest_exact_title_without_year" not in acceptance
    ):
        acceptance["prefer_oldest_exact_title_without_year"] = False


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


def _test_decision(status: str) -> Dict[str, object]:
    return {
        "status": status,
        "accepted": False,
        "has_scoring": False,
        "bypass": False,
    }
