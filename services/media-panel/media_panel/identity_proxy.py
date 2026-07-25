"""Cliente HTTP pequeño y aislado para la configuracion de identidad ARR."""

import json
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple


class IdentityProxy:
    def __init__(self, orchestrator_url: str) -> None:
        self.base_url = str(orchestrator_url or "").rstrip("/")

    def get_rules(self) -> Tuple[int, Dict[str, object]]:
        return self._request("/settings/identity", None, 10)

    def save_rules(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._request("/settings/identity", payload, 25)

    def reset_rules(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._request("/settings/identity/reset", payload, 25)

    def clear_cache(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._request("/settings/identity/cache/clear", payload, 25)

    def test_parser(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._request("/settings/identity/test-parser", payload, 25)

    def test_resolver(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._request("/settings/identity/test-resolver", payload, 90)

    def _request(
        self,
        path: str,
        payload: Optional[Dict[str, object]],
        timeout: int,
    ) -> Tuple[int, Dict[str, object]]:
        data = None
        headers = {"Accept": "application/json"}
        method = "GET"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
            method = "POST"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(response.status)
                raw = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            status = int(error.code)
            raw = error.read(4 * 1024 * 1024)
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
