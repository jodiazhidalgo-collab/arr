"""Almacen CAS de un perfil de identidad con snapshot coherente e historial."""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol

from .defaults import (
    IDENTITY_HISTORY_LIMIT,
    IDENTITY_SCHEMA_VERSION,
    factory_identity_rules,
    identity_profile_setting_key,
)
from .schema import identity_settings_schema
from .scopes import (
    identity_scope_fingerprint,
    normalize_scoped_identity_rules,
    scope_identity_rules,
)
from .validation import IdentityRulesValidationError, normalize_identity_rules
from .validation.common import choice, exact_keys, expect_object, integer, text


_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class SettingsDatabase(Protocol):
    def get_setting(self, key: str) -> Optional[str]: ...

    def set_setting(self, key: str, value: str) -> None: ...

    def compare_and_set_setting_value(
        self, key: str, expected_value: Optional[str], value: str
    ) -> bool: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_history(value: object, limit: int) -> List[Dict[str, object]]:
    if not isinstance(value, list):
        raise IdentityRulesValidationError("identity.pipeline.history debe ser una lista.")
    result: List[Dict[str, object]] = []
    for index, item in enumerate(value[-limit:], start=1):
        entry = expect_object(item, f"identity.pipeline.history[{index}]")
        exact_keys(
            entry,
            {"revision", "saved_at", "fingerprint", "action"},
            f"identity.pipeline.history[{index}]",
        )
        fingerprint = text(
            entry.get("fingerprint"), f"identity.pipeline.history[{index}].fingerprint"
        )
        if not _FINGERPRINT_RE.fullmatch(fingerprint):
            raise IdentityRulesValidationError(
                f"identity.pipeline.history[{index}].fingerprint no es valido."
            )
        result.append(
            {
                "revision": integer(
                    entry.get("revision"),
                    f"identity.pipeline.history[{index}].revision",
                    1,
                    2_147_483_647,
                ),
                "saved_at": text(
                    entry.get("saved_at"),
                    f"identity.pipeline.history[{index}].saved_at",
                ),
                "fingerprint": fingerprint,
                "action": choice(
                    entry.get("action"),
                    f"identity.pipeline.history[{index}].action",
                    ("save", "reset", "migrate"),
                ),
            }
        )
    return result


def _stored_revision(raw: Optional[str]) -> int:
    if raw is None:
        return 0
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    revision = payload.get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
        return revision
    try:
        normalize_identity_rules(payload)
    except IdentityRulesValidationError:
        return 0
    if payload.get("schema_version") == IDENTITY_SCHEMA_VERSION:
        return 1
    return 0


class IdentitySettingsStore:
    """Snapshot en memoria, CAS atomico al guardar y defaults recuperables."""

    def __init__(
        self,
        database: SettingsDatabase,
        default_language: str = "es-ES",
        default_region: str = "ES",
        logger: Optional[logging.Logger] = None,
        history_limit: int = IDENTITY_HISTORY_LIMIT,
        resolver_http_timeout_ms: int = 2500,
        resolver_total_budget_ms: int = 20_000,
        resolver_retry_seconds: int = 60,
        profile: str = "common",
        setting_key: str = identity_profile_setting_key("common"),
    ) -> None:
        if (
            isinstance(history_limit, bool)
            or not isinstance(history_limit, int)
            or not 1 <= history_limit <= 50
        ):
            raise ValueError("history_limit debe estar entre 1 y 50")
        self._database = database
        self._logger = logger or logging.getLogger("arr-orchestrator.identity-settings")
        self._lock = threading.RLock()
        self._history_limit = history_limit
        self._profile = str(profile or "common")
        self._setting_key = str(
            setting_key or identity_profile_setting_key("common")
        )
        self._full_defaults = normalize_identity_rules(
            factory_identity_rules(
                default_language,
                default_region,
                resolver_http_timeout_ms,
                resolver_total_budget_ms,
                resolver_retry_seconds,
            )
        )
        self._defaults = scope_identity_rules(self._full_defaults, self._profile)
        self._rules = copy.deepcopy(self._defaults)
        self._revision = 0
        self._saved_at: Optional[str] = None
        self._history: List[Dict[str, object]] = []
        self._stored_raw: Optional[str] = None
        self._stored_is_canonical = True
        self._stored_error: Optional[str] = None
        self._load_locked(self._database.get_setting(self._setting_key))

    def _load_locked(self, raw: Optional[str]) -> None:
        self._stored_raw = raw
        if raw is None:
            self._rules = copy.deepcopy(self._defaults)
            self._revision = 0
            self._saved_at = None
            self._history = []
            self._stored_is_canonical = True
            self._stored_error = None
            return
        try:
            payload = json.loads(raw)
            envelope = expect_object(payload, "identity.pipeline")
            if "rules" not in envelope:
                raise IdentityRulesValidationError(
                    "identity.pipeline.v2 requiere un sobre versionado con rules."
                )
            if set(envelope) != {"rules", "revision", "saved_at", "history"}:
                raise IdentityRulesValidationError(
                    "identity.pipeline contiene campos incompletos o desconocidos."
                )
            revision = envelope.get("revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise IdentityRulesValidationError(
                    "identity.pipeline.revision no es valida."
                )
            saved_at = envelope.get("saved_at")
            if saved_at is not None and not isinstance(saved_at, str):
                raise IdentityRulesValidationError(
                    "identity.pipeline.saved_at no es valido."
                )
            self._rules = normalize_scoped_identity_rules(
                envelope.get("rules"),
                self._profile,
                full_defaults=self._full_defaults,
            )
            self._revision = revision
            self._saved_at = saved_at
            self._history = _normalize_history(
                envelope.get("history"), self._history_limit
            )
            self._stored_is_canonical = True
            self._stored_error = None
        except (
            IdentityRulesValidationError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            self._logger.warning(
                "identity.pipeline persistido no es valido; se conservan defaults seguros: %s",
                error,
            )
            self._rules = copy.deepcopy(self._defaults)
            self._revision = 0
            self._saved_at = None
            self._history = []
            self._stored_is_canonical = False
            self._stored_error = str(error)

    def _refresh_locked(self, *, force: bool = False) -> None:
        raw = self._database.get_setting(self._setting_key)
        if force or raw != self._stored_raw:
            self._load_locked(raw)

    def _cache_status(self) -> Dict[str, object]:
        reader = getattr(self._database, "resolver_cache_stats", None)
        if not callable(reader):
            return {"available": False}
        try:
            status = reader(self._profile)
            result = dict(status) if isinstance(status, dict) else {}
            result["available"] = True
            return result
        except Exception:
            self._logger.exception("No se pudo consultar el estado de cache de identidad")
            return {"available": False, "error": "cache_status_failed"}

    def _response_locked(self, *, ok: bool = True) -> Dict[str, object]:
        return {
            "ok": ok,
            "rules": copy.deepcopy(self._rules),
            "defaults": copy.deepcopy(self._defaults),
            "schema": identity_settings_schema(self._profile),
            "revision": self._revision,
            "saved_at": self._saved_at,
            "fingerprint": identity_scope_fingerprint(self._rules, self._profile),
            "history": copy.deepcopy(self._history),
            "history_limit": self._history_limit,
            "cache_status": self._cache_status(),
            "rules_path": f"settings/{self._setting_key}",
            "setting_key": self._setting_key,
            "profile": self._profile,
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "export_format": "arr-identity-export-v2",
            "repair_required": not self._stored_is_canonical,
        }

    def payload(self) -> Dict[str, object]:
        """Contrato completo del panel, siempre desacoplado del snapshot interno."""

        with self._lock:
            return self._response_locked()

    def snapshot(self) -> Dict[str, object]:
        """Copia de reglas normalizadas sin consultar SQLite."""

        with self._lock:
            self._ensure_executable_locked()
            return copy.deepcopy(self._rules)

    def job_snapshot(self) -> Dict[str, object]:
        """Reglas, revision y huella capturadas bajo un unico lock."""

        with self._lock:
            self._ensure_executable_locked()
            return {
                "rules": copy.deepcopy(self._rules),
                "revision": self._revision,
                "saved_at": self._saved_at,
                "fingerprint": identity_scope_fingerprint(self._rules, self._profile),
            }

    def job_snapshot_from_raw(self, raw: Optional[str]) -> Dict[str, object]:
        """Decodifica un valor obtenido junto a otros en un snapshot DB atomico."""

        with self._lock:
            if raw is None:
                raise IdentityRulesValidationError(
                    f"{self._setting_key} no existe; no se puede crear un snapshot ejecutable."
                )
            try:
                payload = json.loads(raw)
                envelope = expect_object(payload, "identity.pipeline")
                if set(envelope) != {"rules", "revision", "saved_at", "history"}:
                    raise IdentityRulesValidationError(
                        "identity.pipeline contiene campos incompletos o desconocidos."
                    )
                rules = normalize_scoped_identity_rules(
                    envelope.get("rules"),
                    self._profile,
                    full_defaults=self._full_defaults,
                )
                raw_revision = envelope.get("revision")
                if (
                    isinstance(raw_revision, bool)
                    or not isinstance(raw_revision, int)
                    or raw_revision < 0
                ):
                    raise IdentityRulesValidationError(
                        "identity.pipeline.revision no es valida."
                    )
                revision = raw_revision
                saved_at = envelope.get("saved_at")
                if saved_at is not None and not isinstance(saved_at, str):
                    raise IdentityRulesValidationError(
                        "identity.pipeline.saved_at no es valido."
                    )
                _normalize_history(envelope.get("history"), self._history_limit)
            except (
                IdentityRulesValidationError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as error:
                raise IdentityRulesValidationError(
                    f"{self._setting_key} no es ejecutable: {error}"
                ) from error
            return {
                "rules": rules,
                "revision": revision,
                "saved_at": saved_at,
                "fingerprint": identity_scope_fingerprint(rules, self._profile),
            }

    def _ensure_executable_locked(self) -> None:
        if self._stored_raw is None or self._stored_is_canonical:
            return
        raise IdentityRulesValidationError(
            f"{self._setting_key} no es ejecutable: "
            f"{self._stored_error or 'requiere reparacion explicita.'}"
        )

    def _conflict_locked(self, expected_revision: int) -> Dict[str, object]:
        result = self._response_locked(ok=False)
        result.update(
            {
                "error": "revision_conflict",
                "message": "La configuracion cambio en otra ventana; recarga antes de guardar.",
                "expected_revision": expected_revision,
                "current_revision": self._revision,
            }
        )
        return result

    def _compare_and_set(self, expected_revision: int, serialized: str) -> bool:
        exact_writer = getattr(self._database, "compare_and_set_setting_value", None)
        if callable(exact_writer):
            return bool(
                exact_writer(self._setting_key, self._stored_raw, serialized)
            )
        writer = getattr(self._database, "compare_and_set_setting", None)
        if callable(writer):
            return bool(writer(self._setting_key, expected_revision, serialized))
        # Compatibilidad para dobles antiguos. Database usa el CAS real.
        current = self._database.get_setting(self._setting_key)
        if _stored_revision(current) != expected_revision:
            return False
        self._database.set_setting(self._setting_key, serialized)
        return True

    def _write_locked(
        self,
        normalized: Dict[str, object],
        expected_revision: int,
        action: str,
    ) -> Dict[str, object]:
        self._refresh_locked()
        if expected_revision != self._revision:
            return self._conflict_locked(expected_revision)
        if normalized == self._rules and self._stored_is_canonical:
            result = self._response_locked()
            result.update({"saved": False, "action": action})
            return result

        revision = self._revision + 1
        saved_at = _utc_now()
        fingerprint = identity_scope_fingerprint(normalized, self._profile)
        history = [
            *self._history,
            {
                "revision": revision,
                "saved_at": saved_at,
                "fingerprint": fingerprint,
                "action": action,
            },
        ][-self._history_limit :]
        envelope: Dict[str, object] = {
            "rules": normalized,
            "revision": revision,
            "saved_at": saved_at,
            "history": history,
        }
        serialized = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            saved = self._compare_and_set(expected_revision, serialized)
        except Exception:
            self._logger.exception("No se pudo guardar %s", self._setting_key)
            result = self._response_locked(ok=False)
            result.update(
                {
                    "error": "persistence_failed",
                    "message": "No se pudo guardar la configuracion de identidad.",
                }
            )
            return result
        if not saved:
            self._refresh_locked(force=True)
            return self._conflict_locked(expected_revision)

        self._rules = copy.deepcopy(normalized)
        self._revision = revision
        self._saved_at = saved_at
        self._history = history
        self._stored_raw = serialized
        self._stored_is_canonical = True
        self._stored_error = None
        result = self._response_locked()
        result.update({"saved": True, "action": action})
        return result

    def update(self, payload: object) -> Dict[str, object]:
        try:
            request = expect_object(payload, "payload")
            exact_keys(request, {"rules", "expected_revision"}, "payload")
            expected_revision = integer(
                request.get("expected_revision"),
                "expected_revision",
                0,
                2_147_483_647,
            )
            normalized = normalize_scoped_identity_rules(
                request.get("rules"),
                self._profile,
                full_defaults=self._full_defaults,
            )
        except IdentityRulesValidationError as error:
            with self._lock:
                result = self._response_locked(ok=False)
            result.update({"error": "invalid_rules", "message": str(error)})
            return result
        with self._lock:
            return self._write_locked(normalized, expected_revision, "save")

    def reset(self, payload: object) -> Dict[str, object]:
        try:
            request = expect_object(payload, "payload")
            exact_keys(request, {"expected_revision"}, "payload")
            expected_revision = integer(
                request.get("expected_revision"),
                "expected_revision",
                0,
                2_147_483_647,
            )
        except IdentityRulesValidationError as error:
            with self._lock:
                result = self._response_locked(ok=False)
            result.update({"error": "invalid_rules", "message": str(error)})
            return result
        with self._lock:
            return self._write_locked(
                copy.deepcopy(self._defaults), expected_revision, "reset"
            )

    def clear_cache(self) -> Dict[str, object]:
        """Vacia solo la cache del resolver; no altera reglas ni revision."""

        clearer = getattr(self._database, "clear_resolver_cache", None)
        if not callable(clearer):
            return {
                "ok": False,
                "error": "persistence_failed",
                "message": "La base de datos no permite limpiar la cache de identidad.",
            }
        try:
            deleted = int(clearer(self._profile))
        except Exception:
            self._logger.exception("No se pudo limpiar la cache de identidad")
            return {
                "ok": False,
                "error": "persistence_failed",
                "message": "No se pudo limpiar la cache de identidad.",
            }
        return {"ok": True, "deleted": deleted, "cache_status": self._cache_status()}


__all__ = ["IdentitySettingsStore", "SettingsDatabase"]
