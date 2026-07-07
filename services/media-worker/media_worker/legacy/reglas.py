import json
import os
from copy import deepcopy
from pathlib import Path


DEFAULT_PATH = Path(__file__).with_name("reglas_motor_default.json")
CONFIG_PATH = Path(os.environ.get("MEDIA_AUTO_REGLAS", "/config/reglas_motor.json"))


def _merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        salida = deepcopy(base)
        for clave, valor in override.items():
            salida[clave] = _merge(salida.get(clave), valor)
        return salida
    if override is None:
        return deepcopy(base)
    return deepcopy(override)


def _leer_json(path):
    try:
        path = Path(path)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def cargar_reglas():
    defaults = _leer_json(DEFAULT_PATH)
    overrides = _leer_json(CONFIG_PATH)
    return _merge(defaults, overrides)


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
