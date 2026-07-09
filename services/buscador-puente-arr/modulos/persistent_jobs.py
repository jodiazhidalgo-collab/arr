import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable


JOB_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,80}$")
ACTIVE_STATES = {"queued", "running"}
DISMISSABLE_STATES = {"done", "error", "interrupted"}


class PersistentJobStore:
    def __init__(self, directory: Path, logger, ttl_sec: int = 24 * 60 * 60, max_jobs: int = 120):
        self.directory = Path(directory)
        self.logger = logger
        self.ttl_sec = ttl_sec
        self.max_jobs = max_jobs
        self.lock = threading.RLock()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._mark_interrupted_jobs()

    def _path(self, job_id: str) -> Path:
        job_id = str(job_id or "").strip()
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("identificador de trabajo invalido")
        return self.directory / f"{job_id}.json"

    def _read_path_unlocked(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else None
        except Exception as exc:
            self.logger.warning("ui job load failed file=%s error=%s", path.name, str(exc)[:160])
            return None

    def _write_unlocked(self, job: dict) -> None:
        path = self._path(job.get("id", ""))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _jobs_unlocked(self) -> list[dict]:
        jobs = []
        for path in self.directory.glob("*.json"):
            job = self._read_path_unlocked(path)
            if job:
                jobs.append(job)
        return jobs

    def _cleanup_unlocked(self, now: int) -> None:
        jobs = sorted(self._jobs_unlocked(), key=lambda item: int(item.get("updated_at") or 0), reverse=True)
        for index, job in enumerate(jobs):
            age = now - int(job.get("updated_at") or job.get("created_at") or 0)
            if job.get("state") not in ACTIVE_STATES and (age > self.ttl_sec or index >= self.max_jobs):
                try:
                    self._path(job.get("id", "")).unlink(missing_ok=True)
                except Exception as exc:
                    self.logger.warning("ui job cleanup failed id=%s error=%s", job.get("id", ""), str(exc)[:160])

    def _mark_interrupted_jobs(self) -> None:
        now = int(time.time())
        with self.lock:
            for job in self._jobs_unlocked():
                if job.get("state") in ACTIVE_STATES:
                    job["state"] = "interrupted"
                    job["error"] = "Trabajo interrumpido al reiniciar el servicio"
                    job["updated_at"] = now
                    self._write_unlocked(job)
            self._cleanup_unlocked(now)

    def get(self, job_id: str, kind: str = "") -> dict | None:
        try:
            path = self._path(job_id)
        except ValueError:
            return None
        with self.lock:
            if not path.exists():
                return None
            job = self._read_path_unlocked(path)
            if not job or (kind and job.get("kind") != kind):
                return None
            return job

    def dismiss(
        self,
        job_id: str,
        kind: str = "",
        states: set[str] | None = None,
    ) -> dict:
        try:
            path = self._path(job_id)
        except ValueError:
            return {"removed": False, "reason": "invalid_id"}
        with self.lock:
            if not path.exists():
                return {"removed": False, "reason": "missing"}
            job = self._read_path_unlocked(path)
            if not job:
                return {"removed": False, "reason": "invalid_job"}
            if kind and job.get("kind") != kind:
                return {"removed": False, "reason": "kind_mismatch", "state": str(job.get("state") or "")}
            state = str(job.get("state") or "")
            if state in ACTIVE_STATES:
                return {"removed": False, "reason": "active", "state": state}
            allowed = states or DISMISSABLE_STATES
            if state not in allowed:
                return {"removed": False, "reason": "state_not_allowed", "state": state}
            path.unlink(missing_ok=True)
            return {"removed": True, "reason": "dismissed", "state": state}

    def _find_reusable_unlocked(
        self,
        kind: str,
        fingerprint: str,
        states: set[str],
        max_age_sec: int,
        now: int,
    ) -> dict | None:
        matches = [
            job
            for job in self._jobs_unlocked()
            if job.get("kind") == kind
            and job.get("fingerprint") == fingerprint
            and job.get("state") in states
            and now - int(job.get("created_at") or 0) <= max_age_sec
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: int(item.get("created_at") or 0))

    def create_or_get(
        self,
        kind: str,
        fingerprint: str,
        request_data: dict,
        runner: Callable[[], Any],
        requested_id: str = "",
        reuse_states: set[str] | None = None,
        reuse_age_sec: int = 60,
    ) -> tuple[dict, bool]:
        now = int(time.time())
        reuse_states = reuse_states or set(ACTIVE_STATES)
        requested_id = str(requested_id or "").strip()
        if requested_id and not JOB_ID_RE.fullmatch(requested_id):
            requested_id = ""

        with self.lock:
            self._cleanup_unlocked(now)
            if requested_id:
                requested = self.get(requested_id, kind)
                if requested:
                    return requested, True
            reusable = self._find_reusable_unlocked(kind, fingerprint, reuse_states, reuse_age_sec, now)
            if reusable:
                return reusable, True
            job = {
                "id": requested_id or uuid.uuid4().hex,
                "kind": kind,
                "fingerprint": fingerprint,
                "state": "queued",
                "created_at": now,
                "updated_at": now,
                "request": request_data,
                "result": None,
                "error": "",
            }
            self._write_unlocked(job)

        thread = threading.Thread(
            target=self._run,
            args=(job["id"], runner),
            name=f"ui-job-{kind}-{job['id'][:8]}",
            daemon=True,
        )
        thread.start()
        return job, False

    def _run(self, job_id: str, runner: Callable[[], Any]) -> None:
        self._update(job_id, state="running", error="")
        try:
            result = runner()
            self._update(job_id, state="done", result=result, error="")
        except Exception as exc:
            self.logger.exception("ui job failed id=%s", job_id)
            self._update(job_id, state="error", error=str(exc)[:500])

    def _update(self, job_id: str, **changes) -> None:
        with self.lock:
            job = self.get(job_id)
            if not job:
                return
            job.update(changes)
            job["updated_at"] = int(time.time())
            self._write_unlocked(job)
