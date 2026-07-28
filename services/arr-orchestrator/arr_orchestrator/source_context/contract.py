"""Contrato estricto v1 para eventos de contexto de origen."""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict


SCHEMA_VERSION = 1
MAX_REQUEST_BODY_BYTES = 16 * 1024
MAX_SOURCE_TITLE_CHARS = 512
MAX_EVENT_ID_CHARS = 128
MAX_SOURCE_CHARS = 64
MAX_ROUTE_CHARS = 96
DESTINATIONS = {"movies", "tv"}
DELIVERY_STATES = {"intent", "accepted", "already_present", "failed"}
_SOURCE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,126}[A-Za-z0-9])?$")
_INFOHASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_ROUTE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_SENSITIVE_TITLE_RE = re.compile(
    r"(?i)(?:magnet:\?|\b[a-z][a-z0-9+.-]{0,31}://|urn:btih:|"
    r"(?:authorization|bearer|password|passwd|api[_-]?key|access[_-]?token|"
    r"token|credential|credencial)\s*[:=])"
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?:"
    r"(?<![a-z0-9])[a-z]:[\\/]"
    r"|(?<![\\])\\\\[^\\/\s]+[\\/]"
    r"|^/(?![/\\\s])[^/\s]+(?:$|\s)"
    r"|(?<![a-z0-9])/(?:[^/\s]+/)+[^/\s]+"
    r"|\.\.[\\/]"
    r")"
)
_EXPECTED_KEYS = {
    "schema_version",
    "source",
    "event_id",
    "infohash",
    "destination",
    "source_title",
    "route",
    "delivery_state",
    "created_at",
}


class SourceContextContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SourceContextEvent:
    source: str
    event_id: str
    infohash: str
    destination: str
    source_title: str
    route: str
    delivery_state: str
    created_at: str

    @classmethod
    def from_payload(cls, payload: object) -> "SourceContextEvent":
        if not isinstance(payload, dict):
            raise SourceContextContractError(
                "invalid_json", "El cuerpo debe ser un objeto JSON."
            )
        keys = set(payload)
        missing = sorted(_EXPECTED_KEYS - keys)
        unknown = sorted(keys - _EXPECTED_KEYS)
        if missing:
            raise SourceContextContractError(
                "missing_fields", f"Faltan campos obligatorios: {', '.join(missing)}."
            )
        if unknown:
            raise SourceContextContractError(
                "unknown_fields", f"Hay campos no admitidos: {', '.join(unknown)}."
            )
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            raise SourceContextContractError(
                "unsupported_schema", "schema_version debe ser 1."
            )

        source = _validated_string(
            payload.get("source"), "source", MAX_SOURCE_CHARS, _SOURCE_RE
        )
        event_id = _validated_string(
            payload.get("event_id"), "event_id", MAX_EVENT_ID_CHARS, _EVENT_ID_RE
        )
        infohash = _validated_string(
            payload.get("infohash"), "infohash", 40, _INFOHASH_RE
        ).lower()
        destination = _plain_string(payload.get("destination"), "destination", 16)
        if destination not in DESTINATIONS:
            raise SourceContextContractError(
                "invalid_destination", "destination debe ser movies o tv."
            )
        source_title = validate_source_title(payload.get("source_title"))
        route = _validated_string(
            payload.get("route"), "route", MAX_ROUTE_CHARS, _ROUTE_RE
        )
        delivery_state = _plain_string(
            payload.get("delivery_state"), "delivery_state", 32
        )
        if delivery_state not in DELIVERY_STATES:
            raise SourceContextContractError(
                "invalid_delivery_state",
                "delivery_state debe ser intent, accepted, already_present o failed.",
            )
        created_at = _timestamp(payload.get("created_at"))
        return cls(
            source=source,
            event_id=event_id,
            infohash=infohash,
            destination=destination,
            source_title=source_title,
            route=route,
            delivery_state=delivery_state,
            created_at=created_at,
        )

    def context_payload(self, received_at: float) -> Dict[str, object]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "infohash": self.infohash,
            "destination": self.destination,
            "source_title": self.source_title,
            "route": self.route,
            "delivery_state": self.delivery_state,
            "created_at": self.created_at,
            "received_at": float(received_at),
        }


def _plain_string(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise SourceContextContractError(
            f"invalid_{field}", f"{field} debe ser texto."
        )
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or _has_control(cleaned):
        raise SourceContextContractError(
            f"invalid_{field}", f"{field} no es valido."
        )
    return cleaned


def _validated_string(
    value: object,
    field: str,
    maximum: int,
    pattern: re.Pattern[str],
) -> str:
    cleaned = _plain_string(value, field, maximum)
    if not pattern.fullmatch(cleaned):
        raise SourceContextContractError(
            f"invalid_{field}", f"{field} no tiene un formato valido."
        )
    return cleaned


def validate_source_title(value: object) -> str:
    """Normaliza un titulo y bloquea datos que nunca deben persistirse."""

    if not isinstance(value, str):
        raise SourceContextContractError(
            "invalid_source_title", "source_title debe ser texto."
        )
    normalized = unicodedata.normalize("NFKC", value)
    if _has_control(normalized):
        raise SourceContextContractError(
            "invalid_source_title", "source_title contiene caracteres no validos."
        )
    cleaned = " ".join(normalized.split())
    if not cleaned or len(cleaned) > MAX_SOURCE_TITLE_CHARS:
        raise SourceContextContractError(
            "invalid_source_title", "source_title no es valido."
        )
    if _SENSITIVE_TITLE_RE.search(cleaned) or _ABSOLUTE_PATH_RE.search(cleaned):
        raise SourceContextContractError(
            "invalid_source_title", "source_title contiene datos no admitidos."
        )
    return cleaned


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise SourceContextContractError(
            "invalid_created_at", "created_at debe ser una fecha UTC ISO 8601."
        )
    text = value.strip()
    if not text:
        raise SourceContextContractError(
            "invalid_created_at", "created_at no es valido."
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise SourceContextContractError(
            "invalid_created_at", "created_at no es valido."
        ) from error
    if parsed.tzinfo is None:
        raise SourceContextContractError(
            "invalid_created_at", "created_at debe incluir zona horaria."
        )
    timestamp = parsed.astimezone(timezone.utc).timestamp()
    if timestamp <= 0:
        raise SourceContextContractError(
            "invalid_created_at", "created_at no es valido."
        )
    # Solo evita fechas evidentemente corruptas; la retencion usa reloj del servidor.
    if timestamp > time.time() + 24 * 60 * 60:
        raise SourceContextContractError(
            "invalid_created_at", "created_at esta demasiado adelantado."
        )
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)
