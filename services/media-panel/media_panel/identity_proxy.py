"""Cliente HTTP pequeño y aislado para la configuracion de identidad ARR."""

import json
import math
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple


MIN_RESOLVER_BUDGET_MS = 100
MAX_RESOLVER_BUDGET_MS = 300_000
RESOLVER_PROXY_MARGIN_SECONDS = 5.0
MIN_RESOLVER_PROXY_TIMEOUT_SECONDS = 10.0
MAX_RESOLVER_PROXY_TIMEOUT_SECONDS = (
    MAX_RESOLVER_BUDGET_MS / 1_000 + RESOLVER_PROXY_MARGIN_SECONDS
)
IDENTITY_PROFILES = frozenset({"common", "movies", "tv"})


def _resolver_proxy_timeout(payload: Dict[str, object]) -> float:
    """Deja al motor consumir su presupuesto y un margen HTTP acotado."""

    budget: object = None
    rules = payload.get("rules")
    if isinstance(rules, dict):
        resolver = rules.get("resolver")
        if isinstance(resolver, dict):
            http = resolver.get("http")
            if isinstance(http, dict):
                budget = http.get("total_budget_ms", budget)
    try:
        numeric_budget = float(budget)
    except (TypeError, ValueError):
        return MAX_RESOLVER_PROXY_TIMEOUT_SECONDS
    if not math.isfinite(numeric_budget) or isinstance(budget, bool):
        return MAX_RESOLVER_PROXY_TIMEOUT_SECONDS
    bounded_budget = min(
        MAX_RESOLVER_BUDGET_MS,
        max(MIN_RESOLVER_BUDGET_MS, numeric_budget),
    )
    return min(
        MAX_RESOLVER_PROXY_TIMEOUT_SECONDS,
        max(
            MIN_RESOLVER_PROXY_TIMEOUT_SECONDS,
            bounded_budget / 1_000 + RESOLVER_PROXY_MARGIN_SECONDS,
        ),
    )


class IdentityProxy:
    def __init__(self, orchestrator_url: str) -> None:
        self.base_url = str(orchestrator_url or "").rstrip("/")

    @staticmethod
    def _settings_path(profile: Optional[str] = None, action: str = "") -> str:
        path = "/settings/identity"
        if profile is not None:
            normalized = str(profile or "").strip().lower()
            if normalized not in IDENTITY_PROFILES:
                raise ValueError("Perfil de identidad no valido.")
            path = f"{path}/{normalized}"
        if action:
            path = f"{path}/{str(action).strip('/')}"
        return path

    def get_rules(
        self, profile: Optional[str] = None
    ) -> Tuple[int, Dict[str, object]]:
        return self._request(self._settings_path(profile), None, 10)

    def save_rules(
        self,
        payload: Dict[str, object],
        profile: Optional[str] = None,
    ) -> Tuple[int, Dict[str, object]]:
        return self._request(self._settings_path(profile), payload, 25)

    def reset_rules(
        self,
        payload: Dict[str, object],
        profile: Optional[str] = None,
    ) -> Tuple[int, Dict[str, object]]:
        return self._request(self._settings_path(profile, "reset"), payload, 25)

    def clear_cache(
        self,
        payload: Dict[str, object],
        profile: Optional[str] = None,
    ) -> Tuple[int, Dict[str, object]]:
        return self._request(self._settings_path(profile, "cache/clear"), payload, 25)

    def test_parser(
        self,
        payload: Dict[str, object],
        profile: Optional[str] = None,
    ) -> Tuple[int, Dict[str, object]]:
        return self._request(self._settings_path(profile, "test-parser"), payload, 25)

    def test_resolver(
        self,
        payload: Dict[str, object],
        profile: Optional[str] = None,
    ) -> Tuple[int, Dict[str, object]]:
        return self._request(
            self._settings_path(profile, "test-resolver"),
            payload,
            _resolver_proxy_timeout(payload),
        )

    def _request(
        self,
        path: str,
        payload: Optional[Dict[str, object]],
        timeout: float,
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
            try:
                raw = error.read(4 * 1024 * 1024)
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
