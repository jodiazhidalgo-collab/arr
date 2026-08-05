"""Canal HTTP TMDb con presupuesto, errores tipados y traza saneada."""

import time
from typing import Dict

import requests

from .models import ResolutionError, ResolverUnavailable


TMDB_BASE_URL = "https://api.themoviedb.org/3"


def get_json(
    session: requests.Session,
    token: str,
    endpoint: str,
    params: Dict[str, object],
    deadline: float,
    http_timeout: float,
    trace: Dict[str, object],
) -> Dict[str, object]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ResolverUnavailable("Presupuesto de tiempo TMDb agotado")
    timeout = min(http_timeout, remaining)
    trace_queries = trace.setdefault("queries", [])
    trace_entry: Dict[str, object] = {
        "endpoint": endpoint,
        "params": {
            key: value
            for key, value in params.items()
            if key in {"query", "language", "region", "year", "first_air_date_year", "page"}
        },
        "timeout_seconds": round(timeout, 3),
    }
    if isinstance(trace_queries, list):
        trace_queries.append(trace_entry)
    try:
        response = session.get(
            f"{TMDB_BASE_URL}{endpoint}",
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
    except requests.RequestException as error:
        trace_entry["error"] = type(error).__name__
        raise ResolverUnavailable(f"TMDb no disponible: {error}") from error
    trace_entry["status_code"] = int(response.status_code)
    if response.status_code == 429 or response.status_code >= 500:
        raise ResolverUnavailable(f"TMDb respondio HTTP {response.status_code}")
    if response.status_code == 404:
        raise ResolutionError(
            "TMDb no encontro el recurso solicitado",
            {"http_status": 404, "not_found": True},
        )
    if response.status_code >= 400:
        raise ResolverUnavailable(f"TMDb rechazo temporalmente la consulta: HTTP {response.status_code}")
    try:
        return dict(response.json())
    except (TypeError, ValueError) as error:
        raise ResolverUnavailable("TMDb devolvio JSON invalido") from error
