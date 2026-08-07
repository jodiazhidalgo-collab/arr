import hashlib
import json
import os
import shutil
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_PATH = Path(__file__).with_name("reglas_motor_default.json")
CONFIG_PATH = Path(os.environ.get("MEDIA_AUTO_REGLAS", "/config/reglas_motor.json"))
FLEXIBLE_MAP_PATHS = {
    "audio.codec_prioridad",
    "audio.titulos_codec",
}


class RulesValidationError(ValueError):
    pass


class RulesConflictError(RuntimeError):
    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("Otra ventana guardo reglas mas recientes.")
        self.current = current


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        salida = deepcopy(base)
        for clave, valor_actual in override.items():
            salida[clave] = _merge(salida.get(clave), valor_actual)
        return salida
    if override is None:
        return deepcopy(base)
    return deepcopy(override)


def _leer_json(path: Path, *, required: bool = False) -> dict[str, Any]:
    try:
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
            raise RulesValidationError(f"{path.name} debe contener un objeto JSON.")
        if required:
            raise RulesValidationError(f"No existe {path}.")
    except RulesValidationError:
        raise
    except Exception as error:
        if required:
            raise RulesValidationError(f"No se pudo leer {path.name}: {error}") from error
    return {}


def _same_scalar_type(value: Any, template: Any) -> bool:
    if isinstance(template, bool):
        return isinstance(value, bool)
    if isinstance(template, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(template, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(template))


def _validate_and_sanitize(
    source: Any,
    template: Any,
    path: str = "",
) -> Any:
    label = path or "rules"
    if isinstance(template, dict):
        if not isinstance(source, dict):
            raise RulesValidationError(f"{label} debe ser un objeto.")
        if path in FLEXIBLE_MAP_PATHS:
            sample = next(iter(template.values()), "")
            result = {}
            for key, value in source.items():
                if not isinstance(key, str) or not key.strip():
                    raise RulesValidationError(f"{label} contiene una clave no valida.")
                if not _same_scalar_type(value, sample):
                    raise RulesValidationError(f"{label}.{key} tiene un tipo no valido.")
                result[key] = deepcopy(value)
            return result

        unknown = sorted(set(source) - set(template))
        if unknown:
            raise RulesValidationError(
                f"{label} contiene campos desconocidos: {', '.join(unknown)}."
            )
        result = {}
        for key, value in source.items():
            child_path = f"{path}.{key}" if path else key
            result[key] = _validate_and_sanitize(value, template[key], child_path)
        return result

    if isinstance(template, list):
        if not isinstance(source, list):
            raise RulesValidationError(f"{label} debe ser una lista.")
        if template:
            sample = template[0]
            for index, value in enumerate(source):
                if not _same_scalar_type(value, sample):
                    raise RulesValidationError(
                        f"{label}[{index}] tiene un tipo no valido."
                    )
        return deepcopy(source)

    if not _same_scalar_type(source, template):
        raise RulesValidationError(f"{label} tiene un tipo no valido.")
    return deepcopy(source)


def _fingerprint(rules: dict[str, Any]) -> str:
    canonical = json.dumps(
        rules,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_semantics(rules: dict[str, Any]) -> None:
    if int(rules.get("video", {}).get("pistas_exactas", 0) or 0) != 1:
        raise RulesValidationError("video.pistas_exactas debe ser 1.")
    mode = str(rules.get("subtitulos", {}).get("ocr_imagen_modo") or "")
    if mode not in {"solo_forzados_cortos", "desactivado"}:
        raise RulesValidationError(
            "subtitulos.ocr_imagen_modo debe ser solo_forzados_cortos o desactivado."
        )


def _saved_at(path: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


@dataclass(frozen=True)
class RulesSnapshot:
    rules: dict[str, Any]
    fingerprint: str


class MediaRulesStore:
    def __init__(
        self,
        config_path: Optional[Path] = None,
        default_path: Optional[Path] = None,
    ) -> None:
        self.config_path = Path(config_path or CONFIG_PATH)
        self.default_path = Path(default_path or DEFAULT_PATH)
        self._lock = threading.RLock()
        self._defaults = _leer_json(self.default_path, required=True)
        raw_active = _leer_json(self.config_path)
        self._active = _validate_and_sanitize(raw_active, self._defaults)
        merged = _merge(self._defaults, self._active)
        _validate_semantics(merged)
        self._snapshot = RulesSnapshot(merged, _fingerprint(merged))

    def snapshot(self) -> RulesSnapshot:
        with self._lock:
            return RulesSnapshot(
                deepcopy(self._snapshot.rules),
                self._snapshot.fingerprint,
            )

    def payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "rules": deepcopy(self._snapshot.rules),
                "active": deepcopy(self._active),
                "defaults": deepcopy(self._defaults),
                "rules_path": str(self.config_path),
                "defaults_path": str(self.default_path),
                "fingerprint": self._snapshot.fingerprint,
                "saved_at": _saved_at(self.config_path),
                "applied": True,
                "applies_to": "new_jobs",
            }

    def _backup(self) -> Optional[Path]:
        if not self.config_path.is_file():
            return None
        backup_dir = self.config_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_%f")
        backup = backup_dir / f"reglas_motor_{stamp}.json"
        shutil.copy2(self.config_path, backup)
        return backup

    def _write_atomic(self, rules: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_name(
            f".{self.config_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(rules, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config_path)
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
            sanitized = _validate_and_sanitize(rules, self._defaults)
            merged = _merge(self._defaults, sanitized)
            _validate_semantics(merged)
            next_fingerprint = _fingerprint(merged)
            changed = next_fingerprint != self._snapshot.fingerprint
            backup = None
            if changed or sanitized != self._active:
                backup = self._backup()
                self._write_atomic(sanitized)
                self._active = sanitized
                self._snapshot = RulesSnapshot(merged, next_fingerprint)

            result = self.payload()
            result["saved"] = changed
            result["backup"] = backup.name if backup else None
            return result


RULES_STORE = MediaRulesStore()
_BOUND_RULES: ContextVar[Optional[RulesSnapshot]] = ContextVar(
    "media_worker_rules_snapshot",
    default=None,
)


@contextmanager
def usar_reglas(snapshot: RulesSnapshot):
    bound = RulesSnapshot(deepcopy(snapshot.rules), snapshot.fingerprint)
    token = _BOUND_RULES.set(bound)
    try:
        yield bound
    finally:
        _BOUND_RULES.reset(token)


def cargar_reglas() -> dict[str, Any]:
    bound = _BOUND_RULES.get()
    snapshot = bound if bound is not None else RULES_STORE.snapshot()
    return deepcopy(snapshot.rules)


def huella_reglas() -> str:
    bound = _BOUND_RULES.get()
    return bound.fingerprint if bound is not None else RULES_STORE.snapshot().fingerprint


class _RulesProxy(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return cargar_reglas()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(cargar_reglas())

    def __len__(self) -> int:
        return len(cargar_reglas())


REGLAS_ACTUALES: Mapping[str, Any] = _RulesProxy()


def valor(ruta, defecto=None):
    actual = cargar_reglas()
    for parte in str(ruta).split("."):
        if isinstance(actual, dict) and parte in actual:
            actual = actual[parte]
        else:
            return defecto
    return actual


def lista(ruta, defecto=None):
    dato = valor(ruta, defecto or [])
    if isinstance(dato, list):
        return dato
    return defecto or []


def entero(ruta, defecto=0):
    try:
        return int(valor(ruta, defecto))
    except Exception:
        return defecto


def flotante(ruta, defecto=0.0):
    try:
        return float(valor(ruta, defecto))
    except Exception:
        return defecto


def booleano(ruta, defecto=False):
    dato = valor(ruta, defecto)
    if isinstance(dato, bool):
        return dato
    return str(dato).strip().lower() in {"1", "true", "si", "sí", "yes", "on"}
