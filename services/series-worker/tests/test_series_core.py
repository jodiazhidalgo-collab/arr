import errno
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
import series_worker.core as core_module
import series_worker.delivery as delivery_module

from series_worker.core import (
    JobConflict,
    RequestValidationError,
    SeriesCoordinator,
    SeriesWorkerBusy,
    ServiceUnavailable,
    validate_payload,
)
from series_worker.heavy_lock import HeavyLockTimeout
from series_worker.processing import (
    BASE_TOOLS,
    OCR_TOOLS,
    EpisodeProcessingError,
    ProcessedEpisode,
    ProcessingResult,
)
from series_worker.rules import RulesSnapshot, RulesStore


class FakeProcessor:
    def __init__(self, *, fail_on=0, entered=None, release=None, snapshots=None):
        self.fail_on = fail_on
        self.entered = entered
        self.release = release
        self.snapshots = snapshots

    def process(self, *, manifest, source_root, job_root, rules_snapshot):
        if self.snapshots is not None:
            self.snapshots.append(rules_snapshot)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=3)
        completed = []
        for index, entry in enumerate(manifest.entries, start=1):
            if self.fail_on == index:
                raise EpisodeProcessingError(
                    "fallo controlado",
                    entry=entry,
                    partial_results=completed,
                )
            output = Path(source_root) / Path(
                *PurePosixPath(entry.target_relpath).parts
            )
            output = output.with_suffix(".limpio.mkv")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(f"processed-{entry.source_relpath}".encode())
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


class FakeSidecarProcessor(FakeProcessor):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def process(self, **kwargs):
        self.calls += 1
        result = super().process(**kwargs)
        episodes = []
        for episode in result.episodes:
            output = Path(kwargs["job_root"]) / episode.provisional_relpath
            clean_stem = output.stem.removesuffix(".limpio")
            sidecar = output.with_name(f"{clean_stem}.es.forced.srt")
            sidecar.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHola\n",
                encoding="utf-8",
            )
            episodes.append(
                replace(
                    episode,
                    subtitle_provisional_relpath=sidecar.relative_to(
                        kwargs["job_root"]
                    ).as_posix(),
                    subtitle_size=sidecar.stat().st_size,
                    subtitle_sha256=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
                )
            )
        return replace(result, episodes=tuple(episodes))


class FakePublisher:
    def __init__(self):
        self.calls = []

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
        expected_files = tuple(expected_files)
        self.calls.append(
            (
                job_id,
                prepared,
                final,
                expected_files,
                dict(expected_file_digests),
                dict(allowed_existing_files),
            )
        )
        actual = sorted(
            path.relative_to(prepared).as_posix()
            for path in prepared.rglob("*")
            if path.is_file()
        )
        assert actual == sorted(expected_files)
        for relative, digest in expected_file_digests.items():
            assert hashlib.sha256((prepared / relative).read_bytes()).hexdigest() == digest
        journal.transition(
            "VERIFIED",
            preflight={"supported": True},
            expected_file_digests=dict(expected_file_digests),
        )
        journal.transition("COMMITTING")
        final.mkdir(parents=True, exist_ok=True)
        for path in prepared.rglob("*"):
            if path.is_file():
                target = final / path.relative_to(prepared)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.samefile(path):
                    continue
                shutil.copy2(path, target)
        journal.transition("COMMITTED")
        shutil.rmtree(prepared)
        return {
            "status": "committed",
            "job_id": job_id,
            "generation": "fake-generation",
            "mode": "exchange" if any(final.iterdir()) else "new",
            "recovered": False,
            "cleanup_pending": [],
        }


@pytest.fixture
def layout(tmp_path, monkeypatch):
    taller = tmp_path / "taller"
    tv = tmp_path / "tv"
    review = tmp_path / "review"
    reports = tmp_path / "reports"
    for path in (taller, tv, review, reports):
        path.mkdir()
    monkeypatch.setenv("SERIES_WORKER_ALLOWED_ROOTS", str(taller))
    monkeypatch.setenv("SERIES_WORKER_FINAL_ROOT", str(tv))
    monkeypatch.setenv("SERIES_WORKER_REVIEW_ROOT", str(review))
    monkeypatch.setenv("SERIES_WORKER_REPORT_ROOT", str(reports))
    monkeypatch.setenv("SERIES_WORKER_LOCK_PATH", str(tmp_path / "locks/media-heavy.lock"))
    return {
        "root": tmp_path,
        "taller": taller,
        "tv": tv,
        "review": review,
        "reports": reports,
    }


def _payload(layout, job_id="job-1", videos=None, sidecars=None):
    job = layout["taller"] / job_id
    source = job / "series_filebot_output"
    source.mkdir(parents=True, exist_ok=True)
    for relative, content in (videos or [("Serie/Season 01/Serie.S01E01.mkv", b"one")]):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for relative, content in sidecars or []:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return {
        "job_id": job_id,
        "job_root": str(job),
        "source_root": str(source),
        "final_root": str(layout["tv"]),
        "review_root": str(layout["review"]),
        "reports_root": str(layout["reports"]),
        "callback_url": "",
    }


def _mutate_same_size_and_mtime(path: Path) -> None:
    previous = path.stat()
    content = bytearray(path.read_bytes())
    content[0] ^= 1
    path.write_bytes(content)
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))


def _coordinator(layout, processor=None, publisher=None, lock_factory=None):
    store = RulesStore(config_path=layout["root"] / "rules.json")
    selected_processor = processor or FakeProcessor()
    return SeriesCoordinator(
        rules_store=store,
        processor_factory=lambda: selected_processor,
        publisher=publisher or FakePublisher(),
        recoverer=lambda *args: {"status": "committed", "mode": "exchange", "recovered": True},
        atomic_preflight=lambda root: {"supported": True, "st_dev": root.stat().st_dev},
        tool_checker=lambda: [],
        lock_factory=lock_factory or (lambda *args, **kwargs: nullcontext({"enabled": True})),
    )


def _real_delivery_coordinator(layout, processor):
    return SeriesCoordinator(
        rules_store=RulesStore(config_path=layout["root"] / "rules.json"),
        processor_factory=lambda: processor,
        publisher=delivery_module.publish_series,
        recoverer=delivery_module.recover_delivery,
        atomic_preflight=lambda root: {"supported": True, "st_dev": root.stat().st_dev},
        tool_checker=lambda: [],
        lock_factory=lambda *args, **kwargs: nullcontext({"enabled": True}),
    )


def _portable_exchange(left, right):
    temporary = left.with_name(f".{left.name}.test-exchange")
    os.rename(left, temporary)
    try:
        os.rename(right, left)
        os.rename(temporary, right)
    except Exception:
        if not left.exists() and temporary.exists():
            os.rename(temporary, left)
        raise


def _tree_contents(root, *, ignore_root_names=frozenset()):
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not (path.parent == root and path.name in ignore_root_names)
    }


def _assert_review_matches_source(layout, payload, result):
    review_root = layout["review"] / result["review_path"]
    assert review_root.is_dir()
    assert not Path(payload["source_root"]).exists()
    assert (review_root / "reason.json").is_file()
    assert (review_root / "Revision de serie.txt").is_file()
    return review_root


def test_payload_requires_exact_keys_and_canonical_roots(layout):
    payload = _payload(layout)
    assert validate_payload(payload).source_root.name == "series_filebot_output"

    unknown = {**payload, "extra": "x"}
    with pytest.raises(RequestValidationError, match="desconocidos"):
        validate_payload(unknown)
    wrong = {**payload, "final_root": str(layout["review"])}
    with pytest.raises(RequestValidationError, match="TV canónica"):
        validate_payload(wrong)


def test_success_persists_full_journal_and_terminal_replays(layout):
    payload = _payload(layout)
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    accepted = coordinator.submit(payload)
    terminal = coordinator.wait("job-1")
    replay = coordinator.submit(payload)

    assert accepted.http_status == 202 and accepted.payload["status"] == "accepted"
    assert terminal.http_status == 200
    assert terminal.payload["result"]["status"] == "done"
    assert replay.http_status == 200 and replay.payload == terminal.payload
    assert len(publisher.calls) == 1
    journal_lines = (layout["reports"] / "job-1/journal.jsonl").read_text("utf-8")
    assert [__import__("json").loads(line)["state"] for line in journal_lines.splitlines()] == [
        "PREPARED", "PROCESSING", "VERIFIED", "COMMITTING", "COMMITTED", "COMMITTED"
    ]


@pytest.mark.parametrize("suffix", [".mkv", ".mp4", ".avi"])
def test_real_delivery_replaces_filebot_original_with_single_final_mkv(
    layout,
    suffix,
):
    relative = f"Serie/Season 01/Serie.S01E01{suffix}"
    payload = _payload(layout, videos=[(relative, b"source")])
    coordinator = _real_delivery_coordinator(layout, FakeProcessor())

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    final = layout["tv"] / "Serie/Season 01/Serie.S01E01.mkv"
    assert result["status"] == "done"
    assert final.read_bytes() == f"processed-{relative}".encode()
    assert {
        path.name
        for path in final.parent.iterdir()
        if path.is_file()
    } == {"Serie.S01E01.mkv"}
    assert not (Path(payload["job_root"]) / "series_work").exists()


def test_done_manifest_lists_only_new_pack_without_rehashing_video(layout):
    payload = _payload(
        layout,
        videos=[("Serie/Season 01/Serie.S01E02.mkv", b"new-source")],
    )
    existing_video = layout["tv"] / "Serie/Season 01/Serie.S01E01.mkv"
    existing_video.parent.mkdir(parents=True)
    existing_video.write_bytes(b"existing-video")
    poster = layout["tv"] / "Serie/poster.jpg"
    poster.write_bytes(b"poster")
    coordinator = _coordinator(layout, processor=FakeSidecarProcessor())
    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]
    published_manifest = result["published_manifest"]
    entries = published_manifest["entries"]

    assert published_manifest["schema"] == "series-published-manifest-v1"
    assert [entry["path"] for entry in entries] == [
        "Serie/Season 01/Serie.S01E02.es.forced.srt",
        "Serie/Season 01/Serie.S01E02.mkv",
    ]
    assert result["published"] == ["Serie/Season 01/Serie.S01E02.mkv"]
    for entry in entries:
        path = layout["tv"] / Path(*PurePosixPath(entry["path"]).parts)
        assert entry["size"] == path.stat().st_size
        assert entry["content_sha256"] == ""
    encoded_entries = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert published_manifest["digest"] == hashlib.sha256(encoded_entries).hexdigest()
    assert all(entry["path"].split("/")[-1] != core_module.MARKER_NAME for entry in entries)
    assert existing_video.is_file()
    assert poster.is_file()
    assert not (layout["tv"] / "Serie" / core_module.MARKER_NAME).exists()


def test_tampered_terminal_manifest_cannot_replace_journal_bound_digest(layout):
    payload = _payload(layout)
    coordinator = _coordinator(layout)
    coordinator.submit(payload)
    assert coordinator.wait("job-1").http_status == 200
    result_path = layout["reports"] / "job-1/series_result.json"
    result = json.loads(result_path.read_text("utf-8"))
    entries = result["published_manifest"]["entries"]
    entries[0]["size"] += 1
    result["published_manifest"]["digest"] = hashlib.sha256(
        json.dumps(
            entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ServiceUnavailable, match="journal durable"):
        _coordinator(layout).submit(payload)


def test_processed_output_stays_inside_filebot_output_until_direct_move(layout):
    payload = _payload(layout)
    coordinator = _coordinator(layout)

    prepared, existed, _record = coordinator._candidate(validate_payload(payload))

    assert existed is False
    assert prepared.prepared_series_root is not None
    assert prepared.prepared_series_root == (
        Path(payload["job_root"]) / "series_filebot_output/Serie"
    )
    assert not (Path(payload["job_root"]) / "series_work").exists()
    assert list(layout["tv"].iterdir()) == []


def test_same_active_job_is_202_and_other_job_is_busy(layout):
    entered = threading.Event()
    release = threading.Event()
    processor = FakeProcessor(entered=entered, release=release)
    coordinator = _coordinator(layout, processor=processor)
    first = _payload(layout, "job-1")
    second = _payload(layout, "job-2")

    assert coordinator.submit(first).http_status == 202
    assert entered.wait(timeout=1)
    assert coordinator.submit(first).payload["status"] == "active"
    with pytest.raises(SeriesWorkerBusy):
        coordinator.submit(second)
    release.set()
    assert coordinator.wait("job-1").http_status == 200


def test_same_active_job_with_different_payload_is_conflict(layout):
    entered = threading.Event()
    release = threading.Event()
    coordinator = _coordinator(
        layout, processor=FakeProcessor(entered=entered, release=release)
    )
    payload = _payload(layout)
    coordinator.submit(payload)
    assert entered.wait(timeout=3)
    changed = {
        **payload,
        "callback_url": "http://arr-orchestrator:8787/jobs/job-1/events",
    }

    with pytest.raises(JobConflict):
        coordinator.submit(changed)
    release.set()


def test_external_heavy_lock_busy_is_409_with_durable_reservation_and_no_hash(
    layout,
    monkeypatch,
):
    payload = _payload(layout)
    original_discover = core_module.discover_manifest
    discoveries = []
    monkeypatch.setattr(
        core_module,
        "discover_manifest",
        lambda *_args, **_kwargs: discoveries.append(True)
        or (_ for _ in ()).throw(AssertionError("no debe descubrir con lock ocupado")),
    )

    class BusyContext:
        def __enter__(self):
            raise HeavyLockTimeout("busy")

        def __exit__(self, *args):
            return None

    coordinator = _coordinator(layout, lock_factory=lambda *a, **k: BusyContext())
    for _attempt in range(2):
        with pytest.raises(SeriesWorkerBusy):
            coordinator.submit(payload)
    request = json.loads(
        (layout["reports"] / "job-1/request.json").read_text("utf-8")
    )
    assert request["schema"] == "series-worker-request-v2"
    assert request["stage"] == "reserved"
    assert discoveries == []
    assert not (layout["reports"] / "job-1/manifest.json").exists()

    monkeypatch.setattr(core_module, "discover_manifest", original_discover)
    restarted = _coordinator(layout)
    assert restarted.submit(payload).http_status == 202
    assert restarted.wait("job-1").payload["result"]["status"] == "done"


def test_submit_returns_before_slow_manifest_and_active_retry_never_rehashes(
    layout,
    monkeypatch,
):
    payload = _payload(layout)
    original_discover = core_module.discover_manifest
    entered = threading.Event()
    release = threading.Event()
    discoveries = []
    lock_state = {"held": False, "enters": 0, "exits": 0}

    class RecordingLock:
        def __enter__(self):
            lock_state["held"] = True
            lock_state["enters"] += 1
            return {"enabled": True}

        def __exit__(self, *_args):
            lock_state["held"] = False
            lock_state["exits"] += 1

    def slow_discover(*args, **kwargs):
        assert lock_state == {"held": False, "enters": 1, "exits": 1}
        discoveries.append(True)
        entered.set()
        assert release.wait(timeout=3)
        return original_discover(*args, **kwargs)

    monkeypatch.setattr(core_module, "discover_manifest", slow_discover)
    coordinator = _coordinator(
        layout,
        lock_factory=lambda *_args, **_kwargs: RecordingLock(),
    )

    started = time.monotonic()
    accepted = coordinator.submit(payload)
    elapsed = time.monotonic() - started

    assert accepted.http_status == 202
    assert accepted.payload["status"] == "accepted"
    assert elapsed < 1.0
    assert entered.wait(timeout=1)
    assert coordinator.submit(payload).payload["status"] == "active"
    assert discoveries == [True]
    request = json.loads(
        (layout["reports"] / "job-1/request.json").read_text("utf-8")
    )
    assert request["stage"] == "preparing"
    release.set()
    assert coordinator.wait("job-1").payload["result"]["status"] == "done"
    assert lock_state == {"held": False, "enters": 2, "exits": 2}


def test_restart_reuses_manifest_written_during_interrupted_preparation(
    layout,
    monkeypatch,
):
    payload = _payload(layout)
    original_write = core_module.DurableJournal.write_json_atomic
    interrupted = {"done": False}

    class SimulatedProcessDeath(BaseException):
        pass

    def cut_after_manifest(self, name, value):
        result = original_write(self, name, value)
        if str(name) == "manifest.json" and not interrupted["done"]:
            interrupted["done"] = True
            raise SimulatedProcessDeath("corte tras manifiesto reservado")
        return result

    monkeypatch.setattr(
        core_module.DurableJournal,
        "write_json_atomic",
        cut_after_manifest,
    )
    first = _coordinator(layout)
    reserved, record = first._new_reservation(validate_payload(payload))
    with pytest.raises(SimulatedProcessDeath):
        first._prepare_reserved(reserved, record)
    request = json.loads(
        (layout["reports"] / "job-1/request.json").read_text("utf-8")
    )
    assert request["stage"] == "preparing"
    assert (layout["reports"] / "job-1/manifest.json").is_file()

    monkeypatch.setattr(
        core_module.DurableJournal,
        "write_json_atomic",
        original_write,
    )
    monkeypatch.setattr(
        core_module,
        "discover_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("la recuperación no debe rehashear el pack")
        ),
    )
    restarted = _coordinator(layout)
    assert restarted.submit(payload).http_status == 202
    terminal = restarted.wait("job-1")
    assert terminal.http_status == 200
    assert terminal.payload["result"]["status"] == "done"


def test_second_lock_busy_retries_prepared_job_without_rediscovering_or_replanning(
    layout,
    monkeypatch,
):
    payload = _payload(layout)
    original_discover = core_module.discover_manifest
    original_plan = core_module.plan_collisions
    discoveries = []
    plans = []

    def counted_discover(*args, **kwargs):
        discoveries.append(True)
        return original_discover(*args, **kwargs)

    def counted_plan(*args, **kwargs):
        plans.append(True)
        return original_plan(*args, **kwargs)

    class OpenLock:
        def __enter__(self):
            return {"enabled": True}

        def __exit__(self, *_args):
            return None

    class BusyLock(OpenLock):
        def __enter__(self):
            raise HeavyLockTimeout("busy after prepare")

    lock_calls = []

    def first_factory(*_args, **_kwargs):
        lock_calls.append(True)
        return OpenLock() if len(lock_calls) == 1 else BusyLock()

    monkeypatch.setattr(core_module, "discover_manifest", counted_discover)
    monkeypatch.setattr(core_module, "plan_collisions", counted_plan)
    coordinator = _coordinator(layout, lock_factory=first_factory)
    assert coordinator.submit(payload).http_status == 202
    coordinator._threads["job-1"].join(timeout=3)

    request = json.loads(
        (layout["reports"] / "job-1/request.json").read_text("utf-8")
    )
    assert request["stage"] == "prepared"
    assert request["collision_plan"]["pending"] == [
        "Serie/Season 01/Serie.S01E01.mkv"
    ]
    assert discoveries == [True]
    assert plans == [True]

    coordinator.lock_factory = lambda *_args, **_kwargs: OpenLock()
    assert coordinator.submit(payload).http_status == 202
    terminal = coordinator.wait("job-1")

    assert terminal.payload["result"]["status"] == "done"
    assert discoveries == [True]
    assert plans == [True, True]


def test_manifest_preparation_error_becomes_durable_terminal_review(
    layout,
    monkeypatch,
):
    payload = _payload(layout)
    monkeypatch.setattr(
        core_module,
        "discover_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            core_module.ManifestError("manifest controlado inválido")
        ),
    )
    coordinator = _coordinator(layout)

    assert coordinator.submit(payload).http_status == 202
    terminal = coordinator.wait("job-1")

    assert terminal.http_status == 200
    result = terminal.payload["result"]
    assert result["status"] == "review"
    assert result["review_reasons"] == [
        "preparacion_fallida:manifest controlado inválido"
    ]
    assert result["review_path"]
    assert json.loads(
        (layout["reports"] / "job-1/journal.json").read_text("utf-8")
    )["state"] == "ROLLED_BACK"
    assert coordinator.submit(payload).payload == terminal.payload


def test_audiovisual_lock_is_released_before_publish_and_final_hash(
    layout,
    monkeypatch,
):
    payload = _payload(layout)
    state = {"held": False, "enters": 0, "exits": 0}

    class RecordingLock:
        def __enter__(self):
            assert state["held"] is False
            state["held"] = True
            state["enters"] += 1
            return {"enabled": True}

        def __exit__(self, *_args):
            assert state["held"] is True
            state["held"] = False
            state["exits"] += 1

    class CheckedProcessor(FakeProcessor):
        def process(self, **kwargs):
            assert state["held"] is True
            return super().process(**kwargs)

    publisher = FakePublisher()

    def checked_publisher(*args, **kwargs):
        assert state["held"] is False
        return publisher(*args, **kwargs)

    original_published_manifest = core_module._published_manifest

    def checked_manifest(prepared):
        assert state["held"] is False
        return original_published_manifest(prepared)

    monkeypatch.setattr(core_module, "_published_manifest", checked_manifest)
    coordinator = _coordinator(
        layout,
        processor=CheckedProcessor(),
        publisher=checked_publisher,
        lock_factory=lambda *_args, **_kwargs: RecordingLock(),
    )

    coordinator.submit(payload)
    terminal = coordinator.wait("job-1")

    assert terminal.payload["result"]["status"] == "done"
    assert state == {"held": False, "enters": 2, "exits": 2}


def test_rules_snapshot_is_frozen_while_job_runs(layout):
    entered = threading.Event()
    release = threading.Event()
    snapshots = []
    coordinator = _coordinator(
        layout,
        processor=FakeProcessor(entered=entered, release=release, snapshots=snapshots),
    )
    payload = _payload(layout)
    original = coordinator.rules_store.snapshot()
    coordinator.submit(payload)
    assert entered.wait(timeout=1)
    changed = deepcopy(original.rules)
    changed["audio"]["bitrate_ac3"] = "448k"
    coordinator.rules_store.save(
        {"rules": changed, "expected_fingerprint": original.fingerprint}
    )
    release.set()
    coordinator.wait("job-1")

    assert snapshots[0].fingerprint == original.fingerprint
    assert snapshots[0].rules["audio"]["bitrate_ac3"] == "640k"


def test_second_episode_failure_reviews_complete_pack_without_publishing(
    layout,
    monkeypatch,
):
    payload = _payload(
        layout,
        videos=[
            ("Serie/Season 01/Serie.S01E01.mkv", b"one"),
            ("Serie/Season 01/Serie.S01E02.mkv", b"two"),
        ],
    )
    publisher = FakePublisher()
    callbacks = []
    monkeypatch.setattr(
        core_module,
        "_emit_callback",
        lambda _prepared, phase, event_type, message: callbacks.append(
            (phase, event_type, message)
        ),
    )
    coordinator = _coordinator(
        layout, processor=FakeProcessor(fail_on=2), publisher=publisher
    )

    coordinator.submit(payload)
    terminal = coordinator.wait("job-1")

    result = terminal.payload["result"]
    assert result["status"] == "review"
    assert result["published"] == []
    assert result["review_reasons"] == ["procesamiento_fallido:fallo controlado"]
    assert publisher.calls == []
    assert not (layout["tv"] / "Serie").exists()
    review = _assert_review_matches_source(layout, payload, result)
    assert _tree_contents(review, ignore_root_names=core_module._REVIEW_METADATA) == {
        "Serie/Season 01/Serie.S01E01.mkv": b"one",
        "Serie/Season 01/Serie.S01E02.mkv": b"two",
    }
    assert callbacks[-1] == (
        "series_review",
        "finished",
        "Procesamiento fallido; pack enviado a revisión",
    )


def test_processing_failure_releases_heavy_lock_before_review_copy(
    layout,
    monkeypatch,
):
    payload = _payload(layout)
    lock_state = {"held": False, "exits": 0}

    class RecordingLock:
        def __enter__(self):
            assert lock_state["held"] is False
            lock_state["held"] = True
            return {"enabled": True}

        def __exit__(self, *_args):
            assert lock_state["held"] is True
            lock_state["held"] = False
            lock_state["exits"] += 1

    original_review = core_module._review_pack
    review_started = threading.Event()

    def checked_review(prepared, reasons):
        assert lock_state["held"] is False
        review_started.set()
        return original_review(prepared, reasons)

    monkeypatch.setattr(core_module, "_review_pack", checked_review)
    coordinator = _coordinator(
        layout,
        processor=FakeProcessor(fail_on=1),
        lock_factory=lambda *_args, **_kwargs: RecordingLock(),
    )

    assert coordinator.submit(payload).http_status == 202
    terminal = coordinator.wait("job-1")

    assert terminal.payload["result"]["status"] == "review"
    assert review_started.is_set()
    assert lock_state == {"held": False, "exits": 2}


def test_enospc_on_second_episode_never_publishes_and_preserves_complete_pack(
    layout,
    monkeypatch,
):
    payload = _payload(
        layout,
        videos=[
            ("Serie/Season 01/Serie.S01E01.mkv", b"source-one"),
            ("Serie/Season 01/Serie.S01E02.mkv", b"source-two"),
        ],
    )
    source_root = Path(payload["source_root"])
    source_before = _tree_contents(source_root)
    original_write_bytes = Path.write_bytes

    def fail_second_processed_write(path, data):
        if path.name == "Serie.S01E02.limpio.mkv":
            raise OSError(errno.ENOSPC, "controlled no space left on device")
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_second_processed_write)
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    terminal = coordinator.wait("job-1")
    result = terminal.payload["result"]

    assert result["status"] == "review"
    assert result["review_reasons"][0].startswith("procesamiento_fallido:")
    assert "no space left on device" in result["review_reasons"][0]
    assert str(source_root) not in result["review_reasons"][0]
    assert result["published"] == []
    assert result["review_path"]
    assert publisher.calls == []
    assert not (layout["tv"] / "Serie").exists()
    assert not list(layout["tv"].glob(".*series-worker*"))
    review = _assert_review_matches_source(layout, payload, result)
    assert _tree_contents(review, ignore_root_names=core_module._REVIEW_METADATA) == source_before
    snapshot = json.loads(
        (layout["reports"] / "job-1/journal.json").read_text("utf-8")
    )
    assert snapshot["state"] == "ROLLED_BACK"
    assert snapshot["details"]["terminal_status"] == "review"
    persisted = json.loads(
        (layout["reports"] / "job-1/series_result.json").read_text("utf-8")
    )
    assert persisted == result


def test_processing_failure_and_review_failure_returns_failed_with_source_intact(
    layout,
    monkeypatch,
):
    payload = _payload(
        layout,
        videos=[
            ("Serie/Season 01/Serie.S01E01.mkv", b"source-one"),
            ("Serie/Season 01/Serie.S01E02.mkv", b"source-two"),
        ],
    )
    source_before = _tree_contents(payload["source_root"])
    callbacks = []

    def fail_review_move(*_args, **_kwargs):
        raise OSError(errno.EIO, "controlled review move failure")

    monkeypatch.setattr(core_module.shutil, "move", fail_review_move)
    monkeypatch.setattr(
        core_module,
        "_emit_callback",
        lambda _prepared, phase, event_type, message: callbacks.append(
            (phase, event_type, message)
        ),
    )
    publisher = FakePublisher()
    coordinator = _coordinator(
        layout,
        processor=FakeProcessor(fail_on=2),
        publisher=publisher,
    )

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "failed"
    assert result["error_code"] == "OSError"
    assert "controlled review move failure" in result["error"]
    assert result["published"] == []
    assert result["review_path"] == ""
    assert result["provisional"] == []
    assert publisher.calls == []
    assert _tree_contents(payload["source_root"]) == source_before
    assert not (layout["tv"] / "Serie").exists()
    assert list(layout["review"].iterdir()) == []
    assert callbacks[-1] == (
        "series_review",
        "error",
        "Falló la revisión tras el error de procesamiento",
    )


@pytest.mark.parametrize(
    "videos,reason_prefix",
    [
        (
            [
                ("Serie A/Season 01/Serie.A.S01E01.mkv", b"a"),
                ("Serie B/Season 01/Serie.B.S01E01.mkv", b"b"),
            ],
            "varias_series:",
        ),
        ([("Serie/bonus.mkv", b"bonus")], "episodio_no_reconocido:"),
    ],
)
def test_invalid_pack_goes_to_complete_review(layout, videos, reason_prefix):
    payload = _payload(
        layout,
        videos=videos,
        sidecars=[("LEEME.nfo", "metadata")],
    )
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert any(reason.startswith(reason_prefix) for reason in result["review_reasons"])
    review = layout["review"] / result["review_path"]
    assert (review / "LEEME.nfo").read_text("utf-8") == "metadata"
    assert publisher.calls == []


def test_different_or_other_extension_collision_reviews_entire_pack(layout):
    payload = _payload(layout)
    existing = layout["tv"] / "Serie/Season 01/Serie.S01E01.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")
    coordinator = _coordinator(layout)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert any(reason.startswith("colision_otra_extension:") for reason in result["review_reasons"])
    assert existing.read_bytes() == b"existing"


def test_range_collision_detects_existing_intermediate_episode(layout):
    payload = _payload(
        layout,
        videos=[("Serie/Season 01/Serie.S01E01-E03E05.mkv", b"range-pack")],
    )
    existing = layout["tv"] / "Serie/Season 01/Serie.S01E02.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing-e02")
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert any(
        reason.startswith("colision_otro_nombre:")
        for reason in result["review_reasons"]
    )
    assert publisher.calls == []
    assert existing.read_bytes() == b"existing-e02"


@pytest.mark.parametrize("sidecar", [False, True])
def test_verified_output_is_not_rehashed_before_direct_move(
    layout,
    sidecar,
):
    payload = _payload(layout)

    class MutatingProcessor(FakeSidecarProcessor if sidecar else FakeProcessor):
        def process(self, **kwargs):
            result = super().process(**kwargs)
            episode = result.episodes[0]
            if sidecar:
                relative = episode.subtitle_provisional_relpath
                assert relative is not None
            else:
                relative = episode.provisional_relpath
            _mutate_same_size_and_mtime(Path(kwargs["job_root"]) / relative)
            return result

    publisher = FakePublisher()
    coordinator = _coordinator(
        layout,
        processor=MutatingProcessor(),
        publisher=publisher,
    )

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "done"
    assert len(publisher.calls) == 1
    assert (layout["tv"] / "Serie/Season 01/Serie.S01E01.mkv").is_file()


def test_unicode_compatibility_collision_in_library_reviews_entire_pack(layout):
    payload = _payload(layout)
    existing = layout["tv"] / "Serie/Ｓｅａｓｏｎ ０１/Serie.Ｓ０１Ｅ０１.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"different")
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert any(
        reason.startswith("directorio_no_canonico_tv:")
        for reason in result["review_reasons"]
    )
    assert existing.read_bytes() == b"different"
    assert publisher.calls == []


def test_library_drift_during_processing_fails_closed_to_review(layout):
    entered = threading.Event()
    release = threading.Event()
    payload = _payload(layout)
    publisher = FakePublisher()
    coordinator = _coordinator(
        layout,
        processor=FakeProcessor(entered=entered, release=release),
        publisher=publisher,
    )
    coordinator.submit(payload)
    assert entered.wait(timeout=1)
    appeared = layout["tv"] / "Serie/Season 01/Serie.S01E01.mp4"
    appeared.parent.mkdir(parents=True)
    appeared.write_bytes(b"drift")
    release.set()

    result = coordinator.wait("job-1").payload["result"]
    assert result["status"] == "review"
    assert "biblioteca_cambio_durante_procesado" in result["review_reasons"]
    assert publisher.calls == []
    assert appeared.read_bytes() == b"drift"


def test_identical_collision_goes_to_review_without_hashing_or_processing(layout):
    payload = _payload(layout)
    source = Path(payload["source_root"]) / "Serie/Season 01/Serie.S01E01.mkv"
    existing = layout["tv"] / "Serie/Season 01/Serie.S01E01.mkv"
    existing.parent.mkdir(parents=True)
    shutil.copy2(source, existing)

    class MustNotProcess:
        def process(self, **kwargs):
            raise AssertionError("un episodio satisfecho no se reprocesa")

    publisher = FakePublisher()
    coordinator = _coordinator(
        layout,
        processor=MustNotProcess(),
        publisher=publisher,
    )
    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert any(reason.startswith("colision_existente:") for reason in result["review_reasons"])
    assert publisher.calls == []


def test_unicode_compatibility_identical_collision_is_reviewed_fail_closed(layout):
    payload = _payload(layout)
    source = Path(payload["source_root"]) / "Serie/Season 01/Serie.S01E01.mkv"
    existing = layout["tv"] / "Serie/Ｓｅａｓｏｎ ０１/Serie.Ｓ０１Ｅ０１.mkv"
    existing.parent.mkdir(parents=True)
    shutil.copy2(source, existing)

    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)
    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert any(
        reason.startswith("directorio_no_canonico_tv:")
        for reason in result["review_reasons"]
    )
    assert publisher.calls == []


@pytest.mark.parametrize(
    "existing_directory",
    ["season 01", "Season 1", "Temporada 01"],
)
def test_noncanonical_existing_season_directory_reviews_new_episode(
    layout,
    existing_directory,
):
    payload = _payload(
        layout,
        videos=[("Serie/Season 01/Serie.S01E02.mkv", b"new")],
    )
    existing = layout["tv"] / "Serie" / existing_directory / "Serie.S01E01.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert any(
        reason.startswith(("directorio_no_canonico_tv:", "temporada_en_directorio_distinto_tv:"))
        for reason in result["review_reasons"]
    )
    assert publisher.calls == []
    assert existing.read_bytes() == b"old"


def test_empty_unicode_equivalent_season_directory_reviews_new_episode(layout):
    payload = _payload(layout)
    existing_directory = layout["tv"] / "Serie" / "Ｓｅａｓｏｎ ０１"
    existing_directory.mkdir(parents=True)
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert any(
        reason.startswith("directorio_no_canonico_tv:")
        for reason in result["review_reasons"]
    )
    assert publisher.calls == []


def test_restart_resumes_same_prepared_request(layout):
    payload = _payload(layout)
    first = _coordinator(layout)
    validated = validate_payload(payload)
    prepared, existed, record = first._candidate(validated)
    assert existed is False
    first._persist_prepared(prepared, record)

    publisher = FakePublisher()
    restarted = _coordinator(layout, publisher=publisher)
    accepted = restarted.submit(payload)
    terminal = restarted.wait("job-1")

    assert accepted.http_status == 202
    assert accepted.payload["status"] == "active"
    assert terminal.payload["result"]["status"] == "done"
    assert len(publisher.calls) == 1


def test_rolled_back_replay_cleans_delivery_before_terminal_result(layout):
    payload = _payload(layout)
    first = _coordinator(layout)
    prepared, existed, record = first._candidate(validate_payload(payload))
    assert existed is False and prepared.prepared_series_root is not None
    first._persist_prepared(prepared, record)
    partial = prepared.prepared_series_root / "Season 01/Serie.S01E01.limpio.mkv"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"partial")
    shadow = layout["tv"] / ".Serie.series-worker-owned.shadow"
    shadow.mkdir()
    (shadow / "old.mkv").write_bytes(b"old")
    prepared.journal.transition("PROCESSING")
    prepared.journal.transition("ROLLED_BACK", failure_code="controlled")
    recovery_calls = []

    def recover(*_args):
        recovery_calls.append(True)
        shutil.rmtree(shadow)
        return {"status": "rolled_back"}

    restarted = _coordinator(layout)
    restarted.recoverer = recover
    accepted = restarted.submit(payload)
    terminal = restarted.wait("job-1")

    assert accepted.http_status == 202
    assert terminal.payload["result"]["status"] == "failed"
    assert recovery_calls == [True]
    assert not shadow.exists()
    assert not partial.exists()
    assert (
        prepared.prepared_series_root / "Season 01/Serie.S01E01.mkv"
    ).is_file()


def test_restart_replays_manifest_with_unicode_equivalent_sidecars(layout):
    payload = _payload(
        layout,
        sidecars=[
            (
                "Serie/Season 01/Serie.S01E01.Café.srt",
                "1\n00:00:00,000 --> 00:00:01,000\nHola\n",
            ),
            (
                "Serie/Season 01/Serie.S01E01.Cafe\u0301.srt",
                "1\n00:00:00,000 --> 00:00:01,000\nHola\n",
            ),
        ],
    )
    first = _coordinator(layout)
    validated = validate_payload(payload)
    prepared, existed, record = first._candidate(validated)
    assert existed is False
    first._persist_prepared(prepared, record)

    restarted = _coordinator(layout)
    accepted = restarted.submit(payload)
    terminal = restarted.wait("job-1")

    assert accepted.http_status == 202
    assert terminal.payload["result"]["status"] == "review"
    assert any(
        reason.startswith("colision_sidecar_casefold:")
        for reason in terminal.payload["result"]["review_reasons"]
    )


def test_missing_tools_return_503_and_atomic_probe_is_not_called(layout):
    payload = _payload(layout)
    store = RulesStore(config_path=layout["root"] / "rules.json")
    missing = SeriesCoordinator(
        rules_store=store,
        atomic_preflight=lambda root: {"supported": True},
        tool_checker=lambda: ["mkvpropedit"],
    )
    with pytest.raises(ServiceUnavailable, match="mkvpropedit") as missing_error:
        missing.submit(payload)
    assert missing_error.value.http_status == 503
    assert not (layout["reports"] / "job-1").exists()
    assert list(layout["review"].iterdir()) == []

    calls = []
    atomic = SeriesCoordinator(
        rules_store=store,
        atomic_preflight=lambda root: calls.append(root),
        tool_checker=lambda: [],
        processor_factory=lambda: FakeProcessor(),
        publisher=FakePublisher(),
        lock_factory=lambda *args, **kwargs: nullcontext({"enabled": True}),
    )
    assert atomic.health().http_status == 200
    assert calls == []


def test_terminal_replay_survives_source_cleanup_and_new_global_rules(layout):
    payload = _payload(layout)
    coordinator = _coordinator(layout)
    coordinator.submit(payload)
    terminal = coordinator.wait("job-1")
    current = coordinator.rules_store.snapshot()
    changed_rules = deepcopy(current.rules)
    changed_rules["audio"]["bitrate_ac3"] = "448k"
    coordinator.rules_store.save(
        {"rules": changed_rules, "expected_fingerprint": current.fingerprint}
    )
    shutil.rmtree(payload["job_root"])

    replay = coordinator.submit(payload)

    assert replay.http_status == 200
    assert replay.payload == terminal.payload
    with pytest.raises(JobConflict):
        coordinator.submit(
            {
                **payload,
                "callback_url": "http://arr-orchestrator:8787/jobs/job-1/events",
            }
        )


@pytest.mark.parametrize("artifact", ["rules_snapshot.json", "manifest.json"])
def test_tampered_durable_snapshot_or_manifest_fails_closed(layout, artifact):
    payload = _payload(layout)
    coordinator = _coordinator(layout)
    validated = validate_payload(payload)
    prepared, existed, record = coordinator._candidate(validated)
    assert existed is False
    coordinator._persist_prepared(prepared, record)
    path = layout["reports"] / "job-1" / artifact
    document = json.loads(path.read_text("utf-8"))
    if artifact == "rules_snapshot.json":
        document["rules"]["audio"]["bitrate_ac3"] = "448k"
    else:
        document["entries"][0]["content_sha256"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")

    restarted = _coordinator(layout)
    assert restarted.submit(payload).http_status == 202
    status = restarted.wait("job-1", timeout=10)
    assert status.http_status == 200
    assert status.payload["result"]["status"] == "review"
    assert "durable" in status.payload["result"]["review_reasons"][0]
    assert not (layout["tv"] / "Serie").exists()


@pytest.mark.parametrize(
    "cut_after",
    ["manifest.json", "rules_snapshot.json", "request.json", "PREPARED"],
)
def test_bootstrap_recovers_after_each_durable_boundary(layout, cut_after):
    payload = _payload(layout)
    first = _coordinator(layout)
    prepared, existed, record = first._candidate(validate_payload(payload))
    assert existed is False
    if cut_after == "PREPARED":
        original_transition = prepared.journal.transition

        def interrupted_transition(state, **details):
            result = original_transition(state, **details)
            if state == "PREPARED":
                raise OSError("corte tras PREPARED")
            return result

        prepared.journal.transition = interrupted_transition
    else:
        original_write = prepared.journal.write_json_atomic

        def interrupted_write(name, value):
            result = original_write(name, value)
            if str(name) == cut_after:
                raise OSError(f"corte tras {cut_after}")
            return result

        prepared.journal.write_json_atomic = interrupted_write

    with pytest.raises(OSError, match="corte"):
        first._persist_prepared(prepared, record)

    restarted = _coordinator(layout)
    accepted = restarted.submit(payload)
    terminal = restarted.wait("job-1")

    assert accepted.http_status == 202
    assert terminal.payload["result"]["status"] == "done"


@pytest.mark.parametrize("terminal_status", ["review", "failed"])
def test_rolled_back_reconstructs_result_after_crash_before_result_file(
    layout,
    terminal_status,
    monkeypatch,
):
    if terminal_status == "review":
        payload = _payload(
            layout,
            videos=[("Serie/bonus.mkv", b"bonus")],
        )
        coordinator = _coordinator(layout)
    else:
        payload = _payload(layout)
        coordinator = _coordinator(layout, processor=FakeProcessor(fail_on=1))

        def fail_review_move(*_args, **_kwargs):
            raise OSError(errno.EIO, "controlled review move failure")

        monkeypatch.setattr(core_module.shutil, "move", fail_review_move)

    def crash_before_result(*_args, **_kwargs):
        raise OSError("corte antes de series_result")

    coordinator._write_result = crash_before_result
    coordinator.submit(payload)
    thread = coordinator._threads["job-1"]
    thread.join(timeout=3)

    journal = core_module.DurableJournal(layout["reports"] / "job-1")
    assert journal.state == "ROLLED_BACK"
    assert not (layout["reports"] / "job-1/series_result.json").exists()

    replay = _coordinator(layout).submit(payload)

    assert replay.http_status == 200
    assert replay.payload["result"]["status"] == terminal_status


def test_status_does_not_publish_a_result_that_contradicts_journal(layout):
    payload = _payload(layout)
    coordinator = _coordinator(layout)
    prepared, _, record = coordinator._candidate(validate_payload(payload))
    coordinator._persist_prepared(prepared, record)
    prepared.journal.transition("PROCESSING")
    prepared.journal.write_json_atomic(
        "series_result.json",
        {
            "status": "done",
            "job_id": "job-1",
            "kind": "series",
            "delivery": {"cleanup_pending": False},
        },
    )

    status = coordinator.status("job-1")

    assert status.http_status == 202
    assert status.payload["status"] == "recoverable"
    assert status.payload["journal_state"] == "PROCESSING"


def test_status_retries_after_transient_error_in_same_process(layout):
    payload = _payload(layout)
    coordinator = _coordinator(layout)
    coordinator._new_reservation(validate_payload(payload))
    coordinator._last_errors["job-1"] = "fallo transitorio anterior"

    resumed = coordinator.status("job-1")
    terminal = coordinator.wait("job-1")

    assert resumed.http_status == 202
    assert resumed.payload["status"] in {"accepted", "active"}
    assert terminal.http_status == 200
    assert terminal.payload["result"]["status"] == "done"
    assert "job-1" not in coordinator._last_errors


@pytest.mark.parametrize("entrypoint", ["submit", "status"])
def test_report_job_symlink_is_rejected_without_external_write(
    layout,
    entrypoint,
):
    payload = _payload(layout)
    outside = layout["root"] / "outside-reports"
    outside.mkdir()
    job_dir = layout["reports"] / "job-1"
    try:
        os.symlink(outside, job_dir, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink no disponible: {error}")
    coordinator = _coordinator(layout)

    with pytest.raises(ServiceUnavailable, match="enlace simbólico"):
        if entrypoint == "submit":
            coordinator.submit(payload)
        else:
            coordinator.status("job-1")

    assert list(outside.iterdir()) == []


def test_equivalent_nfkc_series_roots_are_always_ambiguous(layout):
    payload = _payload(layout)
    (layout["tv"] / "Serie").mkdir()
    (layout["tv"] / "Ｓｅｒｉｅ").mkdir()
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert "varias_raices_casefold_en_tv" in result["review_reasons"]
    assert publisher.calls == []


def test_existing_video_with_new_sidecar_goes_to_review_without_reprocessing(layout):
    payload = _payload(
        layout,
        sidecars=[
            (
                "Serie/Season 01/Serie.S01E01.es.srt",
                "1\n00:00:00,000 --> 00:00:01,000\nHola\n",
            )
        ],
    )
    source = Path(payload["source_root"]) / "Serie/Season 01/Serie.S01E01.mkv"
    existing = layout["tv"] / "Serie/Season 01/Serie.S01E01.mkv"
    existing.parent.mkdir(parents=True)
    shutil.copy2(source, existing)

    processor = FakeSidecarProcessor()
    coordinator = _coordinator(layout, processor=processor)
    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert processor.calls == 0
    assert not (layout["tv"] / "Serie/Season 01/Serie.S01E01.es.forced.srt").exists()


def test_real_publisher_keeps_existing_video_and_sidecar_untouched(layout):
    subtitle = "1\n00:00:00,000 --> 00:00:01,000\nHola\n"
    payload = _payload(
        layout,
        sidecars=[("Serie/Season 01/Serie.S01E01.es.srt", subtitle)],
    )
    source = Path(payload["source_root"]) / "Serie/Season 01/Serie.S01E01.mkv"
    final_episode = layout["tv"] / "Serie/Season 01/Serie.S01E01.mkv"
    final_sidecar = final_episode.with_name("Serie.S01E01.es.forced.srt")
    final_episode.parent.mkdir(parents=True)
    source_bytes = source.read_bytes()
    shutil.copy2(source, final_episode)
    final_sidecar.write_text(subtitle, encoding="utf-8")
    processor = FakeSidecarProcessor()
    coordinator = _real_delivery_coordinator(layout, processor)

    coordinator.submit(payload)
    terminal = coordinator.wait("job-1")

    assert terminal.http_status == 200
    result = terminal.payload["result"]
    assert result["status"] == "review"
    assert processor.calls == 0
    assert final_episode.read_bytes() == source_bytes
    assert final_sidecar.read_text(encoding="utf-8") == subtitle
    _assert_review_matches_source(layout, payload, result)
    assert not list(layout["tv"].glob(".*series-worker*"))


def test_existing_target_is_never_authorized_for_overwrite(layout):
    subtitle = "1\n00:00:00,000 --> 00:00:01,000\nHola\n"
    payload = _payload(
        layout,
        sidecars=[("Serie/Season 01/Serie.S01E01.es.srt", subtitle)],
    )
    source = Path(payload["source_root"]) / "Serie/Season 01/Serie.S01E01.mkv"
    final_episode = layout["tv"] / "Serie/Season 01/Serie.S01E01.mkv"
    final_episode.parent.mkdir(parents=True)
    source_bytes = source.read_bytes()
    shutil.copy2(source, final_episode)
    coordinator = _real_delivery_coordinator(layout, FakeSidecarProcessor())

    coordinator.submit(payload)
    terminal = coordinator.wait("job-1")

    assert terminal.http_status == 200
    result = terminal.payload["result"]
    assert result["status"] == "review"
    assert result["published"] == []
    assert final_episode.read_bytes() == source_bytes
    _assert_review_matches_source(layout, payload, result)


def test_existing_episode_moves_new_pack_to_review_without_touching_library(layout):
    payload = _payload(layout)
    source = Path(payload["source_root"]) / "Serie/Season 01/Serie.S01E01.mkv"
    existing = layout["tv"] / "Serie/Season 01/Serie.S01E01.mkv"
    existing.parent.mkdir(parents=True)
    shutil.copy2(source, existing)
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert publisher.calls == []
    assert not source.exists()
    assert existing.is_file()
    review = layout["review"] / result["review_path"]
    assert (review / "Serie/Season 01/Serie.S01E01.mkv").is_file()
    assert not list(layout["tv"].glob(".*series-worker*"))


def test_processing_failure_leaves_no_hidden_staging_in_tv(layout):
    payload = _payload(
        layout,
        videos=[
            ("Serie/Season 01/Serie.S01E01.mkv", b"one"),
            ("Serie/Season 01/Serie.S01E02.mkv", b"two"),
        ],
    )
    publisher = FakePublisher()
    coordinator = _coordinator(
        layout,
        processor=FakeProcessor(fail_on=2),
        publisher=publisher,
    )

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert publisher.calls == []
    assert not list(layout["tv"].glob(".*series-worker*"))
    _assert_review_matches_source(layout, payload, result)


def test_review_moves_pack_once_without_hidden_staging(layout, monkeypatch):
    payload = _payload(layout, videos=[("Serie/bonus.mkv", b"bonus")])
    original_move = core_module.shutil.move
    calls = []

    def recorded_move(source, destination, *args, **kwargs):
        calls.append((Path(source), Path(destination)))
        return original_move(source, destination, *args, **kwargs)

    monkeypatch.setattr(core_module.shutil, "move", recorded_move)
    coordinator = _coordinator(layout)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert len(calls) == 1
    review = layout["review"] / result["review_path"]
    assert (review / "Serie/bonus.mkv").read_bytes() == b"bonus"
    assert not Path(payload["source_root"]).exists()
    assert not list(layout["review"].glob(".*series-worker*"))


def test_review_never_overwrites_existing_job_destination(layout):
    payload = _payload(layout, videos=[("Serie/bonus.mkv", b"bonus")])
    coordinator = _coordinator(layout)
    prepared, existed, _record = coordinator._candidate(validate_payload(payload))
    assert existed is False
    destination = layout["review"] / f"job-1-{prepared.manifest.digest[:12]}"
    destination.mkdir()
    (destination / "foreign.bin").write_bytes(b"keep")

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "failed"
    assert (Path(payload["source_root"]) / "Serie/bonus.mkv").read_bytes() == b"bonus"
    assert (destination / "foreign.bin").read_bytes() == b"keep"


def test_health_checks_destination_without_running_atomic_probe(layout):
    calls = []
    coordinator = SeriesCoordinator(
        rules_store=RulesStore(config_path=layout["root"] / "health-rules.json"),
        processor_factory=lambda: FakeProcessor(),
        publisher=FakePublisher(),
        tool_checker=lambda: [],
        atomic_preflight=lambda root: calls.append(root) or {
            "supported": True,
            "st_dev": root.stat().st_dev,
        },
        lock_factory=lambda *args, **kwargs: nullcontext({"enabled": True}),
    )

    first = coordinator.health()
    second = coordinator.health()
    assert first.http_status == second.http_status == 200
    assert first.payload["checks"]["atomicity"] == {
        "ok": True,
        "mode": "direct_move",
        "verified": False,
    }
    assert second.payload["checks"]["atomicity"] == first.payload["checks"]["atomicity"]
    assert calls == []


@pytest.mark.parametrize("journal_state", ["PREPARED", "PROCESSING"])
def test_status_poll_resumes_nonterminal_job_after_restart(layout, journal_state):
    payload = _payload(layout)
    first = _coordinator(layout)
    prepared, existed, record = first._candidate(validate_payload(payload))
    assert existed is False
    first._persist_prepared(prepared, record)
    if journal_state == "PROCESSING":
        prepared.journal.transition("PROCESSING", source_count=1, pending_count=1)

    publisher = FakePublisher()
    restarted = _coordinator(layout, publisher=publisher)
    polled = restarted.status("job-1")
    terminal = restarted.wait("job-1")

    assert polled.http_status == 202
    assert polled.payload["status"] == "active"
    assert terminal.payload["result"]["status"] == "done"
    assert len(publisher.calls) == 1


def test_health_keeps_missing_contract_for_injected_operational_checker(layout):
    calls = []
    coordinator = SeriesCoordinator(
        rules_store=RulesStore(config_path=layout["root"] / "health-rules.json"),
        tool_checker=lambda: calls.append(True) or ["vobsubocr"],
        atomic_preflight=lambda root: {
            "supported": True,
            "st_dev": root.stat().st_dev,
        },
    )

    response = coordinator.health()

    assert response.http_status == 503
    assert response.payload["ok"] is False
    assert response.payload["checks"]["tools"] == {
        "ok": False,
        "missing": ["vobsubocr"],
    }
    assert response.payload["errors"] == ["tools:vobsubocr"]
    assert calls == [True]


def test_default_tool_checker_uses_bounded_parallel_smoke(layout, monkeypatch):
    calls = []

    def checker(*, names, timeout, parallel):
        calls.append((tuple(names), timeout, parallel))
        return []

    monkeypatch.setattr(core_module, "unavailable_tools", checker)
    coordinator = SeriesCoordinator(
        rules_store=RulesStore(config_path=layout["root"] / "default-health-rules.json"),
        atomic_preflight=lambda root: {
            "supported": True,
            "st_dev": root.stat().st_dev,
        },
    )

    response = coordinator.health()

    assert response.http_status == 200
    assert calls == [((*BASE_TOOLS, *OCR_TOOLS), 3, True)]


def test_unsupported_atomic_probe_is_ignored_by_fast_direct_health(layout):
    calls = []
    coordinator = SeriesCoordinator(
        rules_store=RulesStore(config_path=layout["root"] / "unsupported-health-rules.json"),
        tool_checker=lambda: [],
        atomic_preflight=lambda root: calls.append(root) or {
            "supported": False,
            "st_dev": root.stat().st_dev,
        },
    )

    first = coordinator.health()
    second = coordinator.health()

    assert first.http_status == second.http_status == 200
    assert first.payload["checks"]["atomicity"]["mode"] == "direct_move"
    assert second.payload["checks"]["atomicity"]["mode"] == "direct_move"
    assert calls == []


def test_health_leaves_no_probe_residue(layout, monkeypatch):
    monkeypatch.setenv(
        "SERIES_ATOMIC_PREFLIGHT_LOCK_PATH",
        str(layout["root"] / "locks/series-atomic-preflight.lock"),
    )
    coordinator = SeriesCoordinator(
        rules_store=RulesStore(config_path=layout["root"] / "real-health-rules.json"),
        tool_checker=lambda: [],
        atomic_preflight=delivery_module.preflight_atomic_exchange,
    )

    first = coordinator.health()
    second = coordinator.health()

    assert first.http_status == second.http_status == 200
    assert first.payload["checks"]["atomicity"]["mode"] == "direct_move"
    assert second.payload["checks"]["atomicity"]["mode"] == "direct_move"
    assert list(layout["tv"].iterdir()) == []


@pytest.mark.parametrize("entrypoint", ["status", "submit"])
@pytest.mark.parametrize("terminal_gap", ["cleanup_pending", "missing_result"])
def test_replay_retries_committed_cleanup_without_tools_or_source(
    layout,
    entrypoint,
    terminal_gap,
):
    class PendingCleanupPublisher(FakePublisher):
        def __call__(self, *args, **kwargs):
            result = super().__call__(*args, **kwargs)
            result["cleanup_pending"] = ["old-shadow"]
            return result

    payload = _payload(layout)
    first = _coordinator(layout, publisher=PendingCleanupPublisher())
    first.submit(payload)
    thread = first._threads["job-1"]
    thread.join(timeout=3)
    pending = json.loads(
        (layout["reports"] / "job-1/series_result.json").read_text("utf-8")
    )
    assert pending["delivery"]["cleanup_pending"] is True
    if terminal_gap == "missing_result":
        (layout["reports"] / "job-1/series_result.json").unlink()
    shutil.rmtree(payload["job_root"])
    recovery_calls = []

    def recover(*_args):
        recovery_calls.append(True)
        return {
            "status": "committed",
            "mode": "exchange",
            "generation": "recovered-generation",
            "recovered": True,
            "cleanup_pending": [],
        }

    restarted = _coordinator(layout)
    restarted.recoverer = recover
    tool_calls = []
    preflight_calls = []
    lock_calls = []
    restarted.tool_checker = lambda: tool_calls.append(True) or ["ffmpeg"]
    restarted.atomic_preflight = lambda _root: preflight_calls.append(True) or (_ for _ in ()).throw(
        OSError("no debe ejecutarse")
    )
    restarted.lock_factory = lambda *_args, **_kwargs: lock_calls.append(True) or (_ for _ in ()).throw(
        AssertionError("COMMITTED no debe adquirir el bloqueo audiovisual")
    )

    if entrypoint == "status":
        status = restarted.status("job-1")
        if status.http_status == 202:
            status = restarted.wait("job-1")
    else:
        accepted = restarted.submit(payload)
        assert accepted.http_status == 202
        status = restarted.wait("job-1")

    assert status.http_status == 200
    assert status.payload["result"]["delivery"]["cleanup_pending"] is False
    assert recovery_calls == [True]
    assert tool_calls == []
    assert preflight_calls == []
    assert lock_calls == []
