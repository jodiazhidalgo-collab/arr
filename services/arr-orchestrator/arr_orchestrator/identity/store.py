"""Almacen CAS de ``identity.pipeline`` con snapshot coherente e historial."""

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
    IDENTITY_RULES_PATH,
    IDENTITY_SCHEMA_VERSION,
    IDENTITY_SETTING_KEY,
    factory_identity_rules,
)
from .fingerprint import identity_fingerprint
from .schema import identity_settings_schema
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
                    ("save", "reset"),
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
        resolver_total_budget_ms: int = 5000,
        resolver_retry_seconds: int = 60,
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
        self._defaults = normalize_identity_rules(
            factory_identity_rules(
                default_language,
                default_region,
                resolver_http_timeout_ms,
                resolver_total_budget_ms,
                resolver_retry_seconds,
            )
        )
        self._rules = copy.deepcopy(self._defaults)
        self._revision = 0
        self._saved_at: Optional[str] = None
        self._history: List[Dict[str, object]] = []
        self._stored_raw: Optional[str] = None
        self._stored_is_canonical = True
        self._load_locked(self._database.get_setting(IDENTITY_SETTING_KEY))

    def _load_locked(self, raw: Optional[str]) -> None:
        self._stored_raw = raw
        if raw is None:
            self._rules = copy.deepcopy(self._defaults)
            self._revision = 0
            self._saved_at = None
            self._history = []
            self._stored_is_canonical = True
            return
        try:
            payload = json.loads(raw)
            envelope = expect_object(payload, "identity.pipeline")
            if "rules" not in envelope:
                # Compatibilidad defensiva con una primera escritura de reglas puras.
                self._rules = normalize_identity_rules(envelope)
                self._revision = 1
                self._saved_at = None
                self._history = []
                self._stored_is_canonical = False
                return
            exact_keys(
                envelope,
                {"rules", "revision", "saved_at", "history"},
                "identity.pipeline",
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
            self._rules = normalize_identity_rules(envelope.get("rules"))
            self._revision = revision
            self._saved_at = saved_at
            self._history = _normalize_history(
                envelope.get("history"), self._history_limit
            )
            self._stored_is_canonical = True
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

    def _refresh_locked(self, *, force: bool = False) -> None:
        raw = self._database.get_setting(IDENTITY_SETTING_KEY)
        if force or raw != self._stored_raw:
            self._load_locked(raw)

    def _cache_status(self) -> Dict[str, object]:
        reader = getattr(self._database, "resolver_cache_stats", None)
        if not callable(reader):
            return {"available": False}
        try:
            status = reader()
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
            "schema": identity_settings_schema(),
            "revision": self._revision,
            "saved_at": self._saved_at,
            "fingerprint": identity_fingerprint(self._rules),
            "history": copy.deepcopy(self._history),
            "history_limit": self._history_limit,
            "cache_status": self._cache_status(),
            "rules_path": IDENTITY_RULES_PATH,
            "repair_required": not self._stored_is_canonical,
        }

    def payload(self) -> Dict[str, object]:
        """Contrato completo del panel, siempre desacoplado del snapshot interno."""

        with self._lock:
            return self._response_locked()

    def snapshot(self) -> Dict[str, object]:
        """Copia de reglas normalizadas sin consultar SQLite."""

        with self._lock:
            return copy.deepcopy(self._rules)

    def job_snapshot(self) -> Dict[str, object]:
        """Reglas, revision y huella capturadas bajo un unico lock."""

        with self._lock:
            return {
                "rules": copy.deepcopy(self._rules),
                "revision": self._revision,
                "saved_at": self._saved_at,
                "fingerprint": identity_fingerprint(self._rules),
            }

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
                exact_writer(IDENTITY_SETTING_KEY, self._stored_raw, serialized)
            )
        writer = getattr(self._database, "compare_and_set_setting", None)
        if callable(writer):
            return bool(writer(IDENTITY_SETTING_KEY, expected_revision, serialized))
        # Compatibilidad para dobles antiguos. Database usa el CAS real.
        current = self._database.get_setting(IDENTITY_SETTING_KEY)
        if _stored_revision(current) != expected_revision:
            return False
        self._database.set_setting(IDENTITY_SETTING_KEY, serialized)
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
        fingerprint = identity_fingerprint(normalized)
        history = [
            *self._history,
            {
                "revision": revision,
                "saved_at": saved_at,
                "fingerprint": fingerprint,
                "action": action,
            },
        ][-self._history_limit :]
        serialized = json.dumps(
            {
                "rules": normalized,
                "revision": revision,
                "saved_at": saved_at,
                "history": history,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            saved = self._compare_and_set(expected_revision, serialized)
        except Exception:
            self._logger.exception("No se pudo guardar identity.pipeline")
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
            normalized = normalize_identity_rules(request.get("rules"))
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

    def migrate_legacy_if_absent(self, rules: object) -> Dict[str, object]:
        """Inicializa desde FileBot solo cuando la clave nunca fue persistida.

        Un valor existente, aunque sea invalido o de un esquema futuro, queda
        intacto para que solo una accion explicita de reparacion pueda
        sustituirlo. El CAS por valor exacto protege tambien la carrera entre
        el chequeo de ausencia y la escritura real de produccion.
        """

        try:
            normalized = normalize_identity_rules(rules)
        except IdentityRulesValidationError as error:
            with self._lock:
                result = self._response_locked(ok=False)
            result.update({"error": "invalid_rules", "message": str(error)})
            return result

        with self._lock:
            self._refresh_locked()
            if self._stored_raw is not None:
                result = self._response_locked()
                result.update({"saved": False, "migrated": False, "action": "save"})
                return result
            result = self._write_locked(normalized, 0, "save")
            result["migrated"] = bool(result.get("saved"))
            return result

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
            deleted = int(clearer())
        except Exception:
            self._logger.exception("No se pudo limpiar la cache de identidad")
            return {
                "ok": False,
                "error": "persistence_failed",
                "message": "No se pudo limpiar la cache de identidad.",
            }
        return {"ok": True, "deleted": deleted, "cache_status": self._cache_status()}


__all__ = ["IdentitySettingsStore", "SettingsDatabase"]
