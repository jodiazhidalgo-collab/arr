from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote

import requests


class MediaWorkerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        status_code: Optional[int],
        error_code: str,
        result: Optional[Dict[str, object]] = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code
        self.error_code = error_code
        self.result = result
        self.retryable = retryable


class MediaWorkerJobActive(MediaWorkerError):
    pass


class MediaWorkerTransportError(MediaWorkerError):
    pass


class MediaWorkerClient:
    def __init__(
        self,
        base_url: str,
        callback_base_url: str = "http://arr-orchestrator:8787",
        timeout_seconds: int = 14400,
    ):
        self.base_url = base_url.rstrip("/")
        self.callback_base_url = callback_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def version(self) -> str:
        response = requests.get(f"{self.base_url}/health", timeout=10)
        response.raise_for_status()
        return str(response.json().get("status", "ok"))

    def process_movie(
        self,
        job_id: str,
        source_path: Path,
        final_root: Path,
        review_root: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return self._post(
            "/process-movie",
            self._movie_payload(job_id, source_path, final_root, review_root, reports_root),
        )

    def process_trailer(
        self,
        job_id: str,
        source_path: Path,
        movies_root: Path,
        review_root: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return self._post(
            "/process-trailer",
            self._trailer_payload(job_id, source_path, movies_root, review_root, reports_root),
        )

    def normalize_bluray(
        self,
        job_id: str,
        source_path: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return self._post(
            "/normalize-bluray",
            self._bluray_payload(job_id, source_path, reports_root),
        )

    def job_status(self, job_id: str, kind: str) -> Dict[str, object]:
        if kind not in {"movie", "trailer", "bluray"}:
            raise ValueError("kind debe ser movie, trailer o bluray")
        endpoint = f"/jobs/{quote(job_id, safe='')}/status"
        try:
            response = requests.get(
                f"{self.base_url}{endpoint}",
                params={"kind": kind},
                timeout=10,
            )
        except requests.RequestException as error:
            raise self._transport_error(endpoint, error) from error

        data = self._response_json(response, endpoint)
        if response.status_code == 404 and data.get("status") == "not_found":
            return data
        if response.status_code >= 400 or data.get("status") == "error":
            raise self._response_error(endpoint, response.status_code, data)
        if data.get("status") not in {"active", "terminal"}:
            raise MediaWorkerTransportError(
                "Respuesta de estado inesperada de Media Worker",
                endpoint=endpoint,
                status_code=response.status_code,
                error_code="media_worker_invalid_response",
                result=data,
            )
        return data

    def preview_process_movie(
        self,
        job_id: str,
        source_path: Path,
        final_root: Path,
        review_root: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return self._preview(
            "/process-movie",
            self._movie_payload(job_id, source_path, final_root, review_root, reports_root),
        )

    def preview_process_trailer(
        self,
        job_id: str,
        source_path: Path,
        movies_root: Path,
        review_root: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return self._preview(
            "/process-trailer",
            self._trailer_payload(job_id, source_path, movies_root, review_root, reports_root),
        )

    def preview_normalize_bluray(
        self,
        job_id: str,
        source_path: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return self._preview(
            "/normalize-bluray",
            self._bluray_payload(job_id, source_path, reports_root),
        )

    def _callback_url(self, job_id: str) -> str:
        return f"{self.callback_base_url}/jobs/{job_id}/events"

    def _movie_payload(
        self,
        job_id: str,
        source_path: Path,
        final_root: Path,
        review_root: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return {
            "job_id": job_id,
            "source_path": str(source_path),
            "final_root": str(final_root),
            "review_root": str(review_root),
            "reports_root": str(reports_root),
            "callback_url": self._callback_url(job_id),
        }

    def _trailer_payload(
        self,
        job_id: str,
        source_path: Path,
        movies_root: Path,
        review_root: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return {
            "job_id": job_id,
            "source_path": str(source_path),
            "movies_root": str(movies_root),
            "review_root": str(review_root),
            "reports_root": str(reports_root),
            "callback_url": self._callback_url(job_id),
        }

    def _bluray_payload(
        self,
        job_id: str,
        source_path: Path,
        reports_root: Path,
    ) -> Dict[str, object]:
        return {
            "job_id": job_id,
            "source_path": str(source_path),
            "reports_root": str(reports_root),
            "callback_url": self._callback_url(job_id),
        }

    def _preview(self, endpoint: str, payload: Dict[str, object]) -> Dict[str, object]:
        return {
            "method": "POST",
            "service": "media-worker",
            "endpoint": endpoint,
            "payload": payload,
            "timeout_sec": self.timeout_seconds,
        }

    def _post(self, path: str, payload: Dict[str, object]) -> Dict[str, object]:
        try:
            response = requests.post(
                f"{self.base_url}{path}",
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as error:
            raise self._transport_error(path, error) from error

        data = self._response_json(response, path)
        if response.status_code >= 400 or data.get("status") == "error":
            raise self._response_error(path, response.status_code, data)
        return data

    @staticmethod
    def _response_json(response: requests.Response, endpoint: str) -> Dict[str, object]:
        try:
            data = response.json()
        except (TypeError, ValueError) as error:
            raise MediaWorkerTransportError(
                "Media Worker devolvió JSON inválido",
                endpoint=endpoint,
                status_code=response.status_code,
                error_code="media_worker_invalid_json",
            ) from error
        if not isinstance(data, dict):
            raise MediaWorkerTransportError(
                "Media Worker devolvió una respuesta JSON no válida",
                endpoint=endpoint,
                status_code=response.status_code,
                error_code="media_worker_invalid_json",
            )
        return data

    @staticmethod
    def _response_error(
        endpoint: str,
        status_code: int,
        result: Dict[str, object],
    ) -> MediaWorkerError:
        error_code = str(result.get("error_code") or "media_worker_error")
        message = str(result.get("error") or error_code)
        error_type = MediaWorkerJobActive if status_code == 409 else MediaWorkerError
        return error_type(
            message,
            endpoint=endpoint,
            status_code=status_code,
            error_code=error_code,
            result=result,
            retryable=bool(result.get("retryable", False)),
        )

    @staticmethod
    def _transport_error(
        endpoint: str,
        error: requests.RequestException,
    ) -> MediaWorkerTransportError:
        error_code = (
            "media_worker_timeout"
            if isinstance(error, requests.Timeout)
            else "media_worker_transport_error"
        )
        return MediaWorkerTransportError(
            str(error) or error_code,
            endpoint=endpoint,
            status_code=None,
            error_code=error_code,
            retryable=True,
        )
