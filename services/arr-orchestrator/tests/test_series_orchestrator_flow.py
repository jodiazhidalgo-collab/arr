from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from arr_orchestrator.config import Config
from arr_orchestrator.db import Database
import arr_orchestrator.engine as engine_module
from arr_orchestrator.engine import Engine, WORKER_ACTIVE_MAX_SECONDS
from arr_orchestrator.filesystem import review_content_signature
from arr_orchestrator.series_worker import (
    SeriesWorkerBusy,
    SeriesWorkerConflict,
    SeriesWorkerTransportError,
)
from series_worker.core import SeriesCoordinator
from series_worker.processing import ProcessedEpisode, ProcessingResult
from series_worker.rules import RulesStore


RULES_FINGERPRINT = "a" * 64
LINUX_SYMLINK_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="La validación de symlinks físicos se ejecuta en Linux; Windows omite limpio.",
)


def _config(root: Path, series_mode: str = "active") -> Config:
    data = root / "data"
    complete = data / "downloads" / "torrents" / "complete"
    return Config(
        mode="active",
        config_dir=root / "config",
        data_root=data,
        watch_inbox=data / "torrents" / "watch" / "inbox",
        processed_root=data / "torrents" / "watch" / "processed",
        watch_error=data / "torrents" / "watch" / "error",
        event_dir=data / "torrents" / "events" / "inbox" / "qbt",
        complete_root=complete,
        workshop_root=complete / "taller",
        movies_output=complete / "movies_automatizacion",
        movies_final=data / "media" / "movies",
        tv_output=data / "media" / "tv",
        trailers_inbox=complete / "trailers_automatizacion",
        review_dir=data / "media" / "repetidas_vs_error",
        media_worker_url="http://media-worker:8790",
        callback_url="http://arr-orchestrator:8787",
        media_reports_root=root / "config" / "media-worker",
        codex_diag_root=root / "diagnosticos_codex",
        diagnostics_root=root / "diagnostics" / "arr",
        qbt_url="http://gluetun:8080",
        qbt_user="admin",
        qbt_password="",
        rdt_url="http://rdtclient:6500",
        rdt_user="admin",
        rdt_password="",
        stable_seconds=1,
        reconcile_seconds=30,
        fallback_seconds=5400,
        health_port=8787,
        filebot_bin="/opt/filebot/filebot",
        tmdb_api_token="",
        resolver_language="es-ES",
        resolver_region="ES",
        resolver_http_timeout_ms=2500,
        resolver_total_budget_ms=5000,
        resolver_retry_seconds=60,
        series_worker_url="http://series-worker:8791",
        series_reports_root=root / "config" / "series-worker",
        series_review_dir=data / "media" / "repetidas_vs_error_series",
        series_mode=series_mode,
    )


def _engine(root: Path, series_mode: str = "active") -> tuple[Engine, Database]:
    config = _config(root, series_mode)
    config.ensure_directories()
    database = Database(root / "orchestrator.db")
    database.initialize()
    return Engine(config, database), database


def _wait_state(database: Database, job_id: str, expected: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = database.get_job(job_id)
        if job is not None and job["state"] == expected:
            return job
        time.sleep(0.01)
    job = database.get_job(job_id)
    raise AssertionError(f"Estado final {job['state'] if job else None}, esperado {expected}")


def _series_job(
    engine: Engine,
    database: Database,
    *,
    state: str = "series_postprocess_ready",
    name: str = "Mi Serie S01E01",
    content: bytes = b"episode-one",
):
    job = database.create_job(
        f"test:{name}:{state}",
        "fs",
        "tv",
        name,
        state=state,
        source_meta_json=engine._new_job_source_meta_json(
            category="tv",
            name=name,
        ),
    )
    job_root = engine.config.workshop_root / str(job["job_id"])
    source_root = job_root / "series_filebot_output"
    episode = source_root / "Mi Serie" / "Season 01" / "Mi Serie S01E01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(content)
    job = database.update_job(
        str(job["job_id"]),
        stage_path=str(job_root),
        source_path=str(source_root),
        output_root=str(engine.config.tv_output),
    )
    return job, job_root, source_root, episode


def _manifest(episode: Path, source_root: Path) -> dict[str, object]:
    content_hash = hashlib.sha256(episode.read_bytes()).hexdigest()
    source_relpath = episode.relative_to(source_root).as_posix()
    source_fingerprint = hashlib.sha256(
        f"{source_relpath}\0{episode.stat().st_size}\0{episode.stat().st_mtime_ns}".encode(
            "utf-8"
        )
    ).hexdigest()
    payload = {
        "schema": "series-manifest-v1",
        "status": "ready",
        "digest": "",
        "series_name": "Mi Serie",
        "series_key": "mi serie",
        "review_reasons": [],
        "entries": [
            {
                "source_relpath": source_relpath,
                "target_relpath": "Mi Serie/Season 01/Mi Serie S01E01.mkv",
                "series_name": "Mi Serie",
                "series_key": "mi serie",
                "season": 1,
                "episodes": [1],
                "size": episode.stat().st_size,
                "mtime_ns": episode.stat().st_mtime_ns,
                "source_fingerprint": source_fingerprint,
                "content_sha256": content_hash,
                "subtitle_sidecars": [],
            }
        ],
    }
    canonical = json.dumps(
        {
            "entries": payload["entries"],
            "review_reasons": payload["review_reasons"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["digest"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _published_manifest(final_series: Path) -> dict[str, object]:
    entries = []
    for path in sorted(
        (item for item in final_series.rglob("*") if item.is_file()),
        key=lambda item: (str(item).casefold(), str(item)),
    ):
        relative = Path(final_series.name) / path.relative_to(final_series)
        entries.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    canonical = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema": "series-published-manifest-v1",
        "digest": hashlib.sha256(canonical).hexdigest(),
        "entries": entries,
    }


def _done_result(
    job_id: str,
    manifest: dict[str, object],
    final_series: Path,
) -> dict[str, object]:
    return {
        "status": "done",
        "job_id": job_id,
        "kind": "series",
        "rules_fingerprint": RULES_FINGERPRINT,
        "manifest": manifest,
        "published": ["Mi Serie/Season 01/Mi Serie S01E01.mkv"],
        "published_manifest": _published_manifest(final_series),
        "satisfied": [],
        "series_root": "Mi Serie",
        "review_path": "",
        "delivery": {
            "mode": "new",
            "generation": "d" * 32,
            "recovered": False,
            "cleanup_pending": False,
        },
    }


class _Worker:
    def __init__(self, process_response=None, status_response=None, error=None):
        self.process_response = process_response
        self.status_response = status_response
        self.error = error
        self.process_calls = []
        self.status_calls = []

    def preview_process_series(self, *args):
        return {
            "method": "POST",
            "service": "series-worker",
            "endpoint": "/process-series",
            "payload": {
                "job_id": str(args[0]),
                "job_root": str(args[1]),
                "source_root": str(args[2]),
                "final_root": str(args[3]),
                "review_root": str(args[4]),
                "reports_root": str(args[5]),
                "callback_url": "http://arr-orchestrator:8787/jobs/test/events",
            },
            "timeout_sec": 30,
        }

    def process_series(self, *args):
        self.process_calls.append(args)
        if self.error:
            raise self.error
        return self.process_response

    def job_status(self, job_id):
        self.status_calls.append(job_id)
        return self.status_response


class _CoordinatorProcessor:
    def process(self, *, manifest, source_root, job_root, rules_snapshot):
        completed = []
        for entry in manifest.entries:
            output = Path(job_root) / "series_work" / "processed" / Path(
                *PurePosixPath(entry.target_relpath).parts
            )
            output = output.with_suffix(".mkv")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(f"processed-{entry.source_relpath}".encode("utf-8"))
            completed.append(
                ProcessedEpisode(
                    source_relpath=entry.source_relpath,
                    target_relpath=entry.target_relpath,
                    provisional_relpath=output.relative_to(job_root).as_posix(),
                    output_size=output.stat().st_size,
                    audio_mode="copy",
                    subtitle_mode="none",
                    verification={"ok": True},
                )
            )
        return ProcessingResult(
            status="verified",
            manifest_digest=manifest.digest,
            rules_fingerprint=rules_snapshot.fingerprint,
            episodes=tuple(completed),
        )


class _PendingCleanupPublisher:
    def __call__(
        self,
        job_id,
        prepared,
        final,
        journal,
        *,
        expected_files,
        allowed_existing_files,
    ):
        prepared = Path(prepared)
        final = Path(final)
        actual = sorted(
            path.relative_to(prepared).as_posix()
            for path in prepared.rglob("*")
            if path.is_file()
        )
        assert actual == sorted(expected_files)
        assert isinstance(allowed_existing_files, dict)
        journal.transition("VERIFIED", preflight={"supported": True})
        journal.transition("COMMITTING")
        final.mkdir(parents=True, exist_ok=True)
        for path in prepared.rglob("*"):
            if not path.is_file():
                continue
            target = final / path.relative_to(prepared)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        journal.transition("COMMITTED")
        shutil.rmtree(prepared)
        return {
            "status": "committed",
            "job_id": job_id,
            "generation": "pending-generation",
            "mode": "new",
            "recovered": False,
            "cleanup_pending": ["owned-shadow"],
        }


class _CoordinatorWorker:
    def __init__(self, coordinator: SeriesCoordinator):
        self.coordinator = coordinator

    def preview_process_series(self, *args):
        return {
            "method": "POST",
            "service": "series-worker",
            "endpoint": "/process-series",
            "payload": {"job_id": str(args[0])},
            "timeout_sec": 30,
        }

    def process_series(
        self,
        job_id,
        job_root,
        source_root,
        final_root,
        review_root,
        reports_root,
    ):
        submission = self.coordinator.submit(
            {
                "job_id": str(job_id),
                "job_root": str(job_root),
                "source_root": str(source_root),
                "final_root": str(final_root),
                "review_root": str(review_root),
                "reports_root": str(reports_root),
                "callback_url": "",
            }
        )
        return dict(submission.payload)

    def job_status(self, job_id):
        submission = self.coordinator.status(str(job_id))
        if submission.http_status == 202:
            submission = self.coordinator.wait(str(job_id), timeout=3.0)
        return dict(submission.payload)


def _tree_snapshot(root: Path) -> list[tuple[str, str, object]]:
    snapshot: list[tuple[str, str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot.append((relative, "symlink", os.readlink(path)))
        elif path.is_dir():
            snapshot.append((relative, "dir", None))
        elif path.is_file():
            snapshot.append((relative, "file", path.read_bytes()))
    return snapshot


@pytest.mark.parametrize(
    ("mode", "name", "selected"),
    [
        ("legacy", "codex_live_flow_probe_show", False),
        ("canary", "show_codex_live_flow_probe_", False),
        ("canary", "codex_live_flow_probe_show", True),
        ("active", "normal_show", True),
    ],
)
def test_route_is_frozen_per_new_tv_job(
    tmp_path: Path,
    mode: str,
    name: str,
    selected: bool,
) -> None:
    engine, database = _engine(tmp_path, mode)
    try:
        job = database.create_job(
            f"route:{mode}:{name}",
            "fs",
            "tv",
            name,
            source_meta_json=engine._new_job_source_meta_json(
                category="tv",
                name=name,
            ),
        )
        assert engine._series_selected_for_job(job) is selected
        changed = replace(engine.config, series_mode="legacy" if mode != "legacy" else "active")
        engine.config = changed
        assert engine._series_selected_for_job(database.get_job(job["job_id"])) is selected
    finally:
        database.close()


def test_movie_snapshot_never_selects_series_worker(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        job = database.create_job(
            "route:movie",
            "fs",
            "movies",
            "Mi Pelicula",
            source_meta_json=engine._new_job_source_meta_json(
                category="movies",
                name="Mi Pelicula",
            ),
        )
        assert engine._series_selected_for_job(job) is False
    finally:
        database.close()


def test_green_series_flow_publishes_then_cleans_in_order(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        final_episode = engine.config.tv_output / "Mi Serie" / "Season 01" / episode.name
        final_episode.parent.mkdir(parents=True)
        final_episode.write_bytes(b"processed-episode")
        terminal = _done_result(str(job["job_id"]), manifest, final_episode.parents[1])
        engine.series_worker = _Worker(
            process_response={
                "ok": True,
                "status": "terminal",
                "job_id": str(job["job_id"]),
                "kind": "series",
                "result": terminal,
            }
        )

        engine._run_series_postprocess(job)
        _wait_state(database, str(job["job_id"]), "ready_cleanup")
        assert job_root.exists()
        engine._run_cleanup(database.get_job(job["job_id"]))

        updated = database.get_job(job["job_id"])
        assert updated["state"] == "done"
        assert final_episode.is_file()
        assert not job_root.exists()
        states = [
            event["structured"].get("state")
            for event in database.job_detail(job["job_id"])["timeline"]
            if isinstance(event.get("structured"), dict)
        ]
        assert "series_postprocess_ready" in states
        assert states.index("series_postprocess_running") < states.index("ready_cleanup")
        assert states.index("ready_cleanup") < states.index("done")
        command = next(
            event
            for event in database.job_detail(job["job_id"])["timeline"]
            if event["phase"] == "series" and event["event_type"] == "command"
        )
        encoded = json.dumps(command["structured"], ensure_ascii=False)
        assert str(tmp_path) not in encoded
        assert "<DATA_DOWNLOADS>" in encoded
        assert "<DATA_MEDIA>" in encoded
        assert "<CONFIG>" in encoded
    finally:
        database.close()


def test_accepted_job_reconciles_terminal_without_repeating_post(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        final_episode = engine.config.tv_output / "Mi Serie" / "Season 01" / episode.name
        final_episode.parent.mkdir(parents=True)
        final_episode.write_bytes(b"processed")
        worker = _Worker(
            process_response={
                "ok": True,
                "status": "accepted",
                "job_id": str(job["job_id"]),
                "kind": "series",
                "rules_fingerprint": RULES_FINGERPRINT,
            },
            status_response={
                "ok": True,
                "status": "terminal",
                "job_id": str(job["job_id"]),
                "kind": "series",
                "result": _done_result(
                    str(job["job_id"]),
                    manifest,
                    final_episode.parents[1],
                ),
            },
        )
        engine.series_worker = worker

        engine._run_series_postprocess(job)
        assert database.get_job(job["job_id"])["state"] == "series_postprocess_running"
        engine._reconcile_running_series(database.get_job(job["job_id"]), recovery=True)

        _wait_state(database, str(job["job_id"]), "ready_cleanup")
        assert len(worker.process_calls) == 1
        assert worker.status_calls == [str(job["job_id"])]
    finally:
        database.close()


def test_busy_returns_to_ready_without_fallback_or_cleanup(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(engine, database)
        worker = _Worker(
            error=SeriesWorkerBusy(
                "ocupado",
                endpoint="/process-series",
                status_code=409,
                error_code="series_worker_busy",
                retryable=True,
            )
        )
        engine.series_worker = worker

        engine._run_series_postprocess(job)

        updated = database.get_job(job["job_id"])
        assert updated["state"] == "series_postprocess_ready"
        assert job_root.exists()
        assert not any(engine.config.series_review_dir.iterdir())
        assert len(worker.process_calls) == 1
    finally:
        database.close()


def test_not_found_retries_only_the_idempotent_worker_post(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(
            engine,
            database,
            state="series_postprocess_running",
        )
        engine.series_worker = _Worker(
            status_response={
                "ok": False,
                "status": "not_found",
                "job_id": str(job["job_id"]),
                "kind": "series",
            }
        )

        engine._reconcile_running_series(job, recovery=True)

        updated = database.get_job(job["job_id"])
        assert updated["state"] == "series_postprocess_ready"
        assert updated["source_path"].endswith("series_filebot_output")
        assert job_root.exists()
    finally:
        database.close()


def test_conflict_preserves_whole_job_in_series_review(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(engine, database)
        (job_root / "original" / "readme.nfo").parent.mkdir()
        (job_root / "original" / "readme.nfo").write_text("keep", encoding="utf-8")
        engine.series_worker = _Worker(
            error=SeriesWorkerConflict(
                "conflicto",
                endpoint="/process-series",
                status_code=409,
                error_code="job_conflict",
            )
        )

        engine._run_series_postprocess(job)

        updated = database.get_job(job["job_id"])
        review = Path(updated["stage_path"])
        assert updated["state"] == "manual_review"
        assert review.parent == engine.config.series_review_dir
        assert (review / "series_filebot_output" / "Mi Serie" / "Season 01").is_dir()
        assert (review / "original" / "readme.nfo").read_text(encoding="utf-8") == "keep"
        assert not job_root.exists()
    finally:
        database.close()


def test_manipulated_stage_path_never_moves_an_external_directory(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(engine, database)
        external = tmp_path / "external-do-not-move"
        external_source = external / "series_filebot_output"
        external_source.mkdir(parents=True)
        marker = external / "foreign.txt"
        marker.write_text("untouched", encoding="utf-8")
        job = database.update_job(
            str(job["job_id"]),
            stage_path=str(external),
            source_path=str(external_source),
        )
        worker = _Worker(process_response=None)
        engine.series_worker = worker

        engine._run_series_postprocess(job)

        updated = database.get_job(job["job_id"])
        assert updated["state"] == "manual_review"
        assert updated["last_error_code"] == (
            "series_worker_path_invalid_preservation_unconfirmed"
        )
        assert external.is_dir()
        assert marker.read_text(encoding="utf-8") == "untouched"
        assert job_root.is_dir()
        assert worker.process_calls == []
        assert not any(engine.config.series_review_dir.iterdir())
    finally:
        database.close()


def test_verified_worker_review_allows_client_and_workshop_cleanup(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review_relative = f"{job['job_id']}-{'b' * 12}"
        review = engine.config.series_review_dir / review_relative
        shutil.copytree(source_root, review)
        (review / "reason.json").write_text(
            json.dumps(
                {
                    "job_id": str(job["job_id"]),
                    "manifest_digest": str(manifest["digest"]),
                    "reasons": ["pack_multiserie"],
                }
            ),
            encoding="utf-8",
        )
        (review / "Revision de serie.txt").write_text("revision", encoding="utf-8")
        result = {
            "status": "review",
            "job_id": str(job["job_id"]),
            "kind": "series",
            "rules_fingerprint": RULES_FINGERPRINT,
            "manifest": manifest,
            "review_path": review_relative,
            "review_reasons": ["pack_multiserie"],
            "published": [],
        }
        engine.series_worker = _Worker(
            process_response={
                "ok": True,
                "status": "terminal",
                "job_id": str(job["job_id"]),
                "kind": "series",
                "result": result,
            }
        )

        engine._run_series_postprocess(job)

        updated = database.get_job(job["job_id"])
        assert updated["state"] == "manual_review"
        assert Path(updated["stage_path"]) == review
        assert review.is_dir()
        assert not job_root.exists()
        assert not any(engine.config.tv_output.iterdir())
    finally:
        database.close()


@pytest.mark.parametrize("worker_status", ["active", "recoverable"])
def test_old_active_or_recoverable_worker_is_bounded_without_deleting_input(
    tmp_path: Path,
    worker_status: str,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(
            engine,
            database,
            state="series_postprocess_running",
        )
        engine._worker_started_at[str(job["job_id"])] = (
            time.time() - WORKER_ACTIVE_MAX_SECONDS - 1
        )
        engine.series_worker = _Worker(
            status_response={
                "ok": True,
                "status": worker_status,
                "job_id": str(job["job_id"]),
                "kind": "series",
                "rules_fingerprint": RULES_FINGERPRINT,
                **(
                    {"started_at": time.time() - WORKER_ACTIVE_MAX_SECONDS - 1}
                    if worker_status == "active"
                    else {}
                ),
            }
        )

        engine._reconcile_running_series(job, recovery=True)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "series_postprocess_running"
        assert updated["last_error_code"] == "series_worker_active_timeout"
        assert job_root.is_dir()
        assert not any(engine.config.series_review_dir.iterdir())
    finally:
        database.close()


def test_restart_uses_immutable_series_start_instead_of_recent_job_update(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(engine, database)
        running = database.transition(
            str(job["job_id"]),
            "series_postprocess_running",
            "series",
            "Series Worker iniciado",
        )
        old = time.time() - WORKER_ACTIVE_MAX_SECONDS - 1
        connection = database.connect()
        connection.execute(
            "UPDATE job_events SET ts=? WHERE job_id=? AND phase='series'",
            (old, str(job["job_id"])),
        )
        connection.commit()
        running = database.update_job(
            str(job["job_id"]),
            last_error_code="recent_notice",
        )

        class UnavailableWorker(_Worker):
            def job_status(self, job_id):
                raise SeriesWorkerTransportError(
                    "sin canal",
                    endpoint=f"/jobs/{job_id}/status",
                    status_code=None,
                    error_code="series_worker_transport_error",
                    retryable=True,
                )

        restarted = Engine(engine.config, database)
        restarted.series_worker = UnavailableWorker()
        restarted._reconcile_running_series(running, recovery=True)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "series_postprocess_running"
        assert updated["last_error_code"] == "series_worker_active_timeout"
        assert job_root.is_dir()
    finally:
        database.close()


def test_restart_uses_latest_series_attempt_after_busy_retry(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(engine, database)
        job_id = str(job["job_id"])
        database.transition(
            job_id,
            "series_postprocess_running",
            "series",
            "Series Worker iniciado",
        )
        connection = database.connect()
        connection.execute(
            """
            UPDATE job_events SET ts=?
            WHERE rowid=(
                SELECT MIN(rowid) FROM job_events
                WHERE job_id=? AND message='Series Worker iniciado'
            )
            """,
            (time.time() - WORKER_ACTIVE_MAX_SECONDS - 300, job_id),
        )
        connection.commit()
        database.transition(
            job_id,
            "series_postprocess_ready",
            "series",
            "Series Worker ocupado; reintento seguro pendiente",
        )
        current = database.transition(
            job_id,
            "series_postprocess_running",
            "series",
            "Series Worker iniciado",
        )

        class UnavailableWorker(_Worker):
            def job_status(self, job_id):
                raise SeriesWorkerTransportError(
                    "sin canal",
                    endpoint=f"/jobs/{job_id}/status",
                    status_code=None,
                    error_code="series_worker_transport_error",
                    retryable=True,
                )

        restarted = Engine(engine.config, database)
        restarted.series_worker = UnavailableWorker()
        restarted._reconcile_running_series(current, recovery=True)

        updated = database.get_job(job_id)
        assert updated["state"] == "series_postprocess_running"
        assert updated["last_error_code"] != "series_worker_active_timeout"
        assert job_root.is_dir()
    finally:
        database.close()


def test_job_detail_limit_keeps_latest_events_in_chronological_order(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _, _, _ = _series_job(engine, database)
        job_id = str(job["job_id"])
        for message in ("primero", "segundo", "tercero"):
            database.add_event(job_id, "series", "progress", message)

        detail = database.job_detail(job_id, limit=2)

        assert detail is not None
        assert [event["message"] for event in detail["timeline"]] == [
            "segundo",
            "tercero",
        ]
    finally:
        database.close()


def test_late_terminal_after_timeout_is_still_reconciled(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, episode = _series_job(
            engine,
            database,
            state="series_postprocess_running",
        )
        job_id = str(job["job_id"])
        manifest = _manifest(episode, source_root)
        final_episode = (
            engine.config.tv_output / "Mi Serie" / "Season 01" / episode.name
        )
        final_episode.parent.mkdir(parents=True)
        final_episode.write_bytes(b"published")
        result = _done_result(job_id, manifest, final_episode.parents[1])

        engine._mark_series_active_timeout(job, "plazo", recovery=True)
        timed_out = database.get_job(job_id)
        assert timed_out["state"] == "series_postprocess_running"
        engine.series_worker = _Worker(
            status_response={
                "ok": True,
                "status": "terminal",
                "job_id": job_id,
                "kind": "series",
                "result": result,
            }
        )

        engine._reconcile_running_series(timed_out, recovery=True)
        completed = _wait_state(database, job_id, "ready_cleanup")

        assert completed["last_error_code"] is None
        assert job_root.is_dir()
    finally:
        database.close()


def test_cleanup_pending_worker_recovery_is_consumed_by_orchestrator_without_json_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _source_root, _episode = _series_job(engine, database)
        job_id = str(job["job_id"])
        for name, value in (
            ("SERIES_WORKER_ALLOWED_ROOTS", engine.config.workshop_root),
            ("SERIES_WORKER_FINAL_ROOT", engine.config.tv_output),
            ("SERIES_WORKER_REVIEW_ROOT", engine.config.series_review_dir),
            ("SERIES_WORKER_REPORT_ROOT", engine.config.series_reports_root),
            ("SERIES_WORKER_LOCK_PATH", tmp_path / "locks" / "series.lock"),
        ):
            monkeypatch.setenv(name, str(value))

        coordinator_args = {
            "rules_store": RulesStore(config_path=tmp_path / "series-rules.json"),
            "processor_factory": _CoordinatorProcessor,
            "publisher": _PendingCleanupPublisher(),
            "atomic_preflight": lambda root: {
                "supported": True,
                "st_dev": root.stat().st_dev,
            },
            "tool_checker": lambda: [],
            "lock_factory": lambda *args, **kwargs: nullcontext({"enabled": True}),
        }
        first = SeriesCoordinator(
            **coordinator_args,
            recoverer=lambda *args: (_ for _ in ()).throw(
                AssertionError("La primera ejecución no debe recuperar")
            ),
        )
        engine.series_worker = _CoordinatorWorker(first)

        engine._run_series_postprocess(job)

        result_path = engine.config.series_reports_root / job_id / "series_result.json"
        deadline = time.monotonic() + 3.0
        pending = None
        while time.monotonic() < deadline:
            try:
                candidate = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                time.sleep(0.01)
                continue
            if isinstance(candidate, dict) and bool(
                (candidate.get("delivery") or {}).get("cleanup_pending")
            ):
                pending = candidate
                break
            time.sleep(0.01)
        assert pending is not None
        assert pending["delivery"]["cleanup_pending"] is True
        assert engine._load_series_worker_result(job_id) is None

        recovery_calls = []

        def finish_worker_cleanup(*_args):
            recovery_calls.append(True)
            return {
                "status": "committed",
                "mode": "new",
                "generation": "recovered-generation",
                "recovered": True,
                "cleanup_pending": [],
            }

        restarted = SeriesCoordinator(
            **{
                **coordinator_args,
                "rules_store": RulesStore(config_path=tmp_path / "series-rules.json"),
            },
            recoverer=finish_worker_cleanup,
        )
        engine.series_worker = _CoordinatorWorker(restarted)

        engine._recover_interrupted_jobs()
        completed = _wait_state(database, job_id, "ready_cleanup")

        recovered = json.loads(result_path.read_text(encoding="utf-8"))
        assert recovered["delivery"]["cleanup_pending"] is False
        assert recovered["delivery"]["recovered"] is True
        assert recovery_calls == [True]
        assert completed["last_error_code"] is None
        assert job_root.is_dir()
    finally:
        database.close()


def test_series_verifier_threads_have_a_global_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    release = threading.Event()
    entered = threading.Event()
    entered_count = 0
    entered_lock = threading.Lock()

    def blocked_verify(*_args, **_kwargs):
        nonlocal entered_count
        with entered_lock:
            entered_count += 1
            if entered_count == 2:
                entered.set()
        release.wait(timeout=3)

    monkeypatch.setattr(engine, "_verify_series_terminal_hashes", blocked_verify)
    try:
        jobs = [
            _series_job(
                engine,
                database,
                state="series_postprocess_running",
                name=f"Serie {index} S01E01",
            )[0]
            for index in range(3)
        ]
        for job in jobs:
            engine._schedule_series_terminal_verification(
                job,
                {"status": "done", "kind": "series"},
                recovery=True,
            )

        assert entered.wait(timeout=2)
        with engine._series_verification_lock:
            running_ids = {
                job_id
                for job_id, thread in engine._series_verification_threads.items()
                if thread.is_alive()
            }
        assert len(running_ids) == 2
        assert str(jobs[2]["job_id"]) not in running_ids
    finally:
        release.set()
        for thread in list(engine._series_verification_threads.values()):
            thread.join(timeout=3)
        database.close()


@pytest.mark.parametrize(
    ("extra_name", "extra_content"),
    [
        ("Mi Serie S01E02.mkv", b"episode-two"),
        ("Mi Serie S01E01.es.srt", b"1\n00:00:00,000 --> 00:00:01,000\nHola\n"),
    ],
)
def test_ready_manifest_must_cover_every_source_file(
    tmp_path: Path,
    extra_name: str,
    extra_content: bytes,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        (episode.parent / extra_name).write_bytes(extra_content)
        final_episode = engine.config.tv_output / "Mi Serie" / "Season 01" / episode.name
        final_episode.parent.mkdir(parents=True)
        final_episode.write_bytes(b"processed")
        result = _done_result(str(job["job_id"]), manifest, final_episode.parents[1])

        with pytest.raises(ValueError, match="pack completo"):
            engine._validate_series_worker_result(job, result)
    finally:
        database.close()


def test_final_hash_tamper_never_reaches_cleanup(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        final_episode = engine.config.tv_output / "Mi Serie" / "Season 01" / episode.name
        final_episode.parent.mkdir(parents=True)
        final_episode.write_bytes(b"processed-one")
        terminal = _done_result(str(job["job_id"]), manifest, final_episode.parents[1])
        final_episode.write_bytes(b"tampered--one")
        engine.series_worker = _Worker(
            process_response={
                "ok": True,
                "status": "terminal",
                "job_id": str(job["job_id"]),
                "kind": "series",
                "result": terminal,
            }
        )

        engine._run_series_postprocess(job)
        updated = _wait_state(database, str(job["job_id"]), "manual_review")

        assert updated["last_error_code"] == "series_worker_hash_verification_failed"
        assert Path(updated["stage_path"]).parent == engine.config.series_review_dir
        assert not job_root.exists()
        assert final_episode.read_bytes() == b"tampered--one"
    finally:
        database.close()


def test_final_manifest_must_include_sidecars_and_exact_inventory(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        final_episode = engine.config.tv_output / "Mi Serie" / "Season 01" / episode.name
        final_episode.parent.mkdir(parents=True)
        final_episode.write_bytes(b"processed")
        final_srt = final_episode.with_suffix(".es.srt")
        final_srt.write_text("subtitulo", encoding="utf-8")
        result = _done_result(str(job["job_id"]), manifest, final_episode.parents[1])
        result["published_manifest"]["entries"] = [
            entry
            for entry in result["published_manifest"]["entries"]
            if not str(entry["path"]).endswith(".srt")
        ]
        canonical = json.dumps(
            result["published_manifest"]["entries"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        result["published_manifest"]["digest"] = hashlib.sha256(canonical).hexdigest()

        with pytest.raises(ValueError, match="biblioteca no coincide"):
            engine._validate_series_worker_result(job, result)
    finally:
        database.close()


def test_review_root_symlink_is_rejected_before_moving_the_pack(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(engine, database)
        external = tmp_path / "external-review"
        external.mkdir()
        engine.config.series_review_dir.rmdir()
        try:
            os.symlink(external, engine.config.series_review_dir, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"El host no permite crear symlinks de prueba: {error}")
        engine.series_worker = _Worker(
            error=SeriesWorkerConflict(
                "conflicto",
                endpoint="/process-series",
                status_code=409,
                error_code="job_conflict",
            )
        )

        engine._run_series_postprocess(job)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "manual_review"
        assert updated["last_error_code"].endswith("preservation_unconfirmed")
        assert job_root.is_dir()
        assert not any(external.iterdir())
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
@pytest.mark.parametrize("protected_name", ["workshop", "review", "reports"])
@pytest.mark.parametrize("link_level", ["root", "ancestor"])
def test_series_protected_root_symlink_is_rejected_before_post_or_external_write(
    tmp_path: Path,
    protected_name: str,
    link_level: str,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _, _, _ = _series_job(engine, database)
        protected_roots = {
            "workshop": engine.config.workshop_root,
            "review": engine.config.series_review_dir,
            "reports": engine.config.series_reports_root,
        }
        link_path = protected_roots[protected_name]
        if link_level == "ancestor":
            link_path = link_path.parent
        external = tmp_path / f"external-{protected_name}-{link_level}"
        link_path.rename(external)
        os.symlink(external, link_path, target_is_directory=True)
        (external / "outside-guard.txt").write_text("intacto", encoding="utf-8")
        before = _tree_snapshot(external)
        worker = _Worker(
            process_response={
                "ok": True,
                "status": "accepted",
                "job_id": str(job["job_id"]),
                "kind": "series",
                "rules_fingerprint": RULES_FINGERPRINT,
            }
        )
        engine.series_worker = worker

        engine._run_series_postprocess(job)

        assert worker.process_calls == []
        assert _tree_snapshot(external) == before
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
def test_series_workshop_job_dir_symlink_is_rejected_before_post_or_move(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(engine, database)
        external = tmp_path / "external-workshop-job"
        job_root.rename(external)
        os.symlink(external, job_root, target_is_directory=True)
        before = _tree_snapshot(external)
        worker = _Worker(
            process_response={
                "ok": True,
                "status": "accepted",
                "job_id": str(job["job_id"]),
                "kind": "series",
                "rules_fingerprint": RULES_FINGERPRINT,
            }
        )
        engine.series_worker = worker

        engine._run_series_postprocess(job)

        assert worker.process_calls == []
        assert _tree_snapshot(external) == before
        assert not any(engine.config.series_review_dir.iterdir())
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
def test_series_stage_rejects_workshop_ancestor_symlink_before_move(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        incoming = tmp_path / "incoming" / "Mi Serie S01E01.mkv"
        incoming.parent.mkdir()
        incoming.write_bytes(b"episode")
        job = database.create_job(
            "stage:linked-workshop",
            "fs",
            "tv",
            "Mi Serie S01E01",
            state="ready_stage",
            source_path=str(incoming),
            source_meta_json=engine._new_job_source_meta_json(
                category="tv",
                name="Mi Serie S01E01",
            ),
        )
        external = tmp_path / "external-stage-parent"
        external.mkdir()
        linked_parent = tmp_path / "linked-stage-parent"
        os.symlink(external, linked_parent, target_is_directory=True)
        engine.config = replace(
            engine.config,
            workshop_root=linked_parent / "taller",
        )

        with pytest.raises(ValueError, match="enlace simbólico"):
            engine._run_stage(job)

        assert incoming.is_file()
        assert not (external / "taller").exists()
        assert database.get_job(str(job["job_id"]))["state"] == "ready_stage"
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
def test_series_filebot_rejects_linked_job_before_preparing_or_writing(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job = database.create_job(
            "filebot:linked-job",
            "fs",
            "tv",
            "Mi Serie S01E01",
            state="ready_filebot",
            source_meta_json=engine._new_job_source_meta_json(
                category="tv",
                name="Mi Serie S01E01",
            ),
        )
        job_root = engine.config.workshop_root / str(job["job_id"])
        source = job_root / "original" / "Mi Serie S01E01.mkv"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"episode")
        job = database.update_job(
            str(job["job_id"]),
            stage_path=str(job_root),
            source_path=str(source),
        )
        external = tmp_path / "external-filebot-job"
        job_root.rename(external)
        os.symlink(external, job_root, target_is_directory=True)
        before = _tree_snapshot(external)

        engine._run_filebot(job)

        updated = database.get_job(str(job["job_id"]))
        assert updated["last_error_code"].endswith("preservation_unconfirmed")
        assert _tree_snapshot(external) == before
        assert not (external / "series_filebot_output").exists()
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
def test_series_extract_rejects_linked_job_before_reading_or_writing(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job = database.create_job(
            "extract:linked-job",
            "fs",
            "tv",
            "Mi Serie S01E01",
            state="ready_extract",
            source_meta_json=engine._new_job_source_meta_json(
                category="tv",
                name="Mi Serie S01E01",
            ),
        )
        job_root = engine.config.workshop_root / str(job["job_id"])
        source = job_root / "original" / "Mi Serie S01E01.mkv"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"episode")
        job = database.update_job(
            str(job["job_id"]),
            stage_path=str(job_root),
            source_path=str(source),
        )
        external = tmp_path / "external-extract-job"
        job_root.rename(external)
        os.symlink(external, job_root, target_is_directory=True)
        before = _tree_snapshot(external)

        engine._run_extract(job)

        updated = database.get_job(str(job["job_id"]))
        assert updated["last_error_code"].endswith("preservation_unconfirmed")
        assert _tree_snapshot(external) == before
        assert not (external / "extracted").exists()
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
def test_series_cleanup_rejects_workshop_ancestor_before_clients_or_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    cleanup_calls = []
    try:
        job, _, _, _ = _series_job(
            engine,
            database,
            state="ready_cleanup",
        )
        external = tmp_path / "external-cleanup-complete"
        engine.config.complete_root.rename(external)
        os.symlink(
            external,
            engine.config.complete_root,
            target_is_directory=True,
        )
        before = _tree_snapshot(external)
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: cleanup_calls.append(True) or True,
        )

        engine._run_cleanup(job)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "ready_cleanup"
        assert updated["last_error_code"] == "cleanup_path_invalid"
        assert cleanup_calls == []
        assert _tree_snapshot(external) == before
    finally:
        database.close()


@pytest.mark.parametrize("wrong_stage", ["workshop_root", "other_job", "own_child"])
def test_series_cleanup_rejects_wrong_in_tree_job_root_before_clients_or_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrong_stage: str,
) -> None:
    engine, database = _engine(tmp_path)
    cleanup_calls = []
    try:
        job, job_root, _, _ = _series_job(
            engine,
            database,
            state="ready_cleanup",
        )
        other_job = engine.config.workshop_root / "other-job"
        other_job.mkdir()
        (other_job / "keep.txt").write_text("intacto", encoding="utf-8")
        own_child = job_root / "series_work"
        own_child.mkdir()
        (own_child / "keep.txt").write_text("intacto", encoding="utf-8")
        candidates = {
            "workshop_root": engine.config.workshop_root,
            "other_job": other_job,
            "own_child": own_child,
        }
        before = _tree_snapshot(engine.config.workshop_root)
        job = database.update_job(
            str(job["job_id"]),
            stage_path=str(candidates[wrong_stage]),
        )
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: cleanup_calls.append(True) or True,
        )

        engine._run_cleanup(job)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "ready_cleanup"
        assert updated["last_error_code"] == "cleanup_path_invalid"
        assert cleanup_calls == []
        assert _tree_snapshot(engine.config.workshop_root) == before
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
@pytest.mark.parametrize("linked_path", ["job_dir", "series_result.json"])
def test_series_result_symlink_chain_is_rejected_before_read(
    tmp_path: Path,
    linked_path: str,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job_id = "linked-result-job"
        result_dir = engine.config.series_reports_root / job_id
        external = tmp_path / f"external-{linked_path.replace('.', '-')}"
        payload = {
            "status": "done",
            "job_id": job_id,
            "kind": "series",
            "delivery": {"cleanup_pending": False},
        }
        if linked_path == "job_dir":
            external.mkdir()
            (external / "series_result.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            os.symlink(external, result_dir, target_is_directory=True)
            guarded_root = external
        else:
            result_dir.mkdir()
            external.write_text(json.dumps(payload), encoding="utf-8")
            os.symlink(external, result_dir / "series_result.json")
            guarded_root = external.parent
        before = _tree_snapshot(guarded_root)

        with pytest.raises(ValueError, match="enlace simbólico"):
            engine._load_series_worker_result(job_id)

        assert _tree_snapshot(guarded_root) == before
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
def test_series_result_ancestor_swap_after_validation_is_not_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job_id = "raced-result-job"
        result_dir = engine.config.series_reports_root / job_id
        result_dir.mkdir()
        displaced = tmp_path / "displaced-result-job"
        external = tmp_path / "external-raced-result"
        external.mkdir()
        malicious = external / "series_result.json"
        malicious.write_text(
            json.dumps(
                {
                    "status": "done",
                    "job_id": job_id,
                    "kind": "series",
                    "delivery": {"cleanup_pending": False},
                }
            ),
            encoding="utf-8",
        )
        before = _tree_snapshot(external)
        original_validate = engine._require_series_lexical_path
        validation_calls = 0

        def swap_after_validation(path, root, label):
            nonlocal validation_calls
            validated = original_validate(path, root, label)
            validation_calls += 1
            if validation_calls == 2:
                result_dir.rename(displaced)
                os.symlink(external, result_dir, target_is_directory=True)
            return validated

        monkeypatch.setattr(
            engine,
            "_require_series_lexical_path",
            swap_after_validation,
        )

        with pytest.raises(ValueError, match="no se puede abrir"):
            engine._load_series_worker_result(job_id)

        assert validation_calls == 2
        assert _tree_snapshot(external) == before
        assert not any(displaced.iterdir())
    finally:
        database.close()


def test_internal_symlink_blocks_review_preservation(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, episode = _series_job(engine, database)
        link = job_root / "linked-episode.mkv"
        try:
            os.symlink(episode, link)
        except OSError as error:
            pytest.skip(f"El host no permite crear symlinks de prueba: {error}")
        engine.series_worker = _Worker(
            error=SeriesWorkerConflict(
                "conflicto",
                endpoint="/process-series",
                status_code=409,
                error_code="job_conflict",
            )
        )

        engine._run_series_postprocess(job)

        updated = database.get_job(str(job["job_id"]))
        assert updated["last_error_code"].endswith("preservation_unconfirmed")
        assert job_root.is_dir()
        assert not any(engine.config.series_review_dir.iterdir())
    finally:
        database.close()


def test_failure_after_move_persists_the_new_review_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(engine, database)
        engine.series_worker = _Worker(
            error=SeriesWorkerConflict(
                "conflicto",
                endpoint="/process-series",
                status_code=409,
                error_code="job_conflict",
            )
        )

        def fail_reason(*_args, **_kwargs):
            raise OSError("fallo durable simulado")

        monkeypatch.setattr(engine_module, "write_reason", fail_reason)
        engine._run_series_postprocess(job)

        updated = database.get_job(str(job["job_id"]))
        moved = Path(updated["stage_path"])
        assert updated["state"] == "manual_review"
        assert updated["last_error_code"].endswith("preservation_unconfirmed")
        assert moved.parent == engine.config.series_review_dir
        assert moved.is_dir()
        assert not job_root.exists()
    finally:
        database.close()


def test_second_series_job_waits_until_first_verification_finishes(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        first, _, _, _ = _series_job(
            engine,
            database,
            state="series_postprocess_running",
            name="Mi Serie S01E01 primera",
        )
        second, _, _, _ = _series_job(
            engine,
            database,
            state="series_postprocess_ready",
            name="Mi Serie S01E02 segunda",
        )
        worker = _Worker(
            process_response={
                "ok": True,
                "status": "accepted",
                "job_id": str(second["job_id"]),
                "kind": "series",
                "rules_fingerprint": RULES_FINGERPRINT,
            }
        )
        engine.series_worker = worker

        engine._run_series_postprocess(second)

        waiting = database.get_job(str(second["job_id"]))
        assert waiting["state"] == "series_postprocess_ready"
        assert waiting["last_error_code"] == "series_worker_pipeline_busy"
        assert worker.process_calls == []
        assert database.get_job(str(first["job_id"]))["state"] == "series_postprocess_running"
    finally:
        database.close()


def test_timed_out_unknown_job_does_not_permanently_block_worker_arbitration(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        first, _, _, _ = _series_job(
            engine,
            database,
            state="series_postprocess_running",
            name="Mi Serie S01E01 agotada",
        )
        database.update_job(
            str(first["job_id"]),
            last_error_code="series_worker_active_timeout",
        )
        second, _, _, _ = _series_job(
            engine,
            database,
            state="series_postprocess_ready",
            name="Otra Serie S01E01 nueva",
        )
        worker = _Worker(
            process_response={
                "ok": True,
                "status": "accepted",
                "job_id": str(second["job_id"]),
                "kind": "series",
                "rules_fingerprint": RULES_FINGERPRINT,
            }
        )
        engine.series_worker = worker

        engine._run_series_postprocess(second)

        assert len(worker.process_calls) == 1
        assert database.get_job(str(second["job_id"]))["state"] == (
            "series_postprocess_running"
        )
        assert database.get_job(str(first["job_id"]))["state"] == (
            "series_postprocess_running"
        )
    finally:
        database.close()


def test_series_review_cleanup_recovers_after_workshop_was_already_removed(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(engine, database)
        review = engine.config.series_review_dir / "verified-review"
        review.mkdir()
        (review / "episode.mkv").write_bytes(b"preserved")
        review_signature = engine._series_review_signature_digest(review)
        pending = database.transition(
            str(job["job_id"]),
            "series_review_cleanup",
            "series_review",
            "Revision verificada",
            stage_path=str(review),
            result_json=json.dumps(
                {
                    "status": "review",
                    "review_reasons": ["test"],
                    "_arr_review_signature": review_signature,
                }
            ),
        )
        shutil.rmtree(job_root)

        engine._run_series_review_cleanup(pending)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "manual_review"
        assert Path(updated["stage_path"]) == review
    finally:
        database.close()


def test_review_cleanup_revalidates_copy_before_clients_and_workshop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, _ = _series_job(engine, database)
        review = engine.config.series_review_dir / "verified-review"
        shutil.copytree(source_root, review)
        (review / "reason.json").write_text("{}", encoding="utf-8")
        result = {
            "status": "review",
            "review_reasons": ["test"],
            "_arr_review_signature": engine._series_review_signature_digest(review),
        }
        pending = database.transition(
            str(job["job_id"]),
            "series_review_cleanup",
            "series_review",
            "Revision verificada",
            stage_path=str(review),
            result_json=json.dumps(result),
        )
        cleanup_calls: list[bool] = []
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: cleanup_calls.append(True) and False,
        )

        engine._run_series_review_cleanup(pending)
        target = next(
            path
            for path in review.rglob("*")
            if path.is_file() and path.name != "reason.json"
        )
        target.write_bytes(b"truncated")
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: cleanup_calls.append(True) or True,
        )
        engine._run_series_review_cleanup(database.get_job(str(job["job_id"])))

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "series_review_cleanup"
        assert updated["last_error_code"] == "series_worker_review_integrity_failed"
        assert cleanup_calls == [True]
        assert job_root.is_dir()
    finally:
        database.close()


def test_review_signature_ignores_only_root_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    review = tmp_path / "review"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "reason.json").write_text("legitimo", encoding="utf-8")
    shutil.copytree(source, review)
    (review / "reason.json").write_text("metadata", encoding="utf-8")
    (review / "Revision de serie.txt").write_text("metadata", encoding="utf-8")

    assert review_content_signature(source, whole_tree=True) == review_content_signature(
        review,
        whole_tree=True,
        ignored_names=("reason.json", "Revision de serie.txt"),
    )


def test_series_dependency_is_refreshed_after_delayed_start(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        class HealthyWorker:
            def version(self):
                return "ok"

        engine.dependencies["series-worker"] = "error: arrancando"
        engine.series_worker = HealthyWorker()
        engine._refresh_series_worker_dependency()

        assert engine.dependencies["series-worker"] == "ok"
    finally:
        database.close()


def test_recovery_never_restarts_selected_filebot(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(
            engine,
            database,
            state="filebot_running",
        )

        engine._recover_interrupted_jobs()

        updated = database.get_job(job["job_id"])
        assert updated["state"] == "manual_review"
        assert Path(updated["stage_path"]).parent == engine.config.series_review_dir
        assert not job_root.exists()
    finally:
        database.close()


@pytest.mark.parametrize(
    ("series_mode", "expected_private", "expected_state"),
    [
        ("legacy", False, "ready_cleanup"),
        ("active", True, "series_postprocess_ready"),
    ],
)
def test_filebot_tv_uses_private_staging_only_when_snapshot_selects_worker(
    tmp_path: Path,
    series_mode: str,
    expected_private: bool,
    expected_state: str,
) -> None:
    engine, database = _engine(tmp_path, series_mode)
    try:
        job = database.create_job(
            f"filebot:{series_mode}",
            "fs",
            "tv",
            "Mi Serie S01E01",
            state="ready_filebot",
            source_meta_json=engine._new_job_source_meta_json(
                category="tv",
                name="Mi Serie S01E01",
            ),
        )
        job_root = engine.config.workshop_root / str(job["job_id"])
        original = job_root / "original"
        original.mkdir(parents=True)
        (original / "Mi Serie S01E01.mkv").write_bytes(b"episode")
        job = database.update_job(
            str(job["job_id"]),
            stage_path=str(job_root),
            source_path=str(original),
        )

        class FileBot:
            def __init__(self):
                self.output_roots = []

            def configure_identity_rules(self, _rules):
                pass

            def preview_command(self, _job_id, _category, input_root, output_root, _identity):
                return {
                    "mode": "legacy_amc",
                    "input_path": str(input_root),
                    "output_root": str(output_root),
                    "timeout_sec": 14400,
                }

            def run(self, _job_id, _category, input_root, output_root, *_identity):
                self.output_roots.append(Path(output_root))
                source = next(path for path in Path(input_root).rglob("*.mkv"))
                destination = Path(output_root) / "Mi Serie" / "Season 01" / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                return {
                    "exit_code": 0,
                    "moves": [
                        {"source": str(source), "destination": str(destination)}
                    ],
                    "output_media": [str(destination)],
                    "duplicate": False,
                    "stdout_tail": "ok",
                }

        filebot = FileBot()
        engine.filebot = filebot
        engine._run_filebot(job)

        updated = database.get_job(job["job_id"])
        expected_root = (
            job_root / "series_filebot_output"
            if expected_private
            else engine.config.tv_output
        )
        assert filebot.output_roots == [expected_root]
        assert updated["state"] == expected_state
        if expected_private:
            assert Path(updated["source_path"]) == expected_root
    finally:
        database.close()


def test_partial_filebot_pack_is_preserved_whole_without_worker_post(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        job = database.create_job(
            "filebot:partial",
            "fs",
            "tv",
            "Mi Serie Temporada 1",
            state="ready_filebot",
            source_meta_json=engine._new_job_source_meta_json(
                category="tv",
                name="Mi Serie Temporada 1",
            ),
        )
        job_root = engine.config.workshop_root / str(job["job_id"])
        original = job_root / "original"
        original.mkdir(parents=True)
        (original / "Mi Serie S01E01.mkv").write_bytes(b"one")
        (original / "Mi Serie S01E02.mkv").write_bytes(b"two")
        job = database.update_job(
            str(job["job_id"]),
            stage_path=str(job_root),
            source_path=str(original),
        )

        class PartialFileBot:
            def configure_identity_rules(self, _rules):
                pass

            def run(self, _job_id, _category, input_root, output_root, *_identity):
                source = sorted(Path(input_root).rglob("*.mkv"))[0]
                destination = Path(output_root) / "Mi Serie" / "Season 01" / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                return {
                    "exit_code": 0,
                    "moves": [
                        {"source": str(source), "destination": str(destination)}
                    ],
                    "output_media": [str(destination)],
                    "duplicate": False,
                    "stdout_tail": "partial",
                }

        engine.filebot = PartialFileBot()
        worker = _Worker(process_response=None)
        engine.series_worker = worker

        engine._run_filebot(job)

        updated = database.get_job(job["job_id"])
        review = Path(updated["stage_path"])
        assert updated["state"] == "manual_review"
        assert review.parent == engine.config.series_review_dir
        assert sorted(path.read_bytes() for path in review.rglob("*.mkv")) == [b"one", b"two"]
        assert worker.process_calls == []
        assert not any(engine.config.tv_output.rglob("*.mkv"))
    finally:
        database.close()


def test_corrupt_series_snapshot_fails_closed_before_filebot(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        job = database.create_job(
            "filebot:corrupt-snapshot",
            "fs",
            "tv",
            "Mi Serie S01E01",
            state="ready_filebot",
            source_meta_json="{broken",
        )
        job_root = engine.config.workshop_root / str(job["job_id"])
        original = job_root / "original"
        original.mkdir(parents=True)
        episode = original / "Mi Serie S01E01.mkv"
        episode.write_bytes(b"do-not-publish")
        job = database.update_job(
            str(job["job_id"]),
            stage_path=str(job_root),
            source_path=str(original),
        )

        engine._run_filebot(job)

        updated = database.get_job(job["job_id"])
        review = Path(updated["stage_path"])
        assert updated["state"] == "manual_review"
        assert updated["last_error_code"] == "series_pipeline_invalid"
        assert review.parent == engine.config.series_review_dir
        assert next(review.rglob("*.mkv")).read_bytes() == b"do-not-publish"
        assert not any(engine.config.tv_output.rglob("*.mkv"))
    finally:
        database.close()


@pytest.mark.parametrize(
    ("name", "mode", "eligible", "route"),
    [
        ("Mi Serie S01E01", "active", False, "legacy"),
        ("Mi Serie S01E01", "legacy", False, "series-worker"),
        ("Mi Serie S01E01", "canary", False, "series-worker"),
        ("codex_live_flow_probe_pack", "canary", True, "legacy"),
    ],
)
def test_contradictory_series_route_snapshots_are_rejected(
    tmp_path: Path,
    name: str,
    mode: str,
    eligible: bool,
    route: str,
) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        job = {
            "category": "tv",
            "name": name,
            "source_meta_json": json.dumps(
                {
                    "series_pipeline": {
                        "schema": "arr-series-pipeline-v1",
                        "profile": "tv",
                        "configured_mode": mode,
                        "canary_eligible": eligible,
                        "route": route,
                    }
                }
            ),
        }
        with pytest.raises(ValueError, match="contradice"):
            engine._series_pipeline_for_job(job)
    finally:
        database.close()


def test_historical_job_without_pipeline_keeps_legacy_fallback(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        pipeline = engine._series_pipeline_for_job(
            {"category": "tv", "name": "Serie historica", "source_meta_json": "{}"}
        )
        assert pipeline["route"] == "legacy"
        assert pipeline["source"] == "historical_fallback"
    finally:
        database.close()


def test_new_profile_snapshot_cannot_fall_back_when_series_route_is_missing(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        job = {
            "category": "tv",
            "name": "Mi Serie S01E01",
            "source_meta_json": json.dumps(
                {"identity_rules": {"profile": "tv", "revision": 1}}
            ),
        }
        with pytest.raises(ValueError, match="sin snapshot"):
            engine._series_pipeline_for_job(job)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["Mi Serie S01E01.mkv"], {(1, 1)}),
        (["Mi Serie S01E01-E02.mkv"], {(1, 1), (1, 2)}),
        (["Mi Serie S01E01-E03.mkv"], {(1, 1), (1, 2), (1, 3)}),
        (["Mi Serie S01E01E02.mkv"], {(1, 1), (1, 2)}),
        (["Mi Serie 1x01-03.mkv"], {(1, 1), (1, 2), (1, 3)}),
        (["Mi Serie 1x01x02.mkv"], {(1, 1), (1, 2)}),
        (["Mi Serie S00E01.mkv"], {(0, 1)}),
        (
            ["Mi Serie S01E01.mkv", "Mi Serie S02E03.mkv"],
            {(1, 1), (2, 3)},
        ),
    ],
)
def test_series_episode_manifest_supports_episode_and_pack_shapes(
    names: list[str], expected: set[tuple[int, int]]
) -> None:
    codes, unclassified = Engine._series_episode_manifest([Path(name) for name in names])
    assert codes == expected
    assert unclassified == []


def test_series_episode_groups_preserve_physical_file_cardinality() -> None:
    separate, separate_unknown = Engine._series_episode_groups(
        [Path("Serie S01E01.mkv"), Path("Serie S01E02.mkv")]
    )
    combined, combined_unknown = Engine._series_episode_groups(
        [Path("Serie S01E01E02.mkv")]
    )

    assert {code for group in separate for code in group} == {
        code for group in combined for code in group
    }
    assert separate != combined
    assert separate_unknown == combined_unknown == []


def test_filebot_leftover_subtitle_preserves_whole_pack(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        job = database.create_job(
            "filebot:leftover-subtitle",
            "fs",
            "tv",
            "Mi Serie S01E01",
            state="ready_filebot",
            source_meta_json=engine._new_job_source_meta_json(
                category="tv", name="Mi Serie S01E01"
            ),
        )
        job_root = engine.config.workshop_root / str(job["job_id"])
        original = job_root / "original"
        original.mkdir(parents=True)
        (original / "Mi Serie S01E01.mkv").write_bytes(b"episode")
        (original / "Mi Serie S01E01.es.srt").write_text("subtitle", encoding="utf-8")
        job = database.update_job(
            str(job["job_id"]), stage_path=str(job_root), source_path=str(original)
        )

        class LeavesSubtitleFileBot:
            def configure_identity_rules(self, _rules):
                pass

            def run(self, _job_id, _category, input_root, output_root, *_identity):
                source = next(Path(input_root).rglob("*.mkv"))
                destination = Path(output_root) / "Mi Serie" / "Season 01" / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                return {
                    "exit_code": 0,
                    "moves": [{"source": str(source), "destination": str(destination)}],
                    "output_media": [str(destination)],
                    "duplicate": False,
                    "stdout_tail": "subtitle left behind",
                }

        engine.filebot = LeavesSubtitleFileBot()
        worker = _Worker(process_response=None)
        engine.series_worker = worker
        engine._run_filebot(job)

        updated = database.get_job(job["job_id"])
        review = Path(updated["stage_path"])
        assert updated["state"] == "manual_review"
        assert updated["last_error_code"] == "series_filebot_partial_output"
        assert len(list(review.rglob("*.mkv"))) == 1
        assert next(review.rglob("*.srt")).read_text(encoding="utf-8") == "subtitle"
        assert worker.process_calls == []
        assert not any(engine.config.tv_output.rglob("*.mkv"))
    finally:
        database.close()


def test_filebot_cannot_collapse_two_physical_episodes_into_one_output(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        job = database.create_job(
            "filebot:collapsed-pack",
            "fs",
            "tv",
            "Mi Serie Temporada 1",
            state="ready_filebot",
            source_meta_json=engine._new_job_source_meta_json(
                category="tv", name="Mi Serie Temporada 1"
            ),
        )
        job_root = engine.config.workshop_root / str(job["job_id"])
        original = job_root / "original"
        original.mkdir(parents=True)
        (original / "Mi Serie S01E01.mkv").write_bytes(b"episode-one")
        (original / "Mi Serie S01E02.mkv").write_bytes(b"episode-two")
        job = database.update_job(
            str(job["job_id"]), stage_path=str(job_root), source_path=str(original)
        )

        class CollapsingFileBot:
            def configure_identity_rules(self, _rules):
                pass

            def run(self, _job_id, _category, input_root, output_root, *_identity):
                sources = sorted(Path(input_root).rglob("*.mkv"))
                destination = (
                    Path(output_root)
                    / "Mi Serie"
                    / "Season 01"
                    / "Mi Serie - S01E01E02.mkv"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(sources[0]), str(destination))
                sources[1].unlink()
                return {
                    "exit_code": 0,
                    "moves": [
                        {"source": str(sources[0]), "destination": str(destination)}
                    ],
                    "output_media": [str(destination)],
                    "duplicate": False,
                    "stdout_tail": "collapsed",
                }

        engine.filebot = CollapsingFileBot()
        engine.series_worker = _Worker(process_response=None)
        engine._run_filebot(job)

        updated = database.get_job(job["job_id"])
        assert updated["state"] == "manual_review"
        assert updated["last_error_code"] == "series_filebot_episode_manifest_mismatch"
        assert Path(updated["stage_path"]).parent == engine.config.series_review_dir
        assert not any(engine.config.tv_output.rglob("*.mkv"))
    finally:
        database.close()


def test_filebot_cannot_swap_episode_names_between_physical_files(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        job = database.create_job(
            "filebot:swapped-episodes",
            "fs",
            "tv",
            "Mi Serie Temporada 1",
            state="ready_filebot",
            source_meta_json=engine._new_job_source_meta_json(
                category="tv", name="Mi Serie Temporada 1"
            ),
        )
        job_root = engine.config.workshop_root / str(job["job_id"])
        original = job_root / "original"
        original.mkdir(parents=True)
        (original / "Mi Serie S01E01.mkv").write_bytes(b"real-episode-one")
        (original / "Mi Serie S01E02.mkv").write_bytes(b"real-episode-two")
        job = database.update_job(
            str(job["job_id"]), stage_path=str(job_root), source_path=str(original)
        )

        class SwappingFileBot:
            def configure_identity_rules(self, _rules):
                pass

            def run(self, _job_id, _category, input_root, output_root, *_identity):
                sources = sorted(Path(input_root).rglob("*.mkv"))
                destinations = [
                    Path(output_root)
                    / "Mi Serie"
                    / "Season 01"
                    / "Mi Serie - S01E02.mkv",
                    Path(output_root)
                    / "Mi Serie"
                    / "Season 01"
                    / "Mi Serie - S01E01.mkv",
                ]
                for source, destination in zip(sources, destinations):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(destination))
                return {
                    "exit_code": 0,
                    "moves": [
                        {"source": str(source), "destination": str(destination)}
                        for source, destination in zip(sources, destinations)
                    ],
                    "output_media": [str(path) for path in destinations],
                    "duplicate": False,
                    "stdout_tail": "swapped",
                }

        engine.filebot = SwappingFileBot()
        engine.series_worker = _Worker(process_response=None)
        engine._run_filebot(job)

        updated = database.get_job(job["job_id"])
        assert updated["state"] == "manual_review"
        assert updated["last_error_code"] == "series_filebot_episode_manifest_mismatch"
        review = Path(updated["stage_path"])
        assert next(review.rglob("*S01E01.mkv")).read_bytes() == b"real-episode-two"
        assert next(review.rglob("*S01E02.mkv")).read_bytes() == b"real-episode-one"
        assert not any(engine.config.tv_output.rglob("*.mkv"))
    finally:
        database.close()


def test_filebot_wrong_episode_is_never_handed_to_series_worker(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        job = database.create_job(
            "filebot:wrong-episode",
            "fs",
            "tv",
            "Mi Serie S01E01",
            state="ready_filebot",
            source_meta_json=engine._new_job_source_meta_json(
                category="tv", name="Mi Serie S01E01"
            ),
        )
        job_root = engine.config.workshop_root / str(job["job_id"])
        original = job_root / "original"
        original.mkdir(parents=True)
        (original / "Mi Serie S01E01.mkv").write_bytes(b"episode-one")
        job = database.update_job(
            str(job["job_id"]), stage_path=str(job_root), source_path=str(original)
        )

        class WrongEpisodeFileBot:
            def configure_identity_rules(self, _rules):
                pass

            def run(self, _job_id, _category, input_root, output_root, *_identity):
                source = next(Path(input_root).rglob("*.mkv"))
                destination = (
                    Path(output_root)
                    / "Mi Serie"
                    / "Season 01"
                    / "Mi Serie - S01E02.mkv"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                return {
                    "exit_code": 0,
                    "moves": [{"source": str(source), "destination": str(destination)}],
                    "output_media": [str(destination)],
                    "duplicate": False,
                    "stdout_tail": "wrong episode",
                }

        engine.filebot = WrongEpisodeFileBot()
        worker = _Worker(process_response=None)
        engine.series_worker = worker
        engine._run_filebot(job)

        updated = database.get_job(job["job_id"])
        review = Path(updated["stage_path"])
        assert updated["state"] == "manual_review"
        assert updated["last_error_code"] == "series_filebot_episode_manifest_mismatch"
        assert next(review.rglob("*.mkv")).name.endswith("S01E02.mkv")
        assert worker.process_calls == []
        assert not any(engine.config.tv_output.rglob("*.mkv"))
    finally:
        database.close()
