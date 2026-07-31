"""Almacén aislado de reglas de Series Worker con CAS y snapshots por job."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_RULES_PATH = Path(__file__).with_name("default_rules.json")
DEFAULT_CONFIG_PATH = Path("/config/series-rules/reglas_series.json")
DEFAULT_SEED_PATH = Path("/seed/reglas_motor.json")
RULE_BLOCKS = ("entrada", "video", "audio", "subtitulos", "limpieza")
FLEXIBLE_MAPS = {"audio.codec_prioridad", "audio.titulos_codec"}


class RulesValidationError(ValueError):
    """Las reglas no cumplen el esquema cerrado de Series Worker."""


class RulesConflictError(RuntimeError):
    """El fingerprint CAS ya no coincide con las reglas vigentes."""

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("Otra petición guardó reglas más recientes.")
        self.current = current


def configured_rules_path() -> Path:
    value = (
        os.environ.get("SERIES_WORKER_RULES_PATH")
        or os.environ.get("SERIES_RULES_PATH")
        or str(DEFAULT_CONFIG_PATH)
    )
    return Path(value).resolve()


def configured_seed_path() -> Path:
    value = os.environ.get("SERIES_WORKER_SEED_RULES_PATH") or str(DEFAULT_SEED_PATH)
    return Path(value).resolve()


def _read_json(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise RulesValidationError(f"No existe {path}.")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RulesValidationError(f"No se pudo leer {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise RulesValidationError(f"{path.name} debe contener un objeto JSON.")
    return value


def _same_scalar_type(value: Any, template: Any) -> bool:
    if isinstance(template, bool):
        return isinstance(value, bool)
    if isinstance(template, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(template, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(template))


def _sanitize(source: Any, template: Any, path: str = "") -> Any:
    label = path or "rules"
    if isinstance(template, dict):
        if not isinstance(source, dict):
            raise RulesValidationError(f"{label} debe ser un objeto.")
        if path in FLEXIBLE_MAPS:
            sample = next(iter(template.values()), "")
            result: dict[str, Any] = {}
            for key, value in source.items():
                if not isinstance(key, str) or not key.strip():
                    raise RulesValidationError(f"{label} contiene una clave no válida.")
                if not _same_scalar_type(value, sample):
                    raise RulesValidationError(f"{label}.{key} tiene un tipo no válido.")
                result[key] = deepcopy(value)
            return result

        unknown = sorted(set(source) - set(template))
        if unknown:
            raise RulesValidationError(
                f"{label} contiene campos desconocidos: {', '.join(unknown)}."
            )
        return {
            key: _sanitize(value, template[key], f"{path}.{key}" if path else key)
            for key, value in source.items()
        }

    if isinstance(template, list):
        if not isinstance(source, list):
            raise RulesValidationError(f"{label} debe ser una lista.")
        if template:
            sample = template[0]
            for index, value in enumerate(source):
                if not _same_scalar_type(value, sample):
                    raise RulesValidationError(f"{label}[{index}] tiene un tipo no válido.")
        return deepcopy(source)

    if not _same_scalar_type(source, template):
        raise RulesValidationError(f"{label} tiene un tipo no válido.")
    return deepcopy(source)


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        result = deepcopy(base)
        for key, value in override.items():
            result[key] = _merge(result.get(key), value)
        return result
    return deepcopy(override)


def _validate_semantics(rules: dict[str, Any]) -> None:
    if tuple(rules) != RULE_BLOCKS and set(rules) != set(RULE_BLOCKS):
        raise RulesValidationError("Las reglas deben contener exactamente cinco bloques.")
    extensions = rules["entrada"]["extensiones_video"]
    if not extensions:
        raise RulesValidationError("entrada.extensiones_video no puede estar vacío.")
    if any(
        not item.startswith(".")
        or item != item.lower()
        or "/" in item
        or "\\" in item
        for item in extensions
    ):
        raise RulesValidationError("entrada.extensiones_video contiene una extensión no válida.")
    if len(set(extensions)) != len(extensions):
        raise RulesValidationError("entrada.extensiones_video contiene duplicados.")
    supported = {
        ".mkv", ".mp4", ".m4v", ".avi", ".mov",
        ".wmv", ".ts", ".m2ts", ".mts", ".webm",
    }
    unsupported = sorted(set(extensions) - supported)
    if unsupported:
        raise RulesValidationError(
            "entrada.extensiones_video contiene formatos no implementados: "
            + ", ".join(unsupported)
            + "."
        )
    strictly_positive_paths = (
        ("video", "pistas_exactas"),
        ("audio", "canales_convertir_ac3_desde"),
        ("subtitulos", "frases_maximo_unico_forzado"),
        ("subtitulos", "delay_audio", "frases_maximo"),
        ("limpieza", "capitulo_cada_segundos"),
    )
    for path in strictly_positive_paths:
        value: Any = rules
        for key in path:
            value = value[key]
        if value <= 0:
            raise RulesValidationError(f"{'.'.join(path)} debe ser mayor que cero.")
    minimum = rules["subtitulos"]["frases_descartar_hasta"]
    maximum = rules["subtitulos"]["frases_maximo_unico_forzado"]
    delay_maximum = rules["subtitulos"]["delay_audio"]["frases_maximo"]
    if minimum < 0:
        raise RulesValidationError(
            "subtitulos.frases_descartar_hasta no puede ser negativo."
        )
    if minimum >= maximum:
        raise RulesValidationError(
            "subtitulos.frases_descartar_hasta debe ser menor que frases_maximo_unico_forzado."
        )
    if minimum >= delay_maximum:
        raise RulesValidationError(
            "subtitulos.frases_descartar_hasta debe ser menor que delay_audio.frases_maximo."
        )
    if delay_maximum > maximum:
        raise RulesValidationError(
            "subtitulos.delay_audio.frases_maximo no puede superar frases_maximo_unico_forzado."
        )


def rules_fingerprint(rules: dict[str, Any]) -> str:
    encoded = json.dumps(
        rules, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _saved_at(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def _fsync_parent(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path.parent, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class RulesSnapshot:
    rules: dict[str, Any]
    fingerprint: str


class RulesStore:
    """Carga defaults propios y guarda overrides mediante compare-and-swap."""

    def __init__(
        self,
        config_path: Path | None = None,
        default_path: Path | None = None,
        seed_path: Path | None = None,
    ) -> None:
        self.config_path = Path(config_path or configured_rules_path())
        self.default_path = Path(default_path or DEFAULT_RULES_PATH)
        self.seed_path = Path(seed_path or configured_seed_path())
        self._lock = threading.RLock()
        self._defaults = _sanitize(
            _read_json(self.default_path, required=True),
            _read_json(self.default_path, required=True),
        )
        if set(self._defaults) != set(RULE_BLOCKS):
            raise RulesValidationError("Los defaults deben tener cinco bloques y ningún trailer.")
        _validate_semantics(self._defaults)
        persisted = _read_json(self.config_path, required=False)
        seeded = False
        if not persisted and not self.config_path.exists() and self.seed_path.is_file():
            seed_document = _read_json(self.seed_path, required=True)
            if isinstance(seed_document.get("rules"), dict):
                seed_document = seed_document["rules"]
            missing = [block for block in RULE_BLOCKS if block not in seed_document]
            if missing:
                raise RulesValidationError(
                    "La semilla de películas no contiene: " + ", ".join(missing) + "."
                )
            persisted = {block: deepcopy(seed_document[block]) for block in RULE_BLOCKS}
            seeded = True
        self._active = _sanitize(persisted, self._defaults)
        merged = _merge(self._defaults, self._active)
        _validate_semantics(merged)
        self._snapshot = RulesSnapshot(merged, rules_fingerprint(merged))
        self._seeded_from_movies = seeded
        if seeded:
            self._write_atomic(self._active)

    def snapshot(self) -> RulesSnapshot:
        with self._lock:
            return RulesSnapshot(deepcopy(self._snapshot.rules), self._snapshot.fingerprint)

    def payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "rules": deepcopy(self._snapshot.rules),
                "active": deepcopy(self._active),
                "defaults": deepcopy(self._defaults),
                "rules_path": "<CONFIG>/series-rules/reglas_series.json",
                "defaults_path": "<APP>/series-worker/default_rules.json",
                "fingerprint": self._snapshot.fingerprint,
                "saved_at": _saved_at(self.config_path),
                "applied": True,
                "applies_to": "new_jobs",
                "seeded_from_movies": self._seeded_from_movies,
            }

    def _backup(self) -> Path | None:
        if not self.config_path.is_file():
            return None
        backup_dir = self.config_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_%f")
        backup = backup_dir / f"reglas_series_{stamp}.json"
        shutil.copy2(self.config_path, backup)
        descriptor = os.open(backup, os.O_RDWR)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_parent(backup)
        return backup

    def _write_atomic(self, active: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_name(
            f".{self.config_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(active, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config_path)
            _fsync_parent(self.config_path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        rules = payload.get("rules") if isinstance(payload, dict) else None
        expected = payload.get("expected_fingerprint") if isinstance(payload, dict) else None
        if not isinstance(rules, dict):
            raise RulesValidationError("rules debe ser un objeto.")
        if not isinstance(expected, str) or not expected:
            raise RulesValidationError("expected_fingerprint es obligatorio.")

        with self._lock:
            if expected != self._snapshot.fingerprint:
                raise RulesConflictError(self.payload())
            active = _sanitize(rules, self._defaults)
            merged = _merge(self._defaults, active)
            _validate_semantics(merged)
            fingerprint = rules_fingerprint(merged)
            changed = fingerprint != self._snapshot.fingerprint or active != self._active
            backup = None
            if changed:
                backup = self._backup()
                self._write_atomic(active)
                self._active = active
                self._snapshot = RulesSnapshot(merged, fingerprint)
            result = self.payload()
            result["saved"] = changed
            result["backup"] = backup.name if backup else None
            return result


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_RULES_PATH",
    "DEFAULT_SEED_PATH",
    "RULE_BLOCKS",
    "RulesConflictError",
    "RulesSnapshot",
    "RulesStore",
    "RulesValidationError",
    "configured_rules_path",
    "configured_seed_path",
    "rules_fingerprint",
]
