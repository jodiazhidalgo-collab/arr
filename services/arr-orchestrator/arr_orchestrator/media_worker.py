from pathlib import Path
from typing import Dict

import requests


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
        response = requests.post(
            f"{self.base_url}{path}",
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "error":
            raise RuntimeError(str(data.get("error") or data))
        return data
