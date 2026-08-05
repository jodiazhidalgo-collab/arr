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
from arr_orchestrator.engine import (
    Engine,
    SERIES_REVIEW_METADATA_FILES,
    SERIES_REVIEW_REASON_SPECS,
    SERIES_REVIEW_SIGNATURE_SHA256_V1,
    SERIES_REVIEW_SIGNATURE_STAT_V1,
    SERIES_WORKER_REVIEW_CODES_V2,
    WORKER_ACTIVE_MAX_SECONDS,
)
from arr_orchestrator.filesystem import (
    ExtractionError,
    media_files,
    review_content_signature,
    write_reason,
)
from arr_orchestrator.name_resolver import ResolvedIdentity
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
        series_review_dir=data / "media" / "series-review-test",
        series_mode=series_mode,
    )


def _engine(root: Path, series_mode: str = "active") -> tuple[Engine, Database]:
    config = _config(root, series_mode)
    config.ensure_directories()
    database = Database(root / "orchestrator.db")
    database.initialize()
    engine = Engine(config, database)
    engine.name_resolver = _AcceptedSeriesResolver()
    return engine, database


class _AcceptedSeriesResolver:
    enabled = True

    def __init__(self) -> None:
        self._trace: dict[str, object] = {}

    def resolve(self, job: dict[str, object], input_root: Path) -> ResolvedIdentity:
        paths = media_files(input_root)
        intents, _unclassified = Engine._series_episode_intents(paths)
        first = intents[0] if len(intents) == 1 else {}
        self._trace = {
            "decision": {
                "status": "ACCEPTED_CONFIDENT",
                "accepted": True,
                "retryable": False,
                "selected_tmdb_id": 12345,
                "selected": {
                    "tmdb_id": 12345,
                    "media_type": "tv",
                    "title": "Mi Serie",
                    "year": 2024,
                },
            }
        }
        return ResolvedIdentity(
            media_type="tv",
            tmdb_id=12345,
            title="Mi Serie",
            original_title="Mi Serie",
            year=2024,
            aliases=["Mi Serie"],
            score=100,
            margin=50,
            query=str(job.get("name") or "Mi Serie"),
            guess={},
            source="test",
            season=first.get("season") if isinstance(first, dict) else None,
            episodes=list(first.get("episodes") or []) if isinstance(first, dict) else [],
            resolver_algorithm_version="phased-er-v2",
            decision_status="ACCEPTED_CONFIDENT",
            episode_intents=intents,
        )

    def trace_snapshot(self) -> dict[str, object]:
        return self._trace

    @staticmethod
    def output_matches(_identity: ResolvedIdentity, _names: list[str]) -> bool:
        return True


def _wait_state(database: Database, job_id: str, expected: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = database.get_job(job_id)
        if job is not None and job["state"] == expected:
            return job
        time.sleep(0.01)
    job = database.get_job(job_id)
    raise AssertionError(f"Estado final {job['state'] if job else None}, esperado {expected}")


def _eventually_absent(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists() and not path.is_symlink():
            return True
        time.sleep(0.05)
    return not path.exists() and not path.is_symlink()


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
    content_hash = ""
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
                "content_sha256": "",
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
            "mode": "direct_move",
            "generation": "d" * 32,
            "recovered": False,
            "cleanup_pending": False,
        },
    }


def _review_result_v2(
    *,
    job_id: str,
    manifest: dict[str, object],
    review: Path,
    review_relative: str,
    reason_kind: str,
    reasons: list[str] | None = None,
    review_layout: str = "source_root",
    review_source_prefix: str = "",
) -> dict[str, object]:
    reason_code = SERIES_WORKER_REVIEW_CODES_V2[reason_kind]
    reason_file, reason_title, _explanation = SERIES_REVIEW_REASON_SPECS[
        reason_kind
    ]
    raw_reasons = reasons or [f"motivo_tecnico:{reason_kind}"]
    reason_lines = [
        f"Explicacion visible para {reason_kind}.",
        "Detalle visible controlado.",
    ]
    reason = {
        "schema": "series-review-v2",
        "profile": "series",
        "category": "tv",
        "job_id": job_id,
        "manifest_digest": str(manifest["digest"]),
        "reasons": raw_reasons,
        "reason_code": reason_code,
        "reason_kind": reason_kind,
        "reason_file": reason_file,
        "reason_title": reason_title,
        "reason_lines": reason_lines,
        "review_layout": review_layout,
        "review_source_prefix": review_source_prefix,
    }
    (review / "reason.json").write_text(
        json.dumps(reason, ensure_ascii=False),
        encoding="utf-8",
    )
    (review / reason_file).write_text(
        "\n".join([reason_title, *reason_lines]) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "review",
        "job_id": job_id,
        "kind": "series",
        "rules_fingerprint": RULES_FINGERPRINT,
        "manifest": manifest,
        "review_path": review_relative,
        "review_layout": review_layout,
        "review_source_prefix": review_source_prefix,
        "review_reasons": raw_reasons,
        "published": [],
        "reason_code": reason_code,
        "reason_kind": reason_kind,
        "reason_file": reason_file,
        "reason_title": reason_title,
        "reason_lines": reason_lines,
    }


def _write_orchestrator_review_reason_v2(
    review: Path,
    job_id: str,
    *,
    signature: str = "",
    signature_method: str = SERIES_REVIEW_SIGNATURE_STAT_V1,
) -> dict[str, object]:
    reason_file, reason_title, explanation = SERIES_REVIEW_REASON_SPECS["process"]
    reason = {
        "schema": "series-review-v2",
        "profile": "series",
        "category": "tv",
        "job_id": job_id,
        "reason": "series_process_error",
        "message": "Fallo controlado de prueba",
        "reasons": ["series_process_error: Fallo controlado de prueba"],
        "reason_code": "series_process_error",
        "reason_kind": "process",
        "reason_file": reason_file,
        "reason_title": reason_title,
        "reason_lines": [explanation, "Fallo controlado de prueba"],
    }
    if signature:
        reason["_arr_review_signature"] = signature
        reason["_arr_review_signature_method"] = signature_method
    write_reason(
        review,
        reason,
        reason_file,
        list(reason["reason_lines"]),
    )
    return reason


def _write_legacy_review_reason_v1(
    review: Path,
    job_id: str,
    *,
    reasons: list[str] | None = None,
) -> dict[str, object]:
    raw_reasons = reasons or ["colision_existente"]
    reason = {
        "schema": "series-review-v1",
        "profile": "series",
        "category": "tv",
        "job_id": job_id,
        "manifest_digest": "legacy",
        "reasons": raw_reasons,
        "review_layout": "source_root",
        "review_source_prefix": "",
    }
    (review / "reason.json").write_text(
        json.dumps(reason, ensure_ascii=False),
        encoding="utf-8",
    )
    (review / "Serie repetida.txt").write_text(
        "Serie repetida\n" + "\n".join(raw_reasons) + "\n",
        encoding="utf-8",
    )
    return reason


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
            output = Path(source_root) / Path(
                *PurePosixPath(entry.target_relpath).parts
            )
            output = output.with_suffix(".limpio.mkv")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(f"processed-{entry.source_relpath}".encode("utf-8"))
            output_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
            completed.append(
                ProcessedEpisode(
                    source_relpath=entry.source_relpath,
                    target_relpath=entry.target_relpath,
                    provisional_relpath=output.relative_to(job_root).as_posix(),
                    output_size=output.stat().st_size,
                    output_sha256=output_sha256,
                    subtitle_provisional_relpath=None,
                    subtitle_size=None,
                    subtitle_sha256=None,
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
        expected_file_digests,
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
        assert set(expected_file_digests) == set(expected_files)
        assert isinstance(allowed_existing_files, dict)
        journal.transition(
            "VERIFIED",
            preflight={"supported": True},
            expected_file_digests=dict(expected_file_digests),
        )
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


def _ready_extract_job(
    engine: Engine,
    database: Database,
    *,
    category: str,
    name: str,
):
    job = database.create_job(
        f"extract-failure:{category}:{name}",
        "fs",
        category,
        name,
        state="ready_extract",
        source_meta_json=engine._new_job_source_meta_json(
            category=category,
            name=name,
        ),
    )
    job_root = engine.config.workshop_root / str(job["job_id"])
    original = job_root / "original"
    original.mkdir(parents=True)
    (original / f"{name}.rar").write_bytes(b"archive")
    partial = job_root / "extracted" / "layer_01.tmp" / "partial.bin"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial")
    job = database.update_job(
        str(job["job_id"]),
        stage_path=str(job_root),
        source_path=str(original),
    )
    return job, job_root, original, partial


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


def test_conflict_uses_clean_human_series_review_layout(tmp_path: Path) -> None:
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
        assert review.name == "Mi Serie - S01E01"
        assert (review / "Mi Serie - S01E01.mkv").read_bytes() == b"episode-one"
        assert (review / "Error de proceso.txt").is_file()
        reason = json.loads((review / "reason.json").read_text(encoding="utf-8"))
        assert reason["schema"] == "series-review-v2"
        assert reason["profile"] == "series"
        assert reason["category"] == "tv"
        assert reason["reason"] == "job_conflict"
        assert reason["reason_code"] == "job_conflict"
        assert reason["reason_kind"] == "process"
        assert not any(review.rglob("*.nfo"))
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
        result: dict[str, object] = {}

        class MovingReviewWorker(_Worker):
            def process_series(self, *args):
                shutil.move(str(source_root), str(review))
                result.update(
                    _review_result_v2(
                        job_id=str(job["job_id"]),
                        manifest=manifest,
                        review=review,
                        review_relative=review_relative,
                        reason_kind="manual",
                        reasons=["pack_multiserie"],
                    )
                )
                return super().process_series(*args)

        engine.series_worker = MovingReviewWorker(
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
        assert _eventually_absent(job_root)
        assert not any(engine.config.tv_output.iterdir())
        assert updated["last_error_code"] == "series_manual_review"
        reason = json.loads((review / "reason.json").read_text(encoding="utf-8"))
        assert reason["clients_cleanup_pending"] is False
        assert (review / "Revision de serie.txt").read_text(encoding="utf-8") == (
            "\n".join([reason["reason_title"], *reason["reason_lines"]]) + "\n"
        )
        detail = database.job_detail(str(job["job_id"]))
        assert any(
            event.get("structured", {}).get("last_error_code")
            == "series_manual_review"
            for event in detail["timeline"]
        )
    finally:
        database.close()


def test_worker_review_cleanup_pending_keeps_specific_code_in_db_and_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review_relative = f"{job['job_id']}-audio"
        review = engine.config.series_review_dir / review_relative
        shutil.move(str(source_root), str(review))
        result = _review_result_v2(
            job_id=str(job["job_id"]),
            manifest=manifest,
            review=review,
            review_relative=review_relative,
            reason_kind="audio",
            reasons=["audio_no_valido:sin pista admitida"],
        )
        monkeypatch.setattr(engine, "_cleanup_clients", lambda *_args, **_kwargs: False)

        engine._apply_series_worker_result(job, result, recovery=False)

        pending = database.get_job(str(job["job_id"]))
        assert pending["state"] == "series_review_cleanup"
        assert pending["last_error_code"] == "series_audio_invalid"
        assert job_root.is_dir()
        detail = database.job_detail(str(job["job_id"]))
        assert any(
            isinstance(event.get("structured"), dict)
            and event["structured"].get("status") == "review_cleanup_pending"
            and event["structured"].get("reason_code") == "series_audio_invalid"
            for event in detail["timeline"]
        )
    finally:
        database.close()


def test_clean_worker_review_is_validated_inside_the_shared_review_root(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        engine.config = replace(
            engine.config,
            series_review_dir=engine.config.review_dir,
        )
        job, _job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review = engine.config.review_dir / "Mi Serie - S01E01"
        shutil.move(str(episode.parent), str(review))
        reason = {
            "schema": "series-review-v1",
            "profile": "series",
            "category": "tv",
            "job_id": str(job["job_id"]),
            "manifest_digest": str(manifest["digest"]),
            "reasons": ["colision_existente"],
            "review_layout": "season_root",
            "review_source_prefix": "Mi Serie/Season 01",
        }
        (review / "reason.json").write_text(
            json.dumps(reason, ensure_ascii=False),
            encoding="utf-8",
        )
        (review / "Serie repetida.txt").write_text(
            "Serie repetida\ncolision_existente\n",
            encoding="utf-8",
        )
        result = {
            "status": "review",
            "job_id": str(job["job_id"]),
            "kind": "series",
            "rules_fingerprint": RULES_FINGERPRINT,
            "manifest": manifest,
            "review_path": review.name,
            "review_layout": "season_root",
            "review_source_prefix": "Mi Serie/Season 01",
            "review_reasons": ["colision_existente"],
            "published": [],
        }

        validated = engine._validate_series_worker_result(job, result)

        assert validated == review
        assert {path.name for path in review.iterdir()} == {
            "Mi Serie S01E01.mkv",
            "Serie repetida.txt",
            "reason.json",
        }
    finally:
        database.close()


@pytest.mark.parametrize(
    "reason_kind",
    ["duplicate", "audio", "video", "subtitle", "ocr", "manual", "process"],
)
def test_series_review_v2_accepts_every_worker_reason_family(
    tmp_path: Path,
    reason_kind: str,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review_relative = f"{job['job_id']}-{reason_kind}"
        review = engine.config.series_review_dir / review_relative
        shutil.move(str(source_root), str(review))
        result = _review_result_v2(
            job_id=str(job["job_id"]),
            manifest=manifest,
            review=review,
            review_relative=review_relative,
            reason_kind=reason_kind,
        )

        assert engine._validate_series_worker_result(job, result) == review
        reason_file = str(result["reason_file"])
        assert (review / reason_file).read_text(encoding="utf-8") == (
            "\n".join(
                [str(result["reason_title"]), *list(result["reason_lines"])]
            )
            + "\n"
        )
        assert {
            path.name
            for path in review.iterdir()
            if path.name in {spec[0] for spec in SERIES_REVIEW_REASON_SPECS.values()}
        } == {reason_file}
    finally:
        database.close()


@pytest.mark.parametrize(
    "tamper",
    ["reason_file_traversal", "marker_content", "second_marker", "reason_code"],
)
def test_series_review_v2_rejects_unsafe_or_incoherent_human_marker(
    tmp_path: Path,
    tamper: str,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review_relative = f"{job['job_id']}-unsafe"
        review = engine.config.series_review_dir / review_relative
        shutil.move(str(source_root), str(review))
        result = _review_result_v2(
            job_id=str(job["job_id"]),
            manifest=manifest,
            review=review,
            review_relative=review_relative,
            reason_kind="duplicate",
        )
        reason_path = review / "reason.json"
        reason = json.loads(reason_path.read_text(encoding="utf-8"))
        if tamper == "reason_file_traversal":
            reason["reason_file"] = "../Serie repetida.txt"
            result["reason_file"] = "../Serie repetida.txt"
        elif tamper == "marker_content":
            (review / "Serie repetida.txt").write_text(
                "Serie repetida\ncontenido manipulado\n",
                encoding="utf-8",
            )
        elif tamper == "second_marker":
            (review / "Error de proceso.txt").write_text(
                "Error de proceso\n",
                encoding="utf-8",
            )
        else:
            reason["reason_code"] = "series_process_error"
            result["reason_code"] = "series_process_error"
        reason_path.write_text(
            json.dumps(reason, ensure_ascii=False),
            encoding="utf-8",
        )

        with pytest.raises(ValueError):
            engine._validate_series_worker_result(job, result)
    finally:
        database.close()


def test_series_review_rejects_unknown_schema_but_keeps_v1_compatible(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review_relative = f"{job['job_id']}-unknown"
        review = engine.config.series_review_dir / review_relative
        shutil.move(str(source_root), str(review))
        result = _review_result_v2(
            job_id=str(job["job_id"]),
            manifest=manifest,
            review=review,
            review_relative=review_relative,
            reason_kind="manual",
        )
        reason_path = review / "reason.json"
        reason = json.loads(reason_path.read_text(encoding="utf-8"))
        reason["schema"] = "series-review-v3"
        reason_path.write_text(json.dumps(reason), encoding="utf-8")

        with pytest.raises(ValueError, match="esquema desconocido"):
            engine._validate_series_worker_result(job, result)
    finally:
        database.close()


@pytest.mark.parametrize(
    "tamper",
    ["missing_schema", "marker_content", "second_marker", "result_reasons"],
)
def test_series_review_v1_requires_exact_single_marker_contract(
    tmp_path: Path,
    tamper: str,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review_relative = f"{job['job_id']}-legacy-v1"
        review = engine.config.series_review_dir / review_relative
        shutil.move(str(source_root), str(review))
        reasons = ["colision_existente:Mi Serie/Season 01/Mi Serie S01E01.mkv"]
        reason = {
            "schema": "series-review-v1",
            "profile": "series",
            "category": "tv",
            "job_id": str(job["job_id"]),
            "manifest_digest": str(manifest["digest"]),
            "reasons": reasons,
            "review_layout": "source_root",
            "review_source_prefix": "",
        }
        marker = review / "Serie repetida.txt"
        marker.write_text(
            "Serie repetida\n" + "\n".join(reasons) + "\n",
            encoding="utf-8",
        )
        result = {
            "status": "review",
            "job_id": str(job["job_id"]),
            "kind": "series",
            "rules_fingerprint": RULES_FINGERPRINT,
            "manifest": manifest,
            "review_path": review_relative,
            "review_layout": "source_root",
            "review_source_prefix": "",
            "review_reasons": list(reasons),
            "published": [],
        }
        if tamper == "missing_schema":
            reason.pop("schema")
        elif tamper == "marker_content":
            marker.write_text("Serie repetida\n", encoding="utf-8")
        elif tamper == "second_marker":
            (review / "Error de proceso.txt").write_text(
                "Error de proceso\n",
                encoding="utf-8",
            )
        else:
            result["review_reasons"] = ["otro_motivo"]
        (review / "reason.json").write_text(
            json.dumps(reason, ensure_ascii=False),
            encoding="utf-8",
        )

        with pytest.raises(ValueError):
            engine._validate_series_worker_result(job, result)
    finally:
        database.close()


def test_historical_worker_review_uses_legacy_root_only_with_durable_request(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        legacy_root = engine.config.data_root / "media" / "repetidas_vs_error_series"
        legacy_root.mkdir(parents=True)
        review = legacy_root / "Mi Serie - S01E01"
        shutil.move(str(episode.parent), str(review))
        reason = {
            "schema": "series-review-v1",
            "profile": "series",
            "category": "tv",
            "job_id": str(job["job_id"]),
            "manifest_digest": str(manifest["digest"]),
            "reasons": ["colision_existente"],
            "review_layout": "season_root",
            "review_source_prefix": "Mi Serie/Season 01",
        }
        (review / "reason.json").write_text(
            json.dumps(reason, ensure_ascii=False),
            encoding="utf-8",
        )
        (review / "Serie repetida.txt").write_text(
            "Serie repetida\ncolision_existente\n",
            encoding="utf-8",
        )
        result = {
            "status": "review",
            "job_id": str(job["job_id"]),
            "kind": "series",
            "rules_fingerprint": RULES_FINGERPRINT,
            "manifest": manifest,
            "review_path": review.name,
            "review_layout": "season_root",
            "review_source_prefix": "Mi Serie/Season 01",
            "review_reasons": ["colision_existente"],
            "published": [],
        }

        with pytest.raises(ValueError, match="destino válido"):
            engine._validate_series_worker_result(job, result)

        request_dir = engine.config.series_reports_root / str(job["job_id"])
        request_dir.mkdir(parents=True)
        (request_dir / "request.json").write_text(
            json.dumps(
                {
                    "payload": {
                        "job_id": str(job["job_id"]),
                        "review_root": str(legacy_root),
                    }
                }
            ),
            encoding="utf-8",
        )

        assert engine._validate_series_worker_result(job, result) == review
        current_review = engine.config.series_review_dir / "fallback-current"
        current_review.mkdir()
        assert (
            engine._series_review_root_for_job(
                str(job["job_id"]),
                current_review,
            )
            == engine.config.series_review_dir
        )
    finally:
        database.close()


def test_orchestrator_fallback_uses_the_same_clean_series_review_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        engine.config = replace(
            engine.config,
            series_review_dir=engine.config.review_dir,
        )
        job, job_root, _source_root, episode = _series_job(engine, database)
        episode.with_name(f"{episode.stem}.es.forced.srt").write_text(
            "subtitle",
            encoding="utf-8",
        )
        monkeypatch.setattr(engine, "_cleanup_clients", lambda *_args, **_kwargs: True)

        preserved = engine._preserve_series_job_for_review(
            job,
            job_root,
            "series_worker_unavailable",
            "fallo controlado",
        )

        updated = database.get_job(str(job["job_id"]))
        review = Path(str(updated["stage_path"]))
        assert preserved is True
        assert updated["state"] == "manual_review"
        assert review.parent == engine.config.review_dir
        assert review.name == "Mi Serie - S01E01"
        assert {path.name for path in review.iterdir()} == {
            "Mi Serie - S01E01.mkv",
            "Mi Serie - S01E01.es.forced.srt",
            "Error de proceso.txt",
            "reason.json",
        }
        assert (review / "Mi Serie - S01E01.es.forced.srt").read_text(
            encoding="utf-8"
        ) == "subtitle"
        reason = json.loads((review / "reason.json").read_text(encoding="utf-8"))
        assert reason["reason_code"] == "series_worker_unavailable"
        assert reason["reason_kind"] == "process"
        assert reason["reason_file"] == "Error de proceso.txt"
        assert (review / reason["reason_file"]).read_text(encoding="utf-8") == (
            "\n".join([reason["reason_title"], *reason["reason_lines"]]) + "\n"
        )
        assert reason["profile"] == "series"
        assert reason["category"] == "tv"
        assert not job_root.exists()
    finally:
        database.close()


@pytest.mark.parametrize(
    ("error_code", "phase", "reason_kind", "reason_file"),
    [
        ("series_duplicate", "series", "duplicate", "Serie repetida.txt"),
        ("series_filebot_duplicate", "filebot", "filebot", "Error de FileBot.txt"),
        ("series_audio_invalid", "series", "audio", "Audio no valido.txt"),
        ("series_video_invalid", "series", "video", "Video no valido.txt"),
        (
            "series_subtitle_not_convertible",
            "series",
            "subtitle",
            "Subtitulo no convertible.txt",
        ),
        ("series_ocr_subtitle_failed", "series", "ocr", "OCR subtitulo fallido.txt"),
        ("identity_ambiguous", "identity", "manual", "Revision de serie.txt"),
        ("series_worker_unavailable", "series", "process", "Error de proceso.txt"),
        ("filebot_timeout", "filebot", "filebot", "Error de FileBot.txt"),
        ("extract_volume_missing", "extract", "extraction", "Error de extraccion.txt"),
    ],
)
def test_orchestrator_fallback_reason_matrix_is_semantic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    phase: str,
    reason_kind: str,
    reason_file: str,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _source_root, _episode = _series_job(engine, database)
        monkeypatch.setattr(engine, "_cleanup_clients", lambda *_args, **_kwargs: True)

        assert engine._preserve_series_job_for_review(
            job,
            job_root,
            error_code,
            "detalle técnico controlado",
            phase=phase,
        )

        updated = database.get_job(str(job["job_id"]))
        review = Path(str(updated["stage_path"]))
        reason = json.loads((review / "reason.json").read_text(encoding="utf-8"))
        assert updated["state"] == "manual_review"
        assert updated["last_error_code"] == error_code
        assert reason["reason_code"] == error_code
        assert reason["reason_kind"] == reason_kind
        assert reason["reason_file"] == reason_file
        assert reason["reasons"] == [f"{error_code}: detalle técnico controlado"]
        assert {
            path.name
            for path in review.iterdir()
            if path.name in {spec[0] for spec in SERIES_REVIEW_REASON_SPECS.values()}
        } == {reason_file}
        marker_content = (review / reason_file).read_text(encoding="utf-8")
        assert marker_content == (
            "\n".join([reason["reason_title"], *reason["reason_lines"]]) + "\n"
        )
        assert "Código técnico:" not in marker_content
        assert not job_root.exists()
    finally:
        database.close()


@pytest.mark.parametrize("dirty_layout", [False, True])
def test_private_filebot_collision_is_never_labeled_as_series_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty_layout: bool,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _source_root, _episode = _series_job(engine, database)
        if dirty_layout:
            extra = job_root / "original" / "sin-clasificar.bin"
            extra.parent.mkdir()
            extra.write_bytes(b"keep")
        monkeypatch.setattr(engine, "_cleanup_clients", lambda *_args, **_kwargs: True)

        assert engine._preserve_series_job_for_review(
            job,
            job_root,
            "series_filebot_duplicate",
            "duplicado real confirmado",
            phase="filebot",
        )

        updated = database.get_job(str(job["job_id"]))
        review = Path(str(updated["stage_path"]))
        reason = json.loads((review / "reason.json").read_text(encoding="utf-8"))
        assert reason["reason_kind"] == "filebot"
        assert (review / "Error de FileBot.txt").is_file()
        assert not (review / "Serie repetida.txt").exists()
        assert not (review / "Revision de serie.txt").exists()
    finally:
        database.close()


def test_orchestrator_fallback_preserves_whole_tree_for_unknown_output_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, _episode = _series_job(engine, database)
        unknown = source_root / "Mi Serie" / "Season 01" / "subtitulo_original.sup"
        unknown.write_bytes(b"subtitle-image")
        monkeypatch.setattr(engine, "_cleanup_clients", lambda *_args, **_kwargs: True)

        assert engine._preserve_series_job_for_review(
            job,
            job_root,
            "series_worker_unavailable",
            "fallo controlado",
        )

        updated = database.get_job(str(job["job_id"]))
        review = Path(str(updated["stage_path"]))
        assert review.parent == engine.config.series_review_dir
        assert next(review.rglob("*.sup")).read_bytes() == b"subtitle-image"
        assert next(review.rglob("*.mkv")).read_bytes() == b"episode-one"
        assert (review / "series_filebot_output").is_dir()
        assert not job_root.exists()
    finally:
        database.close()


def test_orchestrator_fallback_never_overwrites_reserved_root_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _source_root, _episode = _series_job(engine, database)
        reserved = job_root / "Audio no valido.txt"
        reserved.write_bytes(b"contenido-del-usuario")
        cleanup_calls: list[bool] = []
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: cleanup_calls.append(True) or True,
        )

        assert not engine._preserve_series_job_for_review(
            job,
            job_root,
            "series_worker_unavailable",
            "fallo controlado",
        )

        assert reserved.read_bytes() == b"contenido-del-usuario"
        assert job_root.is_dir()
        assert cleanup_calls == []
        updated = database.get_job(str(job["job_id"]))
        assert str(updated["last_error_code"]).endswith(
            "_preservation_unconfirmed"
        )
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
def test_write_reason_atomically_replaces_marker_symlinks_without_following_them(
    tmp_path: Path,
) -> None:
    review = tmp_path / "review"
    review.mkdir()
    outside_reason = tmp_path / "outside-reason.json"
    outside_marker = tmp_path / "outside-marker.txt"
    outside_reason.write_text("NO TOCAR JSON", encoding="utf-8")
    outside_marker.write_text("NO TOCAR TXT", encoding="utf-8")
    (review / "reason.json").symlink_to(outside_reason)
    (review / "Error de proceso.txt").symlink_to(outside_marker)

    write_reason(
        review,
        {"job_id": "job-atomic", "reason_file": "Error de proceso.txt"},
        "Error de proceso.txt",
        ["Detalle seguro"],
    )

    assert outside_reason.read_text(encoding="utf-8") == "NO TOCAR JSON"
    assert outside_marker.read_text(encoding="utf-8") == "NO TOCAR TXT"
    assert not (review / "reason.json").is_symlink()
    assert not (review / "Error de proceso.txt").is_symlink()
    assert json.loads((review / "reason.json").read_text(encoding="utf-8")) == {
        "job_id": "job-atomic",
        "reason_file": "Error de proceso.txt",
    }
    assert (review / "Error de proceso.txt").read_text(encoding="utf-8") == (
        "Error de proceso\nDetalle seguro\n"
    )


@LINUX_SYMLINK_ONLY
def test_write_reason_rejects_a_symlinked_destination_directory(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("NO TOCAR", encoding="utf-8")
    linked_review = tmp_path / "linked-review"
    linked_review.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        write_reason(
            linked_review,
            {"job_id": "job-directory-link"},
            "Error de proceso.txt",
            ["Detalle que no debe escribirse fuera"],
        )

    assert sentinel.read_text(encoding="utf-8") == "NO TOCAR"
    assert {path.name for path in outside.iterdir()} == {"sentinel.txt"}


def test_write_reason_rejects_a_replaced_directory_identity(tmp_path: Path) -> None:
    review = tmp_path / "review-identity"
    review.mkdir()
    info = review.lstat()
    expected_identity = (int(info.st_dev), int(info.st_ino))
    original = tmp_path / "original-review-identity"
    review.rename(original)
    review.mkdir()
    sentinel = review / "sentinel.txt"
    sentinel.write_text("NO TOCAR", encoding="utf-8")

    with pytest.raises(OSError, match="carpeta del motivo cambió"):
        write_reason(
            review,
            {"job_id": "job-replaced-directory"},
            "Error de proceso.txt",
            ["Detalle que no debe escribirse"],
            expected_directory_identity=expected_identity,
        )

    assert {path.name for path in review.iterdir()} == {"sentinel.txt"}
    assert sentinel.read_text(encoding="utf-8") == "NO TOCAR"


def test_write_reason_blocks_if_an_old_marker_cannot_be_removed(tmp_path: Path) -> None:
    review = tmp_path / "review-blocked-marker"
    review.mkdir()
    blocked_marker = review / "Audio no valido.txt"
    blocked_marker.mkdir()

    with pytest.raises(OSError):
        write_reason(
            review,
            {"job_id": "job-blocked-marker"},
            "Error de proceso.txt",
            ["No debe coexistir con otro marcador"],
        )

    assert blocked_marker.is_dir()
    assert not (review / "Error de proceso.txt").exists()


def test_orchestrator_fallback_binds_reason_to_the_preserved_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _source_root, _episode = _series_job(engine, database)
        relocated = tmp_path / "fallback-original-review"
        cleanup_calls: list[bool] = []
        original_write_reason = engine_module.write_reason

        def replace_before_open(destination, *args, **kwargs) -> None:
            Path(destination).rename(relocated)
            Path(destination).mkdir()
            (Path(destination) / "sentinel.txt").write_text(
                "NO TOCAR",
                encoding="utf-8",
            )
            original_write_reason(destination, *args, **kwargs)

        monkeypatch.setattr(engine_module, "write_reason", replace_before_open)
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: cleanup_calls.append(True) or True,
        )

        assert not engine._preserve_series_job_for_review(
            job,
            job_root,
            "series_worker_unavailable",
            "fallo controlado",
        )

        review = Path(str(database.get_job(str(job["job_id"]))["stage_path"]))
        assert {path.name for path in review.iterdir()} == {"sentinel.txt"}
        assert [path for path in relocated.rglob("*.mkv") if path.is_file()]
        assert cleanup_calls == []
    finally:
        database.close()


def test_orchestrator_fallback_does_not_confirm_a_swap_during_client_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _source_root, _episode = _series_job(engine, database)
        relocated = tmp_path / "fallback-review-before-cleanup-swap"
        cleanup_calls: list[bool] = []

        def swap_during_cleanup(*_args, **_kwargs) -> bool:
            cleanup_calls.append(True)
            review = Path(str(database.get_job(str(job["job_id"]))["stage_path"]))
            review.rename(relocated)
            review.mkdir()
            (review / "sentinel.txt").write_text("NO TOCAR", encoding="utf-8")
            return True

        monkeypatch.setattr(engine, "_cleanup_clients", swap_during_cleanup)

        assert not engine._preserve_series_job_for_review(
            job,
            job_root,
            "series_worker_unavailable",
            "fallo controlado",
        )

        updated = database.get_job(str(job["job_id"]))
        review = Path(str(updated["stage_path"]))
        assert updated["state"] == "manual_review"
        assert str(updated["last_error_code"]).endswith("_preservation_unconfirmed")
        assert {path.name for path in review.iterdir()} == {"sentinel.txt"}
        assert (relocated / "reason.json").is_file()
        assert [path for path in relocated.rglob("*.mkv") if path.is_file()]
        assert cleanup_calls == [True]
    finally:
        database.close()


def test_orchestrator_v2_rejects_a_movie_marker_as_a_second_marker(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _job_root, _source_root, _episode = _series_job(engine, database)
        review = engine.config.series_review_dir / "v2-extra-movie-marker"
        review.mkdir()
        (review / "episode.mkv").write_bytes(b"preserved")
        reason = _write_orchestrator_review_reason_v2(review, str(job["job_id"]))
        (review / "Pelicula repetida.txt").write_text(
            "Pelicula repetida\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="más de un marcador"):
            engine._validate_series_review_reason(
                review,
                reason,
                expected_job_id=str(job["job_id"]),
            )
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
def test_review_cleanup_blocks_a_metadata_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review_relative = f"{job['job_id']}-atomic-retry"
        review = engine.config.series_review_dir / review_relative
        shutil.move(str(source_root), str(review))
        result = _review_result_v2(
            job_id=str(job["job_id"]),
            manifest=manifest,
            review=review,
            review_relative=review_relative,
            reason_kind="process",
        )
        result["_arr_review_signature"] = engine._series_review_signature_digest(review)
        result["_arr_review_signature_method"] = SERIES_REVIEW_SIGNATURE_STAT_V1
        pending = database.transition(
            str(job["job_id"]),
            "series_review_cleanup",
            "series_review",
            "Revision v2 verificada",
            stage_path=str(review),
            result_json=json.dumps(result),
        )
        outside_reason = tmp_path / "outside-cleanup-reason.json"
        outside_marker = tmp_path / "outside-cleanup-marker.txt"
        outside_reason.write_text("NO TOCAR JSON", encoding="utf-8")
        outside_marker.write_text("NO TOCAR TXT", encoding="utf-8")

        def swap_metadata(*_args, **_kwargs) -> bool:
            reason_path = review / "reason.json"
            marker_path = review / str(result["reason_file"])
            reason_path.unlink()
            marker_path.unlink()
            reason_path.symlink_to(outside_reason)
            marker_path.symlink_to(outside_marker)
            return True

        monkeypatch.setattr(engine, "_cleanup_clients", swap_metadata)

        engine._run_series_review_cleanup(pending)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "series_review_cleanup"
        assert str(updated["last_error_code"]).endswith("review_integrity_failed")
        assert job_root.is_dir()
        assert outside_reason.read_text(encoding="utf-8") == "NO TOCAR JSON"
        assert outside_marker.read_text(encoding="utf-8") == "NO TOCAR TXT"
        assert (review / "reason.json").is_symlink()
        assert (review / str(result["reason_file"])).is_symlink()
    finally:
        database.close()


def test_review_cleanup_binds_the_atomic_writer_to_the_validated_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review_relative = f"{job['job_id']}-writer-identity"
        review = engine.config.series_review_dir / review_relative
        shutil.move(str(source_root), str(review))
        result = _review_result_v2(
            job_id=str(job["job_id"]),
            manifest=manifest,
            review=review,
            review_relative=review_relative,
            reason_kind="process",
        )
        result["_arr_review_signature"] = engine._series_review_signature_digest(review)
        result["_arr_review_signature_method"] = SERIES_REVIEW_SIGNATURE_STAT_V1
        pending = database.transition(
            str(job["job_id"]),
            "series_review_cleanup",
            "series_review",
            "Revision v2 verificada",
            stage_path=str(review),
            result_json=json.dumps(result),
        )
        relocated = tmp_path / "writer-original-review"
        original_write_reason = engine_module.write_reason

        def replace_before_open(destination, *args, **kwargs) -> None:
            review.rename(relocated)
            review.mkdir()
            (review / "sentinel.txt").write_text("NO TOCAR", encoding="utf-8")
            original_write_reason(destination, *args, **kwargs)

        monkeypatch.setattr(engine, "_cleanup_clients", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(engine_module, "write_reason", replace_before_open)

        engine._run_series_review_cleanup(pending)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "series_review_cleanup"
        assert job_root.is_dir()
        assert {path.name for path in review.iterdir()} == {"sentinel.txt"}
        assert (relocated / "reason.json").is_file()
        assert (relocated / str(result["reason_file"])).is_file()
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
def test_review_cleanup_rejects_an_ancestor_swap_inside_the_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review_relative = f"{job['job_id']}-ancestor-swap"
        review_root = engine.config.series_review_dir
        review = review_root / review_relative
        shutil.move(str(source_root), str(review))
        result = _review_result_v2(
            job_id=str(job["job_id"]),
            manifest=manifest,
            review=review,
            review_relative=review_relative,
            reason_kind="process",
        )
        result["_arr_review_signature"] = engine._series_review_signature_digest(review)
        result["_arr_review_signature_method"] = SERIES_REVIEW_SIGNATURE_STAT_V1
        pending = database.transition(
            str(job["job_id"]),
            "series_review_cleanup",
            "series_review",
            "Revision v2 verificada",
            stage_path=str(review),
            result_json=json.dumps(result),
        )
        relocated_root = tmp_path / "relocated-review-root"
        outside_root = tmp_path / "outside-ancestor-root"
        outside_review = outside_root / review_relative
        outside_root.mkdir()
        sentinel = outside_root / "sentinel.txt"
        sentinel.write_text("NO TOCAR", encoding="utf-8")
        original_write_reason = engine_module.write_reason

        def replace_ancestor_before_open(destination, *args, **kwargs) -> None:
            review_root.rename(relocated_root)
            review_root.symlink_to(outside_root, target_is_directory=True)
            original_write_reason(destination, *args, **kwargs)

        monkeypatch.setattr(engine, "_cleanup_clients", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(
            engine_module,
            "write_reason",
            replace_ancestor_before_open,
        )

        engine._run_series_review_cleanup(pending)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "series_review_cleanup"
        assert job_root.is_dir()
        assert not outside_review.exists()
        assert sentinel.read_text(encoding="utf-8") == "NO TOCAR"
        assert {path.name for path in outside_root.iterdir()} == {"sentinel.txt"}
        relocated_review = relocated_root / review_relative
        assert (relocated_review / "reason.json").is_file()
        assert (relocated_review / str(result["reason_file"])).is_file()
    finally:
        database.close()


@LINUX_SYMLINK_ONLY
def test_review_cleanup_blocks_a_whole_directory_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        review_relative = f"{job['job_id']}-directory-swap"
        review = engine.config.series_review_dir / review_relative
        shutil.move(str(source_root), str(review))
        result = _review_result_v2(
            job_id=str(job["job_id"]),
            manifest=manifest,
            review=review,
            review_relative=review_relative,
            reason_kind="process",
        )
        result["_arr_review_signature"] = engine._series_review_signature_digest(review)
        result["_arr_review_signature_method"] = SERIES_REVIEW_SIGNATURE_STAT_V1
        pending = database.transition(
            str(job["job_id"]),
            "series_review_cleanup",
            "series_review",
            "Revision v2 verificada",
            stage_path=str(review),
            result_json=json.dumps(result),
        )
        relocated = tmp_path / "relocated-original-review"
        outside = tmp_path / "outside-directory-swap"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("NO TOCAR", encoding="utf-8")

        def swap_directory(*_args, **_kwargs) -> bool:
            review.rename(relocated)
            review.symlink_to(outside, target_is_directory=True)
            return True

        monkeypatch.setattr(engine, "_cleanup_clients", swap_directory)

        engine._run_series_review_cleanup(pending)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "series_review_cleanup"
        assert str(updated["last_error_code"]).endswith("review_integrity_failed")
        assert job_root.is_dir()
        assert sentinel.read_text(encoding="utf-8") == "NO TOCAR"
        assert {path.name for path in outside.iterdir()} == {"sentinel.txt"}
        assert (relocated / "reason.json").is_file()
        assert (relocated / str(result["reason_file"])).is_file()
    finally:
        database.close()


def test_orchestrator_fallback_preserves_unknown_file_outside_filebot_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _source_root, _episode = _series_job(engine, database)
        unknown = job_root / "original" / "subtitulo_original.sup"
        unknown.parent.mkdir()
        unknown.write_bytes(b"subtitle-image")
        monkeypatch.setattr(engine, "_cleanup_clients", lambda *_args, **_kwargs: True)

        assert engine._preserve_series_job_for_review(
            job,
            job_root,
            "series_worker_unavailable",
            "fallo controlado",
        )

        updated = database.get_job(str(job["job_id"]))
        review = Path(str(updated["stage_path"]))
        assert next(review.rglob("*.sup")).read_bytes() == b"subtitle-image"
        assert next(review.rglob("*.mkv")).read_bytes() == b"episode-one"
        assert (review / "original").is_dir()
        assert not job_root.exists()
    finally:
        database.close()


def test_review_signature_never_reads_the_video_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = tmp_path / "review"
    review.mkdir()
    video = review / "Serie - S01E01.mkv"
    video.write_bytes(b"video")

    monkeypatch.setattr(
        Path,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("la firma no debe leer el vídeo")
        ),
    )

    signature = review_content_signature(review, whole_tree=True)

    assert len(signature) == 1
    assert signature[0][0] == video.name
    assert signature[0][1] == video.stat().st_size


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


def test_direct_result_has_no_shadow_cleanup_or_verifier_thread(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, episode = _series_job(engine, database)
        job_id = str(job["job_id"])
        manifest = _manifest(episode, source_root)
        final_episode = engine.config.tv_output / "Mi Serie/Season 01" / episode.name
        final_episode.parent.mkdir(parents=True)
        final_episode.write_bytes(b"processed")
        result = _done_result(job_id, manifest, final_episode.parents[1])

        engine._apply_series_worker_result(job, result, recovery=False)
        completed = _wait_state(database, job_id, "ready_cleanup")

        assert completed["last_error_code"] is None
        assert job_root.is_dir()
        assert not hasattr(engine, "_series_verification_threads")
        assert not list(engine.config.tv_output.glob(".*series-worker*"))
    finally:
        database.close()


def test_done_terminal_validates_final_output_after_worker_consumes_source(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _, source_root, episode = _series_job(engine, database)
        manifest = _manifest(episode, source_root)
        final_episode = engine.config.tv_output / "Mi Serie" / "Season 01" / episode.name
        final_episode.parent.mkdir(parents=True)
        final_episode.write_bytes(b"processed")
        result = _done_result(str(job["job_id"]), manifest, final_episode.parents[1])
        shutil.rmtree(source_root)

        assert engine._validate_series_worker_result(job, result) is None
    finally:
        database.close()


def test_same_size_final_content_is_not_rehashed_before_cleanup(tmp_path: Path) -> None:
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
        updated = _wait_state(database, str(job["job_id"]), "ready_cleanup")

        assert updated["last_error_code"] is None
        assert job_root.exists()
        assert final_episode.read_bytes() == b"tampered--one"
    finally:
        database.close()


def test_final_validation_checks_declared_outputs_without_scanning_whole_series(tmp_path: Path) -> None:
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

        assert engine._validate_series_worker_result(job, result) is None
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


@pytest.mark.parametrize(
    ("extraction_error", "expected_code"),
    [
        (
            ExtractionError(
                "extract_volume_missing",
                "Falta el siguiente volumen del archivo",
                output_tail="Cannot find volume part02.rar",
            ),
            "extract_volume_missing",
        ),
        (RuntimeError("fallo inesperado del extractor"), "extract_tool_failed"),
    ],
)
def test_series_extraction_failure_preserves_whole_pack_before_client_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extraction_error: Exception,
    expected_code: str,
) -> None:
    engine, database = _engine(tmp_path)
    cleanup_calls: list[tuple[str, bool]] = []
    try:
        job, job_root, _original, partial = _ready_extract_job(
            engine,
            database,
            category="tv",
            name="Mi Serie S01E01",
        )

        def fail_extract(*_args, **_kwargs):
            raise extraction_error

        monkeypatch.setattr(engine_module, "extract_archives", fail_extract)
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda cleanup_job, strict: cleanup_calls.append(
                (str(cleanup_job["job_id"]), strict)
            )
            or True,
        )

        engine._run_extract(job)

        updated = database.get_job(str(job["job_id"]))
        review = Path(str(updated["stage_path"]))
        reason = json.loads((review / "reason.json").read_text(encoding="utf-8"))
        assert updated["state"] == "manual_review"
        assert updated["last_error_code"] == expected_code
        assert review.parent == engine.config.series_review_dir
        assert (review / "original" / f"{job['name']}.rar").read_bytes() == b"archive"
        assert (review / partial.relative_to(job_root)).read_bytes() == b"partial"
        assert (review / "Error de extraccion.txt").is_file()
        assert reason["reason"] == expected_code
        assert reason["reason_code"] == expected_code
        assert reason["reason_kind"] == "extraction"
        assert reason["phase"] == "extract"
        assert len(str(reason["_arr_review_signature"])) == 64
        assert cleanup_calls == [(str(job["job_id"]), True)]
        assert not job_root.exists()
        assert not any(engine.config.review_dir.iterdir())
    finally:
        database.close()


@pytest.mark.parametrize("tamper_mode", ["mutate", "delete"])
def test_series_extraction_review_blocks_cleanup_if_preserved_pack_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper_mode: str,
) -> None:
    engine, database = _engine(tmp_path)
    cleanup_calls: list[str] = []
    try:
        job, _job_root, _original, _partial = _ready_extract_job(
            engine,
            database,
            category="tv",
            name="Mi Serie S01E04",
        )
        monkeypatch.setattr(
            engine_module,
            "extract_archives",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ExtractionError("extract_tool_failed", "Fallo controlado")
            ),
        )
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda cleanup_job, strict: cleanup_calls.append(
                f"{cleanup_job['job_id']}:{strict}"
            )
            or False,
        )

        engine._run_extract(job)

        pending = database.get_job(str(job["job_id"]))
        review = Path(str(pending["stage_path"]))
        assert pending["last_error_code"] == "extract_tool_failed_client_cleanup_pending"
        assert cleanup_calls == [f"{job['job_id']}:True"]
        if tamper_mode == "delete":
            shutil.rmtree(review)
        else:
            preserved_archive = review / "original" / f"{job['name']}.rar"
            preserved_archive.write_bytes(b"archive-manipulado")

        engine._reconcile_late_worker_results()

        blocked = database.get_job(str(job["job_id"]))
        assert blocked["state"] == "manual_review"
        assert blocked["last_error_code"] == "extract_tool_failed_review_integrity_failed"
        assert cleanup_calls == [f"{job['job_id']}:True"]
    finally:
        database.close()


def test_series_extraction_failure_keeps_clients_when_preservation_is_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    cleanup_calls: list[bool] = []
    signature_calls = 0
    try:
        job, job_root, _original, _partial = _ready_extract_job(
            engine,
            database,
            category="tv",
            name="Mi Serie S01E02",
        )

        def fail_extract(*_args, **_kwargs):
            raise ExtractionError(
                "extract_tool_failed",
                "La herramienta de extraccion fallo",
            )

        def mismatched_signature(path: Path, *, whole_tree: bool = False):
            nonlocal signature_calls
            signature_calls += 1
            signature = review_content_signature(path, whole_tree=whole_tree)
            if signature_calls == 2:
                return [*signature, ("__mismatch__", 0, "0" * 64)]
            return signature

        monkeypatch.setattr(engine_module, "extract_archives", fail_extract)
        monkeypatch.setattr(
            engine_module,
            "review_content_signature",
            mismatched_signature,
        )
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: cleanup_calls.append(True) or True,
        )

        engine._run_extract(job)

        updated = database.get_job(str(job["job_id"]))
        review = Path(str(updated["stage_path"]))
        assert updated["state"] == "manual_review"
        assert (
            updated["last_error_code"]
            == "extract_tool_failed_preservation_unconfirmed"
        )
        assert review.parent == engine.config.series_review_dir
        assert review.exists()
        assert cleanup_calls == []
        assert not job_root.exists()
        assert not any(engine.config.review_dir.iterdir())
    finally:
        database.close()


def test_series_extraction_review_retries_failed_client_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    cleanup_outcomes = iter((False, True))
    cleanup_calls: list[str] = []
    try:
        job, _job_root, _original, _partial = _ready_extract_job(
            engine,
            database,
            category="tv",
            name="Mi Serie S01E03",
        )

        monkeypatch.setattr(
            engine_module,
            "extract_archives",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ExtractionError("extract_tool_failed", "Fallo controlado")
            ),
        )
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda cleanup_job, strict: cleanup_calls.append(
                f"{cleanup_job['job_id']}:{strict}"
            )
            or next(cleanup_outcomes),
        )

        engine._run_extract(job)

        pending = database.get_job(str(job["job_id"]))
        pending_result = json.loads(str(pending["result_json"]))
        assert pending["state"] == "manual_review"
        assert pending["last_error_code"] == "extract_tool_failed_client_cleanup_pending"
        assert pending_result["clients_cleanup_pending"] is True
        assert Path(str(pending["stage_path"])).parent == engine.config.series_review_dir

        now = time.time()
        database.connect().executemany(
            """
            INSERT INTO jobs(
                job_id, source_uid, origin, category, name, state,
                created_at, updated_at, last_error_code
            ) VALUES(?, ?, 'fs', 'movies', ?, 'manual_review', ?, ?, ?)
            """,
            [
                (
                    f"old-review-{index:03d}",
                    f"old-review:{index:03d}",
                    f"Revision antigua {index:03d}",
                    now - 2000 + index,
                    now - 2000 + index,
                    "legacy_manual_review",
                )
                for index in range(501)
            ],
        )
        database.connect().commit()
        first_legacy_page = database.jobs_in_states(["manual_review"], 500)
        assert str(job["job_id"]) not in {
            str(candidate["job_id"]) for candidate in first_legacy_page
        }

        engine._reconcile_late_worker_results()

        recovered = database.get_job(str(job["job_id"]))
        recovered_result = json.loads(str(recovered["result_json"]))
        assert recovered["state"] == "manual_review"
        assert recovered["last_error_code"] == "extract_tool_failed"
        assert recovered_result["clients_cleanup_pending"] is False
        review = Path(str(recovered["stage_path"]))
        durable_reason = json.loads((review / "reason.json").read_text(encoding="utf-8"))
        assert durable_reason["clients_cleanup_pending"] is False
        marker_content = (review / "Error de extraccion.txt").read_text(
            encoding="utf-8"
        )
        assert "limpiado sin borrar archivos" in marker_content
        assert "pendiente de reintento automático" not in marker_content
        assert "pendiente de reintento automatico" not in marker_content
        assert marker_content.count("limpiado sin borrar archivos") == 1
        assert not (review / "Revision de serie.txt").exists()
        assert cleanup_calls == [
            f"{job['job_id']}:True",
            f"{job['job_id']}:True",
        ]
    finally:
        database.close()


def test_fallback_reason_with_maximum_safe_message_remains_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    cleanup_outcomes = iter((False, True))
    try:
        job, job_root, _source_root, _episode = _series_job(engine, database)
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: next(cleanup_outcomes),
        )

        assert engine._preserve_series_job_for_review(
            job,
            job_root,
            "series_worker_unavailable",
            "x" * 1200,
        )
        pending = database.get_job(str(job["job_id"]))
        assert pending["last_error_code"] == (
            "series_worker_unavailable_client_cleanup_pending"
        )

        engine._reconcile_late_worker_results()

        recovered = database.get_job(str(job["job_id"]))
        assert recovered["last_error_code"] == "series_worker_unavailable"
        reason = json.loads(str(recovered["result_json"]))
        assert reason["clients_cleanup_pending"] is False
        assert len(reason["message"]) == 1200
    finally:
        database.close()


def test_movie_extraction_failure_keeps_legacy_review_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    cleanup_calls: list[bool] = []
    try:
        job, job_root, _original, partial = _ready_extract_job(
            engine,
            database,
            category="movies",
            name="Mi Pelicula (2026)",
        )

        def fail_extract(*_args, **_kwargs):
            raise ExtractionError(
                "extract_volume_missing",
                "Falta el siguiente volumen del archivo",
            )

        monkeypatch.setattr(engine_module, "extract_archives", fail_extract)
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: cleanup_calls.append(True) or True,
        )

        engine._run_extract(job)

        updated = database.get_job(str(job["job_id"]))
        review = Path(str(updated["stage_path"]))
        assert updated["state"] == "error_terminal"
        assert updated["last_error_code"] == "extract_volume_missing"
        assert review.parent == engine.config.review_dir
        assert (review / partial.relative_to(job_root)).read_bytes() == b"partial"
        assert (review / "Error de extraccion.txt").is_file()
        assert cleanup_calls == []
        assert not any(engine.config.series_review_dir.iterdir())
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


def test_timed_out_unknown_job_keeps_worker_arbitration_closed_for_safety(
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

        assert worker.process_calls == []
        assert database.get_job(str(second["job_id"]))["state"] == (
            "series_postprocess_ready"
        )
        assert database.get_job(str(second["job_id"]))["last_error_code"] == (
            "series_worker_pipeline_busy"
        )
        assert database.get_job(str(first["job_id"]))["state"] == (
            "series_postprocess_running"
        )
    finally:
        database.close()


def test_full_series_queue_stages_only_one_of_twenty_jobs(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        jobs = []
        for index in range(1, 21):
            name = f"Serie Cola S01E{index:02d}.mkv"
            source = engine.config.complete_root / "tv" / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(f"episode-{index}".encode("ascii"))
            jobs.append(
                database.create_job(
                    f"queue:{index}",
                    "fs",
                    "tv",
                    name,
                    state="ready_stage",
                    source_path=str(source),
                    source_meta_json=engine._new_job_source_meta_json(
                        category="tv",
                        name=name,
                    ),
                )
            )

        staged: list[str] = []

        def stage_once(job):
            job_id = str(job["job_id"])
            staged.append(job_id)
            job_root = engine.config.workshop_root / job_id
            job_root.mkdir(parents=True)
            database.transition(
                job_id,
                "series_postprocess_running",
                "test",
                "Trabajo de prueba ocupa la cola completa",
                stage_path=str(job_root),
            )

        engine._run_stage = stage_once  # type: ignore[method-assign]
        engine.process_jobs()

        assert len(staged) == 1
        first = staged[0]
        for job in jobs:
            current = database.get_job(str(job["job_id"]))
            if str(job["job_id"]) == first:
                assert current["state"] == "series_postprocess_running"
                assert (engine.config.workshop_root / first).is_dir()
            else:
                assert current["state"] == "ready_stage"
                assert current["last_error_code"] == "series_worker_pipeline_queued"
                assert not (engine.config.workshop_root / str(job["job_id"])).exists()

        database.transition(first, "done", "test", "Primero terminado")
        engine.process_jobs()

        assert len(staged) == 2
        assert staged[1] != first
    finally:
        database.close()


def test_exhausted_identity_retry_does_not_block_next_series(tmp_path: Path) -> None:
    engine, database = _engine(tmp_path)
    try:
        pending_name = "Serie Pendiente S01E01.mkv"
        pending_source = engine.config.complete_root / "tv" / pending_name
        pending_source.parent.mkdir(parents=True, exist_ok=True)
        pending_source.write_bytes(b"pending")
        pending = database.create_job(
            "queue:identity-provider-pending",
            "fs",
            "tv",
            pending_name,
            state="identity_retry",
            source_path=str(pending_source),
            identity_retry_at=None,
            retry_filebot=3,
            source_meta_json=engine._new_job_source_meta_json(
                category="tv",
                name=pending_name,
            ),
        )
        next_name = "Serie Siguiente S01E02.mkv"
        next_source = engine.config.complete_root / "tv" / next_name
        next_source.write_bytes(b"next")
        following = database.create_job(
            "queue:after-identity-provider-pending",
            "fs",
            "tv",
            next_name,
            state="ready_stage",
            source_path=str(next_source),
            source_meta_json=engine._new_job_source_meta_json(
                category="tv",
                name=next_name,
            ),
        )
        assert engine._series_full_pipeline_owner() == str(following["job_id"])
        assert not engine._series_waits_for_full_pipeline(following)
        assert database.get_job(str(pending["job_id"]))["state"] == "identity_retry"
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
        _write_orchestrator_review_reason_v2(
            review,
            str(job["job_id"]),
            signature=review_signature,
        )
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
                    "_arr_review_signature_method": SERIES_REVIEW_SIGNATURE_STAT_V1,
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
        _write_orchestrator_review_reason_v2(review, str(job["job_id"]))
        result = {
            "status": "review",
            "review_reasons": ["test"],
            "_arr_review_signature": engine._series_review_signature_digest(review),
            "_arr_review_signature_method": SERIES_REVIEW_SIGNATURE_STAT_V1,
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
            if path.is_file() and path.name not in SERIES_REVIEW_METADATA_FILES
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


def test_review_cleanup_rechecks_content_changed_by_client_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, _ = _series_job(engine, database)
        review = engine.config.series_review_dir / "changed-during-client-cleanup"
        shutil.copytree(source_root, review)
        target = next(review.rglob("*.mkv"))
        signature = engine._series_review_signature_digest(review)
        _write_orchestrator_review_reason_v2(
            review,
            str(job["job_id"]),
            signature=signature,
        )
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
                    "_arr_review_signature": signature,
                    "_arr_review_signature_method": SERIES_REVIEW_SIGNATURE_STAT_V1,
                }
            ),
        )
        cleanup_calls: list[bool] = []

        def change_preserved_episode(*_args, **_kwargs) -> bool:
            cleanup_calls.append(True)
            target.write_bytes(b"episodio truncado durante limpieza")
            return True

        monkeypatch.setattr(engine, "_cleanup_clients", change_preserved_episode)

        engine._run_series_review_cleanup(pending)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "series_review_cleanup"
        assert str(updated["last_error_code"]).endswith("review_integrity_failed")
        assert cleanup_calls == [True]
        assert job_root.is_dir()
    finally:
        database.close()


def test_late_cleanup_writer_binds_to_the_validated_review_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _job_root, source_root, _ = _series_job(engine, database)
        review = engine.config.series_review_dir / "late-writer-identity"
        shutil.copytree(source_root, review)
        signature = engine._series_review_signature_digest(review)
        reason = _write_orchestrator_review_reason_v2(
            review,
            str(job["job_id"]),
            signature=signature,
        )
        pending = database.transition(
            str(job["job_id"]),
            "manual_review",
            "series_review",
            "Limpieza de clientes pendiente",
            stage_path=str(review),
            last_error_code="series_process_error_client_cleanup_pending",
            result_json=json.dumps(reason),
        )
        relocated = tmp_path / "late-writer-original-review"
        original_write_reason = engine_module.write_reason

        def replace_before_open(destination, *args, **kwargs) -> None:
            review.rename(relocated)
            review.mkdir()
            (review / "sentinel.txt").write_text("NO TOCAR", encoding="utf-8")
            original_write_reason(destination, *args, **kwargs)

        monkeypatch.setattr(engine_module, "write_reason", replace_before_open)

        with pytest.raises(OSError):
            engine._mark_series_review_cleanup_completed(pending)

        assert {path.name for path in review.iterdir()} == {"sentinel.txt"}
        assert (relocated / "reason.json").is_file()
        assert (relocated / str(reason["reason_file"])).is_file()
    finally:
        database.close()


@pytest.mark.parametrize(
    "tamper",
    ["missing_schema", "unknown_schema", "marker", "foreign_marker"],
)
def test_series_review_cleanup_revalidates_legacy_v1_contract_before_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, _ = _series_job(engine, database)
        review = engine.config.series_review_dir / "legacy-v1-retry"
        shutil.copytree(source_root, review)
        reason = _write_legacy_review_reason_v1(review, str(job["job_id"]))
        signature = engine._series_review_signature_digest(review)
        pending = database.transition(
            str(job["job_id"]),
            "series_review_cleanup",
            "series_review",
            "Revision v1 verificada",
            stage_path=str(review),
            result_json=json.dumps(
                {
                    "status": "review",
                    "review_reasons": list(reason["reasons"]),
                    "_arr_review_signature": signature,
                    "_arr_review_signature_method": SERIES_REVIEW_SIGNATURE_STAT_V1,
                }
            ),
        )
        if tamper == "marker":
            (review / "Serie repetida.txt").write_text(
                "Serie repetida\ncontenido manipulado\n",
                encoding="utf-8",
            )
        elif tamper == "foreign_marker":
            (review / "Pelicula repetida.txt").write_text(
                "Pelicula repetida\n",
                encoding="utf-8",
            )
        else:
            reason_path = review / "reason.json"
            changed = json.loads(reason_path.read_text(encoding="utf-8"))
            if tamper == "missing_schema":
                changed.pop("schema")
            else:
                changed["schema"] = "series-review-v3"
            reason_path.write_text(json.dumps(changed), encoding="utf-8")
        cleanup_calls: list[bool] = []
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: cleanup_calls.append(True) or True,
        )

        engine._run_series_review_cleanup(pending)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "series_review_cleanup"
        assert str(updated["last_error_code"]).endswith("review_integrity_failed")
        assert cleanup_calls == []
        assert job_root.is_dir()
    finally:
        database.close()


def test_series_review_cleanup_accepts_legacy_sha256_signature(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, _, _ = _series_job(engine, database)
        review = engine.config.series_review_dir / "legacy-verified-review"
        review.mkdir()
        (review / "episode.mkv").write_bytes(b"preserved")
        legacy_signature = engine._series_review_signature_digest(
            review,
            SERIES_REVIEW_SIGNATURE_SHA256_V1,
        )
        _write_orchestrator_review_reason_v2(
            review,
            str(job["job_id"]),
            signature=legacy_signature,
            signature_method=SERIES_REVIEW_SIGNATURE_SHA256_V1,
        )
        pending = database.transition(
            str(job["job_id"]),
            "series_review_cleanup",
            "series_review",
            "Revision verificada antigua",
            stage_path=str(review),
            result_json=json.dumps(
                {
                    "status": "review",
                    "review_reasons": ["legacy"],
                    "_arr_review_signature": legacy_signature,
                }
            ),
        )
        shutil.rmtree(job_root)

        engine._run_series_review_cleanup(pending)

        updated = database.get_job(str(job["job_id"]))
        assert updated["state"] == "manual_review"
        assert Path(str(updated["stage_path"])) == review
    finally:
        database.close()


def test_pending_client_cleanup_rejects_unknown_review_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, job_root, source_root, _ = _series_job(engine, database)
        review = engine.config.series_review_dir / "pending-client-cleanup"
        shutil.copytree(source_root, review)
        signature = engine._series_review_signature_digest(review)
        reason = _write_orchestrator_review_reason_v2(
            review,
            str(job["job_id"]),
            signature=signature,
        )
        pending = database.transition(
            str(job["job_id"]),
            "manual_review",
            "series_review",
            "Limpieza de clientes pendiente",
            stage_path=str(review),
            last_error_code="series_process_error_client_cleanup_pending",
            result_json=json.dumps(reason),
        )
        reason["schema"] = "series-review-v3"
        (review / "reason.json").write_text(json.dumps(reason), encoding="utf-8")
        cleanup_calls: list[bool] = []
        monkeypatch.setattr(
            engine,
            "_cleanup_clients",
            lambda *_args, **_kwargs: cleanup_calls.append(True) or True,
        )

        engine._reconcile_late_worker_results()

        updated = database.get_job(str(pending["job_id"]))
        assert str(updated["last_error_code"]).endswith("review_integrity_failed")
        assert cleanup_calls == []
        assert job_root.is_dir()
    finally:
        database.close()


def test_legacy_review_without_signature_compares_content_not_mtime(
    tmp_path: Path,
) -> None:
    engine, database = _engine(tmp_path)
    try:
        job, _job_root, source_root, source_episode = _series_job(engine, database)
        review = engine.config.series_review_dir / "legacy-unsigned-review"
        shutil.copytree(source_root, review)
        review_episode = next(review.rglob("*.mkv"))
        os.utime(
            review_episode,
            ns=(source_episode.stat().st_atime_ns, source_episode.stat().st_mtime_ns + 1),
        )
        _write_legacy_review_reason_v1(review, str(job["job_id"]))
        pending = database.transition(
            str(job["job_id"]),
            "series_review_cleanup",
            "series_review",
            "Revision antigua sin firma",
            stage_path=str(review),
            result_json=json.dumps(
                {
                    "status": "review",
                    "review_reasons": ["legacy-unsigned"],
                }
            ),
        )

        engine._run_series_review_cleanup(pending)

        updated = database.get_job(str(job["job_id"]))
        result = json.loads(str(updated["result_json"]))
        assert updated["state"] == "manual_review"
        assert result["_arr_review_signature_method"] == SERIES_REVIEW_SIGNATURE_SHA256_V1
    finally:
        database.close()


def test_review_signature_ignores_only_root_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    review = tmp_path / "review"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "reason.json").write_text("legitimo", encoding="utf-8")
    shutil.copytree(source, review)
    for filename in SERIES_REVIEW_METADATA_FILES:
        (review / filename).write_text("metadata", encoding="utf-8")

    assert review_content_signature(source, whole_tree=True) == review_content_signature(
        review,
        whole_tree=True,
        ignored_names=SERIES_REVIEW_METADATA_FILES,
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
        (["Mi Serie S01E01-E03E05.mkv"], {(1, 1), (1, 2), (1, 3), (1, 5)}),
        (["Mi Serie S01E01E02.mkv"], {(1, 1), (1, 2)}),
        (
            ["Juego.De.Tronos.S04E01 1080p. BluRayRIP SPANiSH.1080p.mkv"],
            {(4, 1)},
        ),
        (["Mi Serie S02E03.2160p.mkv"], {(2, 3)}),
        (["Mi Serie 1x01-03.mkv"], {(1, 1), (1, 2), (1, 3)}),
        (["Mi Serie 1x01-03x05.mkv"], {(1, 1), (1, 2), (1, 3), (1, 5)}),
        (["Mi Serie 1x01x02.mkv"], {(1, 1), (1, 2)}),
        (["Mi Serie 1x04 720p.mkv"], {(1, 4)}),
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


@pytest.mark.parametrize(
    ("name", "expected_signature"),
    [
        ("Mi Serie [Cap.201].mkv", ("episodes", ((2, 1),))),
        ("Mi Serie Capitulo 14.mkv", ("absolute", 14)),
        ("Mi Serie S00E03.mkv", ("episodes", ((0, 3),))),
        ("Mi Serie Especial 2.mkv", ("episodes", ((0, 2),))),
        ("Mi Serie T06 completa.mkv", ("season_pack", 6)),
        ("Mi Serie S01E01E02.mkv", ("episodes", ((1, 1), (1, 2)))),
    ],
)
def test_series_episode_intents_cover_supported_tv_shapes(
    name: str,
    expected_signature: tuple[object, ...],
) -> None:
    intents, unclassified = Engine._series_episode_intents([Path(name)])

    assert unclassified == []
    assert len(intents) == 1
    assert Engine._episode_intent_signature(intents[0]) == expected_signature


def test_series_episode_intents_preserve_every_file_in_a_pack() -> None:
    paths = [
        Path("Mi Serie [Cap.201].mkv"),
        Path("Mi Serie S02E02E03.mkv"),
        Path("Mi Serie Especial 4.mkv"),
    ]

    intents, unclassified = Engine._series_episode_intents(paths)

    assert unclassified == []
    assert [str(item["source"]) for item in intents] == [path.name for path in paths]
    assert Engine._series_intent_codes(intents) == {
        (0, 4),
        (2, 1),
        (2, 2),
        (2, 3),
    }


def test_game_of_thrones_1080p_pack_matches_resolver_episode_intents() -> None:
    names = [
        f"Juego.De.Tronos.S04E{episode:02d} 1080p. "
        "BluRayRIP SPANiSH.1080p [www.newpct1.com].mkv"
        for episode in range(1, 11)
    ]
    local_intents, unclassified = Engine._series_episode_intents(
        [Path(name) for name in names]
    )
    resolver_intents = [
        {
            "source": name,
            "season": 4,
            "episodes": [episode],
            "absolute_episode": None,
            "is_season_pack": False,
        }
        for episode, name in enumerate(names, start=1)
    ]
    identity = ResolvedIdentity(
        media_type="tv",
        tmdb_id=1399,
        title="Juego de tronos",
        original_title="Game of Thrones",
        year=2011,
        aliases=["Juego de tronos", "Game of Thrones"],
        score=100,
        margin=50,
        query="Juego de Tronos",
        guess={},
        source="test",
        season=4,
        episodes=list(range(1, 11)),
        resolver_algorithm_version="phased-er-v2",
        decision_status="ACCEPTED_CONFIDENT",
        episode_intents=resolver_intents,
    )

    assert unclassified == []
    assert Engine._series_intent_codes(local_intents) == {
        (4, episode) for episode in range(1, 11)
    }
    assert (
        Engine._series_identity_input_conflict(
            identity,
            local_intents,
            resolver_intents,
        )
        is None
    )


def test_aggregate_validation_keeps_physical_binding_and_accepts_absolute_mapping() -> None:
    assert Engine._series_episode_intents_compatible(
        ("absolute", 14),
        ("episodes", ((3, 14),)),
    )
    assert not Engine._series_episode_intents_compatible(
        ("episodes", ((1, 1),)),
        ("episodes", ((1, 2),)),
    )


@pytest.mark.parametrize(
    ("input_name", "output_name"),
    [
        ("Mi Serie [Cap.201].mkv", "Mi Serie - S02E01.mkv"),
        ("Mi Serie Capitulo 14.mkv", "Mi Serie - S03E14.mkv"),
        ("Mi Serie Especial 2.mkv", "Mi Serie - S00E02.mkv"),
        ("Mi Serie T06 completa.mkv", "Mi Serie - S06E01.mkv"),
        ("Mi Serie S01E01E02.mkv", "Mi Serie - S01E01E02.mkv"),
    ],
)
def test_series_shape_is_validated_as_a_per_file_intent_through_filebot(
    tmp_path: Path,
    input_name: str,
    output_name: str,
) -> None:
    engine, database = _engine(tmp_path, "active")
    try:
        job = database.create_job(
            f"filebot:intent:{input_name}",
            "fs",
            "tv",
            input_name,
            state="ready_filebot",
            source_meta_json=engine._new_job_source_meta_json(
                category="tv",
                name=input_name,
            ),
        )
        job_root = engine.config.workshop_root / str(job["job_id"])
        original = job_root / "original"
        original.mkdir(parents=True)
        (original / input_name).write_bytes(b"episode")
        job = database.update_job(
            str(job["job_id"]),
            stage_path=str(job_root),
            source_path=str(original),
        )

        class ShapeFileBot:
            def configure_identity_rules(self, _rules):
                pass

            def run(self, _job_id, _category, input_root, output_root, identity):
                assert identity.tmdb_id == 12345
                source = next(Path(input_root).rglob("*.mkv"))
                destination = Path(output_root) / "Mi Serie" / "Season" / output_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                return {
                    "exit_code": 0,
                    "moves": [{"source": str(source), "destination": str(destination)}],
                    "output_media": [str(destination)],
                    "duplicate": False,
                    "stdout_tail": "ok",
                }

        engine.filebot = ShapeFileBot()
        engine._run_filebot(job)

        updated = database.get_job(job["job_id"])
        assert updated["state"] == "series_postprocess_ready"
        assert Path(updated["source_path"]).joinpath(
            "Mi Serie", "Season", output_name
        ).exists()
    finally:
        database.close()


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
