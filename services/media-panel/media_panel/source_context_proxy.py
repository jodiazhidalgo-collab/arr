"""Proxy autenticado y acotado para eventos source context."""

from __future__ import annotations

import hmac
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Tuple


MAX_REQUEST_BODY_BYTES = 16 * 1024
MAX_UPSTREAM_RESPONSE_BYTES = 64 * 1024


def read_source_context_token() -> str:
    token_file = os.environ.get("ARR_SOURCE_CONTEXT_TOKEN_FILE", "").strip()
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return os.environ.get("ARR_SOURCE_CONTEXT_TOKEN", "").strip()


class SourceContextProxy:
    def __init__(self, orchestrator_url: str, token: str) -> None:
        self.base_url = str(orchestrator_url or "").rstrip("/")
        self.token = str(token or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def authorized(self, header: object) -> bool:
        if not self.token or not isinstance(header, str):
            return False
        scheme, separator, supplied = header.partition(" ")
        if not separator or scheme.casefold() != "bearer":
            return False
        return hmac.compare_digest(supplied.strip(), self.token)

    def post_event(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        if not self.enabled:
            return 503, {
                "ok": False,
                "error": "source_context_disabled",
                "message": "La recepcion de contexto de origen no esta configurada.",
            }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/internal/source-context/events",
            data=data,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = int(response.status)
                raw = response.read(MAX_UPSTREAM_RESPONSE_BYTES)
        except urllib.error.HTTPError as error:
            status = int(error.code)
            try:
                raw = error.read(MAX_UPSTREAM_RESPONSE_BYTES)
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return 502, {
                "ok": False,
                "error": "orchestrator_unavailable",
                "message": f"Orquestador no disponible: {type(error).__name__}",
            }
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 502, {
                "ok": False,
                "error": "invalid_upstream_response",
                "message": "El orquestador devolvio una respuesta no valida.",
            }
        if not isinstance(result, dict):
            return 502, {
                "ok": False,
                "error": "invalid_upstream_response",
                "message": "El orquestador no devolvio un objeto JSON.",
            }
        return status, result
