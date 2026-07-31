"""Cliente HTTP independiente para el servicio Series Worker."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

from .diagnostic_sanitizer import sanitize_for_export, sanitize_text


PROCESS_ENDPOINT = "/process-series"
HEALTH_ENDPOINT = "/health"
STATUS_TIMEOUT_SECONDS = 10
PROCESS_TIMEOUT_SECONDS = 30


class SeriesWorkerError(RuntimeError):
    """Error tipado devuelto por Series Worker."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        status_code: Optional[int],
        error_code: str,
        result: Optional[Dict[str, object]] = None,
        retryable: bool = False,
    ) -> None:
        safe_code = _safe_error_code(error_code)
        super().__init__(_safe_error_text(message) or safe_code)
        self.endpoint = endpoint
        self.status_code = status_code
        self.error_code = safe_code
        self.result = _safe_mapping(result)
        self.retryable = bool(retryable)


class SeriesWorkerBusy(SeriesWorkerError):
    """El bloqueo pesado está ocupado por otro trabajo."""


class SeriesWorkerConflict(SeriesWorkerError):
    """El mismo job_id ya existe con un payload distinto."""


class SeriesWorkerBadRequest(SeriesWorkerError):
    """El worker rechazó el payload o sus rutas."""


class SeriesWorkerUnavailable(SeriesWorkerError):
    """Reglas, herramientas o publicación atómica no disponibles."""


class SeriesWorkerTransportError(SeriesWorkerError):
    """No se pudo completar el intercambio HTTP con Series Worker."""


def _safe_mapping(value: object) -> Optional[Dict[str, object]]:
    if not isinstance(value, dict):
        return None
    sanitized = _sanitize_error_value(sanitize_for_export(value))
    return sanitized if isinstance(sanitized, dict) else None


def _safe_error_code(value: object) -> str:
    code = str(value or "").strip()
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", code):
        return code
    return "series_worker_error"


def _safe_error_text(value: object) -> str:
    text = sanitize_text(value)
    text = re.sub(r"(?i)https?://[^\s,;]+", "<URL_REDACTED>", text)
    text = re.sub(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s,;]+", "<PATH_REDACTED>", text)
    text = re.sub(r"(?<![>\w])/(?:[^/\s]+/)*[^\s,;]*", "<PATH_REDACTED>", text)
    return text


def _sanitize_error_value(value: object) -> object:
    if isinstance(value, str):
        return _safe_error_text(value)
    if isinstance(value, list):
        return [_sanitize_error_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_error_value(item) for key, item in value.items()}
    return value


class SeriesWorkerClient:
    """Canal exclusivo del orquestador hacia Series Worker."""

    def __init__(
        self,
        base_url: str,
        callback_base_url: str = "http://arr-orchestrator:8787",
        timeout_seconds: int = PROCESS_TIMEOUT_SECONDS,
        status_timeout_seconds: int = STATUS_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.callback_base_url = callback_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.status_timeout_seconds = status_timeout_seconds

    def health(self) -> Dict[str, object]:
        try:
            response = requests.get(
                f"{self.base_url}{HEALTH_ENDPOINT}",
                timeout=self.status_timeout_seconds,
            )
        except requests.RequestException as error:
            raise self._transport_error(HEALTH_ENDPOINT, error) from error

        data = self._response_json(response, HEALTH_ENDPOINT)
        if response.status_code >= 400 or data.get("ok") is False:
            raise self._response_error(HEALTH_ENDPOINT, response.status_code, data)
        if (
            response.status_code != 200
            or data.get("ok") is not True
            or data.get("status") != "ok"
            or data.get("service") != "series-worker"
            or not isinstance(data.get("checks"), dict)
        ):
            raise self._invalid_response(HEALTH_ENDPOINT, response.status_code, data)
        sanitized = _safe_mapping(data)
        if sanitized is None:
            raise self._invalid_response(HEALTH_ENDPOINT, response.status_code, data)
        return sanitized

    def version(self) -> str:
        return str(self.health()["status"])

    def process_series(
        self,
        job_id: str,
        job_root: Path,
        source_root: Path,
        final_root: Path,
        review_root: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        payload = self._series_payload(
            job_id,
            job_root,
            source_root,
            final_root,
            review_root,
            reports_root,
        )
        return self._post_process(job_id, payload)

    def job_status(self, job_id: str) -> Dict[str, object]:
        endpoint = self._status_endpoint(job_id)
        try:
            response = requests.get(
                f"{self.base_url}{endpoint}",
                params={"kind": "series"},
                timeout=self.status_timeout_seconds,
            )
        except requests.RequestException as error:
            raise self._transport_error(endpoint, error) from error

        data = self._response_json(response, endpoint)
        status = str(data.get("status") or "")
        if response.status_code == 404 and status == "not_found":
            return self._validated_response(
                data,
                endpoint=endpoint,
                job_id=job_id,
                allowed_statuses={"not_found"},
            )
        if response.status_code >= 400 or data.get("ok") is False:
            raise self._response_error(endpoint, response.status_code, data)
        if response.status_code == 202 and status in {"active", "recoverable"}:
            return self._validated_response(
                data,
                endpoint=endpoint,
                job_id=job_id,
                allowed_statuses={"active", "recoverable"},
            )
        if response.status_code == 200 and status == "terminal":
            return self._validated_response(
                data,
                endpoint=endpoint,
                job_id=job_id,
                allowed_statuses={"terminal"},
            )
        raise self._invalid_response(endpoint, response.status_code, data)

    def preview_process_series(
        self,
        job_id: str,
        job_root: Path,
        source_root: Path,
        final_root: Path,
        review_root: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return {
            "method": "POST",
            "service": "series-worker",
            "endpoint": PROCESS_ENDPOINT,
            "payload": self._series_payload(
                job_id,
                job_root,
                source_root,
                final_root,
                review_root,
                reports_root,
            ),
            "timeout_sec": self.timeout_seconds,
        }

    def _series_payload(
        self,
        job_id: str,
        job_root: Path,
        source_root: Path,
        final_root: Path,
        review_root: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return {
            "job_id": str(job_id),
            "job_root": str(job_root),
            "source_root": str(source_root),
            "final_root": str(final_root),
            "review_root": str(review_root),
            "reports_root": str(reports_root),
            "callback_url": self._callback_url(job_id),
        }

    def _callback_url(self, job_id: str) -> str:
        encoded = quote(str(job_id), safe="")
        return f"{self.callback_base_url}/jobs/{encoded}/events"

    @staticmethod
    def _status_endpoint(job_id: str) -> str:
        return f"/jobs/{quote(str(job_id), safe='')}/status"

    def _post_process(
        self,
        job_id: str,
        payload: Dict[str, object],
    ) -> Dict[str, object]:
        try:
            response = requests.post(
                f"{self.base_url}{PROCESS_ENDPOINT}",
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as error:
            return self._recover_post_timeout(job_id, error)
        except requests.RequestException as error:
            raise self._transport_error(PROCESS_ENDPOINT, error) from error

        data = self._response_json(response, PROCESS_ENDPOINT)
        status = str(data.get("status") or "")
        if response.status_code >= 400 or data.get("ok") is False:
            raise self._response_error(PROCESS_ENDPOINT, response.status_code, data)
        if response.status_code == 202 and status in {"accepted", "active"}:
            return self._validated_response(
                data,
                endpoint=PROCESS_ENDPOINT,
                job_id=job_id,
                allowed_statuses={"accepted", "active"},
            )
        if response.status_code == 200 and status == "terminal":
            return self._validated_response(
                data,
                endpoint=PROCESS_ENDPOINT,
                job_id=job_id,
                allowed_statuses={"terminal"},
            )
        raise self._invalid_response(PROCESS_ENDPOINT, response.status_code, data)

    def _recover_post_timeout(
        self,
        job_id: str,
        timeout_error: requests.Timeout,
    ) -> Dict[str, object]:
        try:
            status = self.job_status(job_id)
        except SeriesWorkerError as status_error:
            raise SeriesWorkerTransportError(
                "Series Worker agotó el plazo y no se pudo confirmar el estado",
                endpoint=PROCESS_ENDPOINT,
                status_code=None,
                error_code="series_worker_timeout_status_unknown",
                result={"status_check_error": status_error.error_code},
                retryable=True,
            ) from timeout_error
        if status.get("status") in {"active", "recoverable", "terminal"}:
            return status
        raise SeriesWorkerTransportError(
            "Series Worker agotó el plazo y el trabajo aún no existe",
            endpoint=PROCESS_ENDPOINT,
            status_code=None,
            error_code="series_worker_timeout_not_found",
            result={"status": "not_found", "job_id": job_id},
            retryable=True,
        ) from timeout_error

    @classmethod
    def _validated_response(
        cls,
        data: Dict[str, object],
        *,
        endpoint: str,
        job_id: str,
        allowed_statuses: set[str],
    ) -> Dict[str, object]:
        status = str(data.get("status") or "")
        if status not in allowed_statuses:
            raise cls._invalid_response(endpoint, None, data)
        if str(data.get("job_id") or "") != str(job_id):
            raise cls._invalid_response(endpoint, None, data)
        if data.get("kind") != "series":
            raise cls._invalid_response(endpoint, None, data)
        if status == "terminal":
            result = data.get("result")
            if (
                not isinstance(result, dict)
                or str(result.get("job_id") or "") != str(job_id)
                or result.get("kind") != "series"
                or result.get("status") not in {"done", "review", "failed"}
            ):
                raise cls._invalid_response(endpoint, None, data)
        return data

    @staticmethod
    def _response_json(
        response: requests.Response,
        endpoint: str,
    ) -> Dict[str, object]:
        try:
            data = response.json()
        except (TypeError, ValueError) as error:
            raise SeriesWorkerTransportError(
                "Series Worker devolvió JSON inválido",
                endpoint=endpoint,
                status_code=response.status_code,
                error_code="series_worker_invalid_json",
            ) from error
        if not isinstance(data, dict):
            raise SeriesWorkerTransportError(
                "Series Worker devolvió una respuesta JSON no válida",
                endpoint=endpoint,
                status_code=response.status_code,
                error_code="series_worker_invalid_json",
            )
        return data

    @staticmethod
    def _response_error(
        endpoint: str,
        status_code: int,
        result: Dict[str, object],
    ) -> SeriesWorkerError:
        error_code = str(
            result.get("error")
            or result.get("error_code")
            or "series_worker_error"
        )
        message = str(result.get("message") or error_code)
        error_type: type[SeriesWorkerError]
        if status_code == 409 and error_code == "series_worker_busy":
            error_type = SeriesWorkerBusy
            retryable = True
        elif status_code == 409 and error_code == "job_conflict":
            error_type = SeriesWorkerConflict
            retryable = False
        elif status_code == 400:
            error_type = SeriesWorkerBadRequest
            retryable = False
        elif status_code == 503:
            error_type = SeriesWorkerUnavailable
            retryable = bool(result.get("retryable", False))
        else:
            error_type = SeriesWorkerError
            retryable = bool(result.get("retryable", False))
        return error_type(
            message,
            endpoint=endpoint,
            status_code=status_code,
            error_code=error_code,
            result=result,
            retryable=retryable,
        )

    @staticmethod
    def _invalid_response(
        endpoint: str,
        status_code: Optional[int],
        result: Dict[str, object],
    ) -> SeriesWorkerTransportError:
        return SeriesWorkerTransportError(
            "Series Worker devolvió una respuesta incompatible con el contrato",
            endpoint=endpoint,
            status_code=status_code,
            error_code="series_worker_invalid_response",
            result=result,
        )

    @staticmethod
    def _transport_error(
        endpoint: str,
        error: requests.RequestException,
    ) -> SeriesWorkerTransportError:
        is_timeout = isinstance(error, requests.Timeout)
        return SeriesWorkerTransportError(
            (
                "Series Worker no respondió dentro del plazo"
                if is_timeout
                else "No se pudo comunicar con Series Worker"
            ),
            endpoint=endpoint,
            status_code=None,
            error_code=(
                "series_worker_timeout"
                if is_timeout
                else "series_worker_transport_error"
            ),
            retryable=True,
        )


__all__ = [
    "SeriesWorkerBadRequest",
    "SeriesWorkerBusy",
    "SeriesWorkerClient",
    "SeriesWorkerConflict",
    "SeriesWorkerError",
    "SeriesWorkerTransportError",
    "SeriesWorkerUnavailable",
]
