"""Reglas editables y seguras para la resolucion y el renombrado con FileBot.

El almacen mantiene una instantanea en memoria para que el motor no tenga que
consultar SQLite por cada trabajo. Solo el guardado explicito persiste cambios.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol, Tuple


FILEBOT_RULES_SETTING_KEY = "filebot.rules"
FILEBOT_RULES_SCHEMA_VERSION = 1
FILEBOT_RULES_PATH = f"settings/{FILEBOT_RULES_SETTING_KEY}"

MOVIE_FILENAME_STYLES = ("title_year", "title_year_quality")
TV_FILENAME_STYLES = ("series_sxxexx", "series_sxxexx_title")
TV_EPISODE_ORDERS = ("Airdate", "DVD", "Absolute")

MAX_RULE_ITEMS = 128
MAX_RULE_TEXT_LENGTH = 255
MAX_TITLE_LENGTH = 200
MAX_TMDB_ID = 2_147_483_647

_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2})?$")
_REGION_RE = re.compile(r"^[A-Za-z]{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")
_TMDB_ID_RE = re.compile(r"^\d+$")


DEFAULT_FILEBOT_RULES: Dict[str, object] = {
    "schema_version": FILEBOT_RULES_SCHEMA_VERSION,
    "movies": {
        "language": "es-ES",
        "region": "ES",
        "query_aliases": [],
        "forced_matches": [],
        "filename_style": "title_year",
    },
    "tv": {
        "language": "es-ES",
        "query_aliases": [],
        "forced_matches": [],
        "filename_style": "series_sxxexx",
        "episode_order": "Airdate",
    },
}

# Estos valores se muestran para dejar claro que la interfaz no puede
# reconfigurar las barreras operativas del motor.
FILEBOT_SAFETY: Dict[str, object] = {
    "read_only": True,
    "databases": {"movies": "TheMovieDB", "tv": "TheMovieDB::TV"},
    "action": "move",
    "conflict": "skip",
    "strictness": "non-strict",
    "canonical_root_folders": {"movies": "{n} ({y})", "tv": "{n}"},
    "paths_editable": False,
    "custom_code_editable": False,
    "exec_editable": False,
}


class SettingsDatabase(Protocol):
    def get_setting(self, key: str) -> Optional[str]: ...

    def set_setting(self, key: str, value: str) -> None: ...


class RulesValidationError(ValueError):
    """Error de contrato del formulario, seguro para devolver por la API."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _expect_object(value: object, label: str) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise RulesValidationError(f"{label} debe ser un objeto.")
    return value


def _reject_unknown(mapping: Dict[str, object], allowed: set[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise RulesValidationError(
            f"{label} contiene campos no permitidos: {', '.join(unknown)}."
        )


def _normalize_language(value: object, label: str) -> str:
    if not isinstance(value, str) or not _LANGUAGE_RE.fullmatch(value.strip()):
        raise RulesValidationError(f"{label} no es un idioma valido.")
    parts = value.strip().split("-", 1)
    return parts[0].lower() + (f"-{parts[1].upper()}" if len(parts) == 2 else "")


def _normalize_region(value: object, label: str) -> str:
    if not isinstance(value, str) or not _REGION_RE.fullmatch(value.strip()):
        raise RulesValidationError(f"{label} debe tener dos letras.")
    return value.strip().upper()


def _normalize_choice(value: object, allowed: Tuple[str, ...], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise RulesValidationError(
            f"{label} debe ser uno de estos valores: {', '.join(allowed)}."
        )
    return value


def _normalize_aliases(value: object, label: str) -> List[str]:
    if not isinstance(value, list):
        raise RulesValidationError(f"{label} debe ser una lista.")
    if len(value) > MAX_RULE_ITEMS:
        raise RulesValidationError(f"{label} admite como maximo {MAX_RULE_ITEMS} reglas.")
    normalized: List[str] = []
    seen: Dict[str, str] = {}
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, str) or "\x00" in entry or len(entry) > MAX_RULE_TEXT_LENGTH:
            raise RulesValidationError(f"{label}[{index}] no es texto valido.")
        parts = [part.strip() for part in entry.split("|")]
        if len(parts) != 2 or not all(parts):
            raise RulesValidationError(
                f"{label}[{index}] debe usar el formato origen | destino."
            )
        rule = f"{parts[0]} | {parts[1]}"
        selector = parts[0].casefold()
        destination = parts[1].casefold()
        if selector in seen and seen[selector] != destination:
            raise RulesValidationError(
                f"{label}[{index}] contradice otro alias con el mismo origen."
            )
        if selector not in seen:
            seen[selector] = destination
            normalized.append(rule)
    return normalized


def _normalize_forced_matches(value: object, label: str, category: str) -> List[str]:
    if not isinstance(value, list):
        raise RulesValidationError(f"{label} debe ser una lista.")
    if len(value) > MAX_RULE_ITEMS:
        raise RulesValidationError(f"{label} admite como maximo {MAX_RULE_ITEMS} reglas.")
    normalized: List[str] = []
    seen: Dict[Tuple[str, str], int] = {}
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, str) or "\x00" in entry or len(entry) > MAX_RULE_TEXT_LENGTH:
            raise RulesValidationError(f"{label}[{index}] no es texto valido.")
        parts = [part.strip() for part in entry.split("|")]
        valid_shape = (
            len(parts) == 3 and bool(parts[0]) and bool(parts[1]) and bool(parts[2])
            if category == "movies"
            else len(parts) == 2 and all(parts)
            or len(parts) == 3 and bool(parts[0]) and bool(parts[2])
        )
        if not valid_shape:
            raise RulesValidationError(
                f"{label}[{index}] debe usar "
                + (
                    "titulo | año | tmdb_id."
                    if category == "movies"
                    else "titulo | tmdb_id o titulo | año opcional | tmdb_id."
                )
            )
        title = parts[0]
        if len(title) > MAX_TITLE_LENGTH:
            raise RulesValidationError(f"{label}[{index}] tiene un titulo demasiado largo.")
        year = parts[1] if len(parts) == 3 and parts[1] else None
        tmdb_id = parts[-1]
        maximum_year = datetime.now(timezone.utc).year + 5
        if year is not None and (
            not _YEAR_RE.fullmatch(year) or not (1870 <= int(year) <= maximum_year)
        ):
            raise RulesValidationError(f"{label}[{index}] contiene un año no valido.")
        if not _TMDB_ID_RE.fullmatch(tmdb_id) or not (1 <= int(tmdb_id) <= MAX_TMDB_ID):
            raise RulesValidationError(f"{label}[{index}] contiene un TMDb ID no valido.")
        rule = f"{title} | {year} | {int(tmdb_id)}" if year else f"{title} | {int(tmdb_id)}"
        selector = (title.casefold(), year or "")
        selected_tmdb_id = int(tmdb_id)
        if selector in seen and seen[selector] != selected_tmdb_id:
            raise RulesValidationError(
                f"{label}[{index}] contradice otra regla para el mismo titulo y año."
            )
        if selector not in seen:
            seen[selector] = selected_tmdb_id
            normalized.append(rule)
    return normalized


def normalize_filebot_rules(value: object) -> Dict[str, object]:
    rules = _expect_object(value, "rules")
    _reject_unknown(rules, {"schema_version", "movies", "tv"}, "rules")
    version = rules.get("schema_version")
    if isinstance(version, bool) or version != FILEBOT_RULES_SCHEMA_VERSION:
        raise RulesValidationError(
            f"rules.schema_version debe ser {FILEBOT_RULES_SCHEMA_VERSION}."
        )

    movies = _expect_object(rules.get("movies"), "rules.movies")
    _reject_unknown(
        movies,
        {"language", "region", "query_aliases", "forced_matches", "filename_style"},
        "rules.movies",
    )
    tv = _expect_object(rules.get("tv"), "rules.tv")
    _reject_unknown(
        tv,
        {
            "language",
            "query_aliases",
            "forced_matches",
            "filename_style",
            "episode_order",
        },
        "rules.tv",
    )

    return {
        "schema_version": FILEBOT_RULES_SCHEMA_VERSION,
        "movies": {
            "language": _normalize_language(movies.get("language"), "rules.movies.language"),
            "region": _normalize_region(movies.get("region"), "rules.movies.region"),
            "query_aliases": _normalize_aliases(
                movies.get("query_aliases"), "rules.movies.query_aliases"
            ),
            "forced_matches": _normalize_forced_matches(
                movies.get("forced_matches"), "rules.movies.forced_matches", "movies"
            ),
            "filename_style": _normalize_choice(
                movies.get("filename_style"),
                MOVIE_FILENAME_STYLES,
                "rules.movies.filename_style",
            ),
        },
        "tv": {
            "language": _normalize_language(tv.get("language"), "rules.tv.language"),
            "query_aliases": _normalize_aliases(
                tv.get("query_aliases"), "rules.tv.query_aliases"
            ),
            "forced_matches": _normalize_forced_matches(
                tv.get("forced_matches"), "rules.tv.forced_matches", "tv"
            ),
            "filename_style": _normalize_choice(
                tv.get("filename_style"),
                TV_FILENAME_STYLES,
                "rules.tv.filename_style",
            ),
            "episode_order": _normalize_choice(
                tv.get("episode_order"), TV_EPISODE_ORDERS, "rules.tv.episode_order"
            ),
        },
    }


def resolver_fingerprint(rules: Dict[str, object]) -> str:
    relevant: Dict[str, object] = {"schema_version": rules["schema_version"]}
    for category in ("movies", "tv"):
        category_rules = dict(rules[category])  # type: ignore[arg-type]
        keys = (
            ("language", "region", "query_aliases", "forced_matches")
            if category == "movies"
            else ("language", "query_aliases", "forced_matches")
        )
        relevant[category] = {
            key: category_rules[key]
            for key in keys
        }
    canonical = json.dumps(
        relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class FileBotSettingsStore:
    """Almacen persistente con actualizacion optimista y lectura desde memoria."""

    def __init__(
        self,
        database: SettingsDatabase,
        default_language: str = "es-ES",
        default_region: str = "ES",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._database = database
        self._logger = logger or logging.getLogger("arr-orchestrator.filebot-settings")
        self._lock = threading.RLock()
        defaults = copy.deepcopy(DEFAULT_FILEBOT_RULES)
        for category in ("movies", "tv"):
            defaults[category]["language"] = default_language  # type: ignore[index]
        defaults["movies"]["region"] = default_region  # type: ignore[index]
        self._rules = normalize_filebot_rules(defaults)
        self._revision = 0
        self._saved_at: Optional[str] = None
        self._load()

    def _load(self) -> None:
        stored = self._database.get_setting(FILEBOT_RULES_SETTING_KEY)
        if stored is None:
            return
        try:
            payload = json.loads(stored)
            envelope = _expect_object(payload, "filebot.rules")
            if "rules" in envelope:
                _reject_unknown(envelope, {"rules", "revision", "saved_at"}, "filebot.rules")
                rules = normalize_filebot_rules(envelope.get("rules"))
                revision = envelope.get("revision")
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                    raise RulesValidationError("filebot.rules.revision no es valida.")
                saved_at = envelope.get("saved_at")
                if saved_at is not None and not isinstance(saved_at, str):
                    raise RulesValidationError("filebot.rules.saved_at no es valido.")
            else:
                # Compatibilidad defensiva con una primera version que pudiera
                # haber guardado solo las reglas.
                rules = normalize_filebot_rules(envelope)
                revision = 1
                saved_at = None
            self._rules = rules
            self._revision = revision
            self._saved_at = saved_at
        except (json.JSONDecodeError, RulesValidationError, TypeError, ValueError) as error:
            self._logger.warning(
                "Reglas FileBot persistidas invalidas; se conservan los valores seguros: %s",
                error,
            )

    def _response_locked(self, *, ok: bool = True) -> Dict[str, object]:
        return {
            "ok": ok,
            "rules": copy.deepcopy(self._rules),
            "revision": self._revision,
            "saved_at": self._saved_at,
            "resolver_fingerprint": resolver_fingerprint(self._rules),
            "rules_path": FILEBOT_RULES_PATH,
            "safety": copy.deepcopy(FILEBOT_SAFETY),
        }

    def payload(self) -> Dict[str, object]:
        """Contrato completo y serializable de la API."""

        with self._lock:
            return self._response_locked()

    def snapshot(self) -> Dict[str, object]:
        """Copia de reglas para capturarla en un trabajo sin leer SQLite."""

        with self._lock:
            return copy.deepcopy(self._rules)

    def job_snapshot(self) -> Dict[str, object]:
        """Contexto coherente para un trabajo, capturado bajo un unico lock."""

        with self._lock:
            return {
                "revision": self._revision,
                "saved_at": self._saved_at,
                "resolver_fingerprint": resolver_fingerprint(self._rules),
                "rules": copy.deepcopy(self._rules),
            }

    def rules_snapshot(self) -> Dict[str, object]:
        """Alias explicito para consumidores que prefieran ese nombre."""

        return self.snapshot()

    def update(self, payload: object) -> Dict[str, object]:
        try:
            request = _expect_object(payload, "payload")
            _reject_unknown(request, {"rules", "expected_revision"}, "payload")
            if "rules" not in request or "expected_revision" not in request:
                raise RulesValidationError("payload requiere rules y expected_revision.")
            expected_revision = request.get("expected_revision")
            if (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or expected_revision < 0
            ):
                raise RulesValidationError("expected_revision debe ser un entero no negativo.")
            normalized = normalize_filebot_rules(request.get("rules"))
        except RulesValidationError as error:
            with self._lock:
                result = self._response_locked(ok=False)
            result.update({"error": "invalid_rules", "message": str(error)})
            return result
        with self._lock:
            if expected_revision != self._revision:
                result = self._response_locked(ok=False)
                result.update(
                    {
                        "error": "revision_conflict",
                        "message": "Las reglas cambiaron en otra ventana; recarga antes de guardar.",
                        "expected_revision": expected_revision,
                        "current_revision": self._revision,
                    }
                )
                return result
            if normalized == self._rules:
                result = self._response_locked()
                result["saved"] = False
                return result

            saved_at = _utc_now()
            revision = self._revision + 1
            serialized = json.dumps(
                {"rules": normalized, "revision": revision, "saved_at": saved_at},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            try:
                self._database.set_setting(FILEBOT_RULES_SETTING_KEY, serialized)
            except Exception:
                self._logger.exception("No se pudieron guardar las reglas FileBot")
                result = self._response_locked(ok=False)
                result.update(
                    {
                        "error": "persistence_failed",
                        "message": "No se pudieron guardar las reglas FileBot.",
                    }
                )
                return result

            self._rules = normalized
            self._revision = revision
            self._saved_at = saved_at
            result = self._response_locked()
            result["saved"] = True
            return result


# Nombre anterior conservado para no romper importaciones durante el despliegue
# coordinado del motor y las pruebas.
FileBotRulesStore = FileBotSettingsStore
