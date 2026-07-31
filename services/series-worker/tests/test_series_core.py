import errno
import json
import shutil
import threading
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path, PurePosixPath

import pytest
import series_worker.core as core_module

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
            output = Path(job_root) / "series_work" / "processed" / Path(
                *PurePosixPath(entry.target_relpath).parts
            )
            output = output.with_suffix(".mkv")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(f"processed-{entry.source_relpath}".encode())
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


class FakePublisher:
    def __init__(self):
        self.calls = []

    def __call__(self, job_id, prepared, final, journal, *, expected_files):
        prepared = Path(prepared)
        final = Path(final)
        expected_files = tuple(expected_files)
        self.calls.append((job_id, prepared, final, expected_files))
        actual = sorted(
            path.relative_to(prepared).as_posix()
            for path in prepared.rglob("*")
            if path.is_file()
        )
        assert actual == sorted(expected_files)
        journal.transition("VERIFIED", preflight={"supported": True})
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
        "PREPARED", "PROCESSING", "VERIFIED", "COMMITTING", "COMMITTED"
    ]


def test_prepared_publication_staging_stays_on_the_tv_atomic_mount(layout):
    payload = _payload(layout)
    coordinator = _coordinator(layout)

    prepared, existed, _record = coordinator._candidate(validate_payload(payload))

    assert existed is False
    assert prepared.prepared_series_root is not None
    assert prepared.prepared_series_root.parent == layout["tv"]
    assert prepared.prepared_series_root.name.startswith(".Serie.series-worker.")


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
    assert entered.wait(timeout=1)
    changed = {
        **payload,
        "callback_url": "http://arr-orchestrator:8787/jobs/job-1/events",
    }

    with pytest.raises(JobConflict):
        coordinator.submit(changed)
    release.set()


def test_external_heavy_lock_busy_is_409_without_persisting_job(layout):
    payload = _payload(layout)

    class BusyContext:
        def __enter__(self):
            raise HeavyLockTimeout("busy")

        def __exit__(self, *args):
            return None

    coordinator = _coordinator(layout, lock_factory=lambda *a, **k: BusyContext())
    with pytest.raises(SeriesWorkerBusy):
        coordinator.submit(payload)
    assert not (layout["reports"] / "job-1/request.json").exists()


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


def test_second_episode_failure_keeps_tv_unpublished(layout):
    payload = _payload(
        layout,
        videos=[
            ("Serie/Season 01/Serie.S01E01.mkv", b"one"),
            ("Serie/Season 01/Serie.S01E02.mkv", b"two"),
        ],
    )
    publisher = FakePublisher()
    coordinator = _coordinator(
        layout, processor=FakeProcessor(fail_on=2), publisher=publisher
    )

    coordinator.submit(payload)
    terminal = coordinator.wait("job-1")

    assert terminal.payload["result"]["status"] == "failed"
    assert terminal.payload["result"]["published"] == []
    assert publisher.calls == []
    assert not (layout["tv"] / "Serie").exists()
    assert (Path(payload["source_root"]) / "Serie/Season 01/Serie.S01E02.mkv").exists()


def test_enospc_on_second_episode_never_publishes_and_preserves_complete_source(
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
    original_write_bytes = Path.write_bytes

    def fail_second_processed_write(path, data):
        if "series_work" in path.parts and path.name == "Serie.S01E02.mkv":
            raise OSError(errno.ENOSPC, "controlled no space left on device")
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_second_processed_write)
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    terminal = coordinator.wait("job-1")
    result = terminal.payload["result"]

    assert result["status"] == "failed"
    assert result["error_code"] == "OSError"
    assert "no space left on device" in result["error"]
    assert str(source_root) not in result["error"]
    assert result["published"] == []
    assert result["review_path"] == ""
    assert publisher.calls == []
    assert not (layout["tv"] / "Serie").exists()
    assert not list(layout["tv"].glob(".*.series-worker.*.prepared"))
    assert list(layout["review"].iterdir()) == []
    assert (
        source_root / "Serie/Season 01/Serie.S01E01.mkv"
    ).read_bytes() == b"source-one"
    assert (
        source_root / "Serie/Season 01/Serie.S01E02.mkv"
    ).read_bytes() == b"source-two"
    snapshot = json.loads(
        (layout["reports"] / "job-1/journal.json").read_text("utf-8")
    )
    assert snapshot["state"] == "ROLLED_BACK"
    assert snapshot["details"]["terminal_status"] == "failed"
    persisted = json.loads(
        (layout["reports"] / "job-1/series_result.json").read_text("utf-8")
    )
    assert persisted == result


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


def test_identical_collision_is_satisfied_without_processing(layout):
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

    assert result["status"] == "done"
    assert result["satisfied"] == ["Serie/Season 01/Serie.S01E01.mkv"]
    assert publisher.calls[0][3] == ("Season 01/Serie.S01E01.mkv",)


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
    prepared.prepared_series_root.mkdir(parents=True)
    (prepared.prepared_series_root / "partial.mkv").write_bytes(b"partial")
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
    assert not prepared.prepared_series_root.exists()


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


def test_missing_tools_and_atomicity_fail_with_503(layout):
    payload = _payload(layout)
    store = RulesStore(config_path=layout["root"] / "rules.json")
    missing = SeriesCoordinator(
        rules_store=store,
        atomic_preflight=lambda root: {"supported": True},
        tool_checker=lambda: ["mkvmerge"],
    )
    with pytest.raises(ServiceUnavailable, match="mkvmerge"):
        missing.submit(payload)

    atomic = SeriesCoordinator(
        rules_store=store,
        atomic_preflight=lambda root: (_ for _ in ()).throw(OSError("unsupported")),
        tool_checker=lambda: [],
    )
    with pytest.raises(ServiceUnavailable, match="atómica"):
        atomic.submit(payload)


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

    with pytest.raises(ServiceUnavailable, match="durable"):
        _coordinator(layout).submit(payload)


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


def test_identical_video_with_new_sidecar_is_processed_and_published(layout):
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

    class SidecarProcessor(FakeProcessor):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def process(self, **kwargs):
            self.calls += 1
            result = super().process(**kwargs)
            for episode in result.episodes:
                output = Path(kwargs["job_root"]) / episode.provisional_relpath
                output.with_name(f"{output.stem}.es.forced.srt").write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nHola\n",
                    encoding="utf-8",
                )
            return result

    processor = SidecarProcessor()
    coordinator = _coordinator(layout, processor=processor)
    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "done"
    assert processor.calls == 1
    assert (layout["tv"] / "Serie/Season 01/Serie.S01E01.es.forced.srt").is_file()


def test_satisfied_episode_hardlink_failure_reviews_without_copy_fallback(
    layout,
    monkeypatch,
):
    payload = _payload(layout)
    source = Path(payload["source_root"]) / "Serie/Season 01/Serie.S01E01.mkv"
    existing = layout["tv"] / "Serie/Season 01/Serie.S01E01.mkv"
    existing.parent.mkdir(parents=True)
    shutil.copy2(source, existing)
    publisher = FakePublisher()
    monkeypatch.setattr(core_module.os, "link", lambda *_a, **_k: (_ for _ in ()).throw(OSError("no link")))
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert publisher.calls == []
    assert source.is_file() and existing.is_file()
    assert not list(layout["tv"].glob(".*.series-worker.*.prepared"))


def test_partial_prepared_copy_is_removed_before_terminal_failure(layout, monkeypatch):
    payload = _payload(
        layout,
        videos=[
            ("Serie/Season 01/Serie.S01E01.mkv", b"one"),
            ("Serie/Season 01/Serie.S01E02.mkv", b"two"),
        ],
    )
    original_copy2 = core_module.shutil.copy2
    calls = []

    def fail_second_copy(source, destination, *args, **kwargs):
        calls.append((source, destination))
        if len(calls) == 2:
            raise OSError("fallo controlado en la segunda copia")
        return original_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(core_module.shutil, "copy2", fail_second_copy)
    publisher = FakePublisher()
    coordinator = _coordinator(layout, publisher=publisher)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "failed"
    assert publisher.calls == []
    assert not list(layout["tv"].glob(".*.series-worker.*.prepared"))


@pytest.mark.parametrize("owned_partial", [False, True])
def test_review_retry_cleans_only_its_identified_staging(layout, owned_partial):
    payload = _payload(layout, videos=[("Serie/bonus.mkv", b"bonus")])
    coordinator = _coordinator(layout)
    prepared, existed, _record = coordinator._candidate(validate_payload(payload))
    assert existed is False
    reasons = tuple(prepared.manifest.review_reasons)
    destination_name = f"job-1-{prepared.manifest.digest[:12]}"
    temporary = layout["review"] / f".{destination_name}.series-worker.tmp"
    temporary.mkdir()
    if owned_partial:
        (temporary / "reason.json").write_text(
            json.dumps(
                {
                    "job_id": "job-1",
                    "manifest_digest": prepared.manifest.digest,
                    "reasons": list(reasons),
                }
            ),
            encoding="utf-8",
        )
        (temporary / "partial.mkv").write_bytes(b"partial")

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "review"
    assert not temporary.exists()
    assert (layout["review"] / result["review_path"] / "reason.json").is_file()


def test_review_retry_never_deletes_foreign_staging(layout):
    payload = _payload(layout, videos=[("Serie/bonus.mkv", b"bonus")])
    coordinator = _coordinator(layout)
    prepared, existed, _record = coordinator._candidate(validate_payload(payload))
    assert existed is False
    destination_name = f"job-1-{prepared.manifest.digest[:12]}"
    temporary = layout["review"] / f".{destination_name}.series-worker.tmp"
    temporary.mkdir()
    (temporary / "reason.json").write_text(
        json.dumps({"job_id": "otro-job"}),
        encoding="utf-8",
    )
    (temporary / "foreign.bin").write_bytes(b"keep")

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "failed"
    assert (temporary / "foreign.bin").read_bytes() == b"keep"


def test_review_copy_must_match_source_before_becoming_review(
    layout,
    monkeypatch,
):
    payload = _payload(
        layout,
        videos=[("Serie/bonus.mkv", b"bonus")],
        sidecars=[("LEEME.nfo", "original")],
    )
    original_copytree = core_module.shutil.copytree

    def corrupt_copy(source, destination, *args, **kwargs):
        result = original_copytree(source, destination, *args, **kwargs)
        (Path(destination) / "LEEME.nfo").write_text("corrupto", encoding="utf-8")
        return result

    monkeypatch.setattr(core_module.shutil, "copytree", corrupt_copy)
    coordinator = _coordinator(layout)

    coordinator.submit(payload)
    result = coordinator.wait("job-1").payload["result"]

    assert result["status"] == "failed"
    assert (Path(payload["source_root"]) / "LEEME.nfo").read_text("utf-8") == "original"
    assert not any(layout["review"].iterdir())


def test_health_is_read_only_until_submit_caches_atomic_preflight(layout):
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
    accepted = coordinator.submit(_payload(layout))
    terminal = coordinator.wait("job-1")
    after_submit = coordinator.health()

    assert first.http_status == second.http_status == 200
    assert first.payload["checks"]["atomicity"]["verified"] is False
    assert second.payload["checks"]["atomicity"]["verified"] is False
    assert accepted.http_status == 202
    assert terminal.payload["result"]["status"] == "done"
    assert len(calls) == 1
    assert after_submit.payload["checks"]["atomicity"]["verified"] is True


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
        atomic_preflight=lambda root: {"supported": True},
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
        rules_store=RulesStore(config_path=layout["root"] / "default-health-rules.json")
    )

    response = coordinator.health()

    assert response.http_status == 200
    assert calls == [((*BASE_TOOLS, *OCR_TOOLS), 3, True)]


def test_failed_or_stale_preflight_invalidates_health_cache(layout):
    outcomes = [
        {"supported": True, "st_dev": layout["tv"].stat().st_dev},
        OSError(errno.EIO, "controlled preflight failure"),
    ]

    def preflight(_root):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    coordinator = SeriesCoordinator(
        rules_store=RulesStore(config_path=layout["root"] / "health-cache-rules.json"),
        processor_factory=lambda: FakeProcessor(),
        publisher=FakePublisher(),
        tool_checker=lambda: [],
        atomic_preflight=preflight,
        lock_factory=lambda *args, **kwargs: nullcontext({"enabled": True}),
    )
    coordinator.submit(_payload(layout, job_id="job-cache-1"))
    coordinator.wait("job-cache-1")
    assert coordinator.health().payload["checks"]["atomicity"]["verified"] is True

    with pytest.raises(ServiceUnavailable, match="atómica"):
        coordinator.submit(
            _payload(
                layout,
                job_id="job-cache-2",
                videos=[("Otra/Season 01/Otra.S01E01.mkv", b"two")],
            )
        )
    assert coordinator.health().payload["checks"]["atomicity"]["verified"] is False

    coordinator._health_atomicity_cache = {
        "supported": True,
        "st_dev": layout["tv"].stat().st_dev + 1,
    }
    stale = coordinator.health()
    assert stale.payload["checks"]["atomicity"]["verified"] is False
    assert coordinator._health_atomicity_cache is None


def test_unexpected_preflight_failure_invalidates_health_cache(layout):
    outcomes = [
        {"supported": True, "st_dev": layout["tv"].stat().st_dev},
        ValueError("controlled unexpected preflight failure"),
    ]

    def preflight(_root):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    coordinator = SeriesCoordinator(
        rules_store=RulesStore(config_path=layout["root"] / "unexpected-cache-rules.json"),
        processor_factory=lambda: FakeProcessor(),
        publisher=FakePublisher(),
        tool_checker=lambda: [],
        atomic_preflight=preflight,
        lock_factory=lambda *args, **kwargs: nullcontext({"enabled": True}),
    )
    coordinator.submit(_payload(layout, job_id="job-unexpected-cache-1"))
    coordinator.wait("job-unexpected-cache-1")
    assert coordinator.health().payload["checks"]["atomicity"]["verified"] is True

    with pytest.raises(ServiceUnavailable, match="atómica"):
        coordinator.submit(
            _payload(
                layout,
                job_id="job-unexpected-cache-2",
                videos=[("Otra/Season 01/Otra.S01E01.mkv", b"two")],
            )
        )

    assert coordinator.health().payload["checks"]["atomicity"]["verified"] is False


@pytest.mark.parametrize("entrypoint", ["status", "submit"])
def test_replay_retries_committed_cleanup_without_tools_or_source(layout, entrypoint):
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
    restarted.tool_checker = lambda: tool_calls.append(True) or ["ffmpeg"]
    restarted.atomic_preflight = lambda _root: preflight_calls.append(True) or (_ for _ in ()).throw(
        OSError("no debe ejecutarse")
    )

    if entrypoint == "status":
        status = restarted.status("job-1")
    else:
        accepted = restarted.submit(payload)
        assert accepted.http_status == 202
        status = restarted.wait("job-1")

    assert status.http_status == 200
    assert status.payload["result"]["delivery"]["cleanup_pending"] is False
    assert recovery_calls == [True]
    assert tool_calls == []
    assert preflight_calls == []
