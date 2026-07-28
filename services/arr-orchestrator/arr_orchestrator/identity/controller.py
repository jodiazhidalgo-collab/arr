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
from ..source_context.contract import (
    SourceContextContractError,
    validate_source_title,
)
from .resolver_defaults import DEFAULT_SOURCE_TITLE_FALLBACK
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
        self.store = IdentitySettingsStore(
            database,
            default_language=str(getattr(config, "resolver_language", "es-ES")),
            default_region=str(getattr(config, "resolver_region", "ES")),
            logger=self.log,
            resolver_http_timeout_ms=int(
                getattr(config, "resolver_http_timeout_ms", 2500)
            ),
            resolver_total_budget_ms=int(
                getattr(config, "resolver_total_budget_ms", 5000)
            ),
            resolver_retry_seconds=int(
                getattr(config, "resolver_retry_seconds", 60)
            ),
        )
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

    def payload(self) -> Dict[str, object]:
        return self.store.payload()

    def update(self, payload: Dict[str, object]) -> Dict[str, object]:
        return self.store.update(payload)

    def reset(self, payload: Dict[str, object]) -> Dict[str, object]:
        return self.store.reset(payload)

    def clear_cache(self) -> Dict[str, object]:
        return self.store.clear_cache()

    def job_snapshot(self) -> Dict[str, object]:
        return self.store.job_snapshot()

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
                "source": "legacy_filebot_snapshot",
            }

        current = self.store.job_snapshot()
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
        active_rules = rules if isinstance(rules, dict) else self.store.snapshot()
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

    def test_parser(self, payload: Dict[str, object]) -> Dict[str, object]:
        try:
            name, category, rules = self._test_request(payload, allow_auto=True)
        except IdentityRulesValidationError as error:
            return {"ok": False, "error": "invalid_rules", "message": str(error)}
        try:
            result = test_parser_title(name, category, rules=rules.get("parser"))
        except (IndexError, TypeError, ValueError, re.error):
            self.log.exception("Las reglas del parser fallaron durante la prueba")
            return {
                "ok": False,
                "error": "parser_execution_failed",
                "message": "Las reglas del parser no pudieron aplicarse.",
            }
        return {"ok": True, "status": _parser_status(result), "result": result}

    def test_resolver(self, payload: Dict[str, object]) -> Dict[str, object]:
        try:
            name, category, rules = self._test_request(payload, allow_auto=False)
        except IdentityRulesValidationError as error:
            return {
                "ok": False,
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
                "status": "PARSER_ERROR",
                "error": "parser_execution_failed",
                "message": "Las reglas del parser no pudieron aplicarse.",
                "decision": _test_decision("PARSER_ERROR"),
            }
        raw_source_title = payload.get("source_title")
        source_title = ""
        if raw_source_title not in (None, ""):
            try:
                source_title = validate_source_title(raw_source_title)
            except SourceContextContractError:
                return {
                    "ok": False,
                    "status": "INVALID_RULES",
                    "error": "invalid_source_title",
                    "message": "source_title no es válido.",
                    "decision": _test_decision("INVALID_RULES"),
                }
        result = self.resolver.preview(
            name,
            category,
            rules,
            source_title=source_title,
            source="preview",
        )
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
        self, payload: Dict[str, object], *, allow_auto: bool
    ) -> tuple[str, str, Dict[str, object]]:
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 2_000 or "\x00" in name:
            raise IdentityRulesValidationError("name debe contener un titulo valido.")
        category = str(payload.get("category") or ("auto" if allow_auto else "movies")).strip().lower()
        aliases = {"movie": "movies", "pelicula": "movies", "series": "tv", "serie": "tv"}
        category = aliases.get(category, category)
        allowed = {"movies", "tv", "auto"} if allow_auto else {"movies", "tv"}
        if category not in allowed:
            raise IdentityRulesValidationError("category debe ser movies, tv o auto.")
        explicit_category = "" if category == "auto" else category
        supplied = payload.get("rules")
        rules = normalize_identity_rules(supplied) if supplied is not None else self.store.snapshot()
        return name, explicit_category, rules

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
    if not only_when_missing or "source_title_fallback" not in resolver:
        source_fallback = copy.deepcopy(DEFAULT_SOURCE_TITLE_FALLBACK)
        source_fallback["enabled"] = False
        resolver["source_title_fallback"] = source_fallback
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
