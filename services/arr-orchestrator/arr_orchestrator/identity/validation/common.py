"""Primitivas estrictas compartidas por la validacion de identidad."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


MAX_LIST_ITEMS = 256
MAX_TEXT_LENGTH = 512
MAX_PATTERN_LENGTH = 2_000
MAX_TMDB_ID = 2_147_483_647

LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2})?$")
REGION_RE = re.compile(r"^[A-Za-z]{2}$")
EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
TLD_RE = re.compile(r"^[A-Za-z]{2,24}$")
YEAR_RE = re.compile(r"^\d{4}$")
TMDB_ID_RE = re.compile(r"^\d+$")


class IdentityRulesValidationError(ValueError):
    """Error de formulario seguro para devolver por la API."""


def expect_object(value: object, label: str) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise IdentityRulesValidationError(f"{label} debe ser un objeto.")
    return value


def exact_keys(mapping: Dict[str, object], expected: set[str], label: str) -> None:
    unknown = sorted(set(mapping) - expected)
    missing = sorted(expected - set(mapping))
    if unknown:
        raise IdentityRulesValidationError(
            f"{label} contiene campos no permitidos: {', '.join(unknown)}."
        )
    if missing:
        raise IdentityRulesValidationError(
            f"{label} requiere estos campos: {', '.join(missing)}."
        )


def text(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
    maximum: int = MAX_TEXT_LENGTH,
) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > maximum:
        raise IdentityRulesValidationError(f"{label} no es texto valido.")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise IdentityRulesValidationError(f"{label} no puede estar vacio.")
    return normalized


def boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise IdentityRulesValidationError(f"{label} debe ser verdadero o falso.")
    return value


def integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise IdentityRulesValidationError(
            f"{label} debe ser un entero entre {minimum} y {maximum}."
        )
    return value


def number(value: object, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IdentityRulesValidationError(f"{label} debe ser numerico.")
    normalized = float(value)
    if not minimum <= normalized <= maximum:
        raise IdentityRulesValidationError(
            f"{label} debe estar entre {minimum:g} y {maximum:g}."
        )
    return normalized


def choice(value: object, label: str, allowed: Tuple[str, ...]) -> str:
    normalized = text(value, label)
    if normalized not in allowed:
        raise IdentityRulesValidationError(
            f"{label} debe ser uno de estos valores: {', '.join(allowed)}."
        )
    return normalized


def language(value: object, label: str) -> str:
    normalized = text(value, label)
    if not LANGUAGE_RE.fullmatch(normalized):
        raise IdentityRulesValidationError(f"{label} no es un idioma valido.")
    parts = normalized.split("-", 1)
    return parts[0].lower() + (f"-{parts[1].upper()}" if len(parts) == 2 else "")


def region(value: object, label: str) -> str:
    normalized = text(value, label)
    if not REGION_RE.fullmatch(normalized):
        raise IdentityRulesValidationError(f"{label} debe tener dos letras.")
    return normalized.upper()


def string_list(
    value: object,
    label: str,
    *,
    lowercase: bool = False,
    validator: Optional[re.Pattern[str]] = None,
) -> List[str]:
    if not isinstance(value, list):
        raise IdentityRulesValidationError(f"{label} debe ser una lista.")
    if len(value) > MAX_LIST_ITEMS:
        raise IdentityRulesValidationError(
            f"{label} admite como maximo {MAX_LIST_ITEMS} elementos."
        )
    result: List[str] = []
    seen = set()
    for index, item in enumerate(value, start=1):
        normalized = text(item, f"{label}[{index}]")
        if validator is not None and not validator.fullmatch(normalized):
            raise IdentityRulesValidationError(f"{label}[{index}] no tiene un formato valido.")
        if lowercase:
            normalized = normalized.lower()
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def regex(value: object, label: str) -> str:
    normalized = text(value, label, maximum=MAX_PATTERN_LENGTH)
    try:
        re.compile(normalized, re.IGNORECASE)
    except re.error as error:
        raise IdentityRulesValidationError(
            f"{label} no es una expresion regular valida: {error}."
        ) from error
    return normalized


def ocr_replacements(value: object, label: str) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        raise IdentityRulesValidationError(f"{label} debe ser una lista.")
    if len(value) > MAX_LIST_ITEMS:
        raise IdentityRulesValidationError(
            f"{label} admite como maximo {MAX_LIST_ITEMS} elementos."
        )
    result: List[Dict[str, str]] = []
    seen: Dict[str, str] = {}
    for index, item in enumerate(value, start=1):
        entry = expect_object(item, f"{label}[{index}]")
        exact_keys(entry, {"pattern", "replacement"}, f"{label}[{index}]")
        pattern = regex(entry.get("pattern"), f"{label}[{index}].pattern")
        replacement = text(
            entry.get("replacement"),
            f"{label}[{index}].replacement",
            allow_empty=True,
        )
        try:
            re.compile(pattern, re.IGNORECASE).sub(replacement, "")
        except re.error as error:
            raise IdentityRulesValidationError(
                f"{label}[{index}].replacement no es valido para su patron: {error}."
            ) from error
        if pattern in seen and seen[pattern] != replacement:
            raise IdentityRulesValidationError(
                f"{label}[{index}] contradice otro reemplazo para el mismo patron."
            )
        if pattern not in seen:
            seen[pattern] = replacement
            result.append({"pattern": pattern, "replacement": replacement})
    return result


def aliases(value: object, label: str) -> List[str]:
    if not isinstance(value, list):
        raise IdentityRulesValidationError(f"{label} debe ser una lista.")
    if len(value) > MAX_LIST_ITEMS:
        raise IdentityRulesValidationError(
            f"{label} admite como maximo {MAX_LIST_ITEMS} reglas."
        )
    result: List[str] = []
    seen: Dict[str, str] = {}
    for index, item in enumerate(value, start=1):
        raw = text(item, f"{label}[{index}]")
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) != 2 or not all(parts):
            raise IdentityRulesValidationError(
                f"{label}[{index}] debe usar el formato origen | destino."
            )
        source, destination = parts
        key = source.casefold()
        destination_key = destination.casefold()
        # Los autoalias no aportan evidencia independiente y en v1 podian
        # inflar artificialmente la corroboracion del mismo titulo.
        if key == destination_key:
            continue
        if key in seen and seen[key] != destination_key:
            raise IdentityRulesValidationError(
                f"{label}[{index}] contradice otro alias con el mismo origen."
            )
        if key not in seen:
            seen[key] = destination_key
            result.append(f"{source} | {destination}")
    return result


def forced_matches(value: object, label: str, category: str) -> List[str]:
    if not isinstance(value, list):
        raise IdentityRulesValidationError(f"{label} debe ser una lista.")
    if len(value) > MAX_LIST_ITEMS:
        raise IdentityRulesValidationError(
            f"{label} admite como maximo {MAX_LIST_ITEMS} reglas."
        )
    result: List[str] = []
    seen: Dict[Tuple[str, str], int] = {}
    maximum_year = datetime.now(timezone.utc).year + 5
    for index, item in enumerate(value, start=1):
        raw = text(item, f"{label}[{index}]")
        parts = [part.strip() for part in raw.split("|")]
        if category == "movies":
            valid = len(parts) == 3 and all(parts)
        else:
            valid = (len(parts) == 2 and all(parts)) or (
                len(parts) == 3 and bool(parts[0]) and bool(parts[2])
            )
        if not valid:
            expected = (
                "titulo | año | tmdb_id"
                if category == "movies"
                else "titulo | tmdb_id o titulo | año opcional | tmdb_id"
            )
            raise IdentityRulesValidationError(
                f"{label}[{index}] debe usar el formato {expected}."
            )
        title = parts[0]
        year = parts[1] if len(parts) == 3 and parts[1] else None
        tmdb_id = parts[-1]
        if year is not None and (
            not YEAR_RE.fullmatch(year) or not 1870 <= int(year) <= maximum_year
        ):
            raise IdentityRulesValidationError(f"{label}[{index}] contiene un año no valido.")
        if not TMDB_ID_RE.fullmatch(tmdb_id) or not 1 <= int(tmdb_id) <= MAX_TMDB_ID:
            raise IdentityRulesValidationError(f"{label}[{index}] contiene un TMDb ID no valido.")
        key = (title.casefold(), year or "")
        numeric_id = int(tmdb_id)
        if key in seen and seen[key] != numeric_id:
            raise IdentityRulesValidationError(
                f"{label}[{index}] contradice otra regla para el mismo titulo y año."
            )
        if key not in seen:
            seen[key] = numeric_id
            result.append(
                f"{title} | {year} | {numeric_id}"
                if year
                else f"{title} | {numeric_id}"
            )
    return result
