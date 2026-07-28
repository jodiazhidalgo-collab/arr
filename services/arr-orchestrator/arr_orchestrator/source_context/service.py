"""Servicio pequeño para validar y persistir eventos source context."""

from __future__ import annotations

from typing import Callable, Dict, Optional, Tuple

from ..db import Database
from .contract import SCHEMA_VERSION, SourceContextContractError, SourceContextEvent
from .store import SourceContextStore


class SourceContextService:
    def __init__(
        self,
        database: Database,
        identity_snapshot_provider: Optional[Callable[[], Dict[str, object]]] = None,
    ) -> None:
        self.store = SourceContextStore(database)
        self.identity_snapshot_provider = identity_snapshot_provider

    def handle(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        try:
            event = SourceContextEvent.from_payload(payload)
        except SourceContextContractError as error:
            return 400, {
                "ok": False,
                "error": error.code,
                "message": error.message,
                "schema_version": SCHEMA_VERSION,
            }
        identity_context = (
            self.identity_snapshot_provider()
            if self.identity_snapshot_provider is not None
            else {}
        )
        result = self.store.apply(event, identity_context)
        response: Dict[str, object] = {
            "ok": result.action not in {"destination_conflict", "event_conflict"},
            "schema_version": SCHEMA_VERSION,
            "action": result.action,
            "job_id": result.job_id,
            "infohash": event.infohash,
            "destination": event.destination,
            "context_count": result.context_count,
        }
        if result.action in {"destination_conflict", "event_conflict"}:
            response.update(
                {
                    "error": result.action,
                }
            )
            if result.action == "destination_conflict":
                response["current_destination"] = result.conflict_destination
            return 409, response
        return (201 if result.created else 200), response
