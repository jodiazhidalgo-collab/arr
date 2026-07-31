import errno
import multiprocessing
import os
import shutil
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from series_worker import delivery
from series_worker.journal import DurableJournal


class _SimulatedCrash(BaseException):
    """Corte abrupto portable: no debe entrar en ``except Exception``."""


def _supported_preflight(path):
    return {
        "supported": True,
        "operation": "renameat2(RENAME_EXCHANGE)",
        "st_dev": os.stat(path).st_dev,
    }


def _portable_test_exchange(left, right):
    temporary = left.parent / f".test-swap-{uuid.uuid4().hex}"
    os.rename(left, temporary)
    os.rename(right, left)
    os.rename(temporary, right)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _identity(job_id, generation, prepared, final, mode, shadow=None):
    payload = {
        "job_id": job_id,
        "generation": generation,
        "prepared_series_root": str(prepared.resolve()),
        "final_series_root": str(final.parent.resolve() / final.name),
        "mode": mode,
        "marker_name": delivery.MARKER_NAME,
    }
    if shadow is not None:
        payload["shadow_root"] = str(shadow)
    prepared_path = prepared.resolve(strict=False)
    final_path = final.parent.resolve() / final.name
    shadow_path = Path(shadow).resolve(strict=False) if shadow is not None else None
    if mode == "new":
        candidate = final_path if delivery._is_generation(
            delivery._read_marker(final_path), job_id, generation
        ) else prepared_path
        payload["candidate_signature"] = delivery._tree_signature_digest(candidate)
        payload["prepared_signature"] = delivery._tree_signature_digest(candidate)
        payload["candidate_identity"] = delivery._cleanup_root_identity(candidate)
        payload["cleanup_identities"] = {}
    elif (
        shadow_path is not None
        and shadow_path.is_dir()
        and final_path.is_dir()
        and prepared_path.is_dir()
    ):
        final_has_candidate = delivery._is_generation(
            delivery._read_marker(final_path), job_id, generation
        )
        candidate = final_path if final_has_candidate else shadow_path
        base = shadow_path if final_has_candidate else final_path
        payload["candidate_signature"] = delivery._tree_signature_digest(candidate)
        payload["base_signature"] = delivery._tree_signature_digest(base)
        payload["prepared_signature"] = delivery._tree_signature_digest(prepared_path)
        payload["candidate_identity"] = delivery._cleanup_root_identity(candidate)
        payload["cleanup_identities"] = {
            "shadow": delivery._cleanup_root_identity(base),
            "prepared": delivery._cleanup_root_identity(prepared_path),
        }
    return payload


def _journal_at_committing(journal, identity):
    journal.transition("PREPARED", **identity)
    journal.transition("PROCESSING")
    journal.transition("VERIFIED")
    journal.transition("COMMITTING")


def _journal_at_processing_new(journal, job_id, generation, prepared, final):
    identity = delivery._journal_details(
        job_id,
        generation,
        prepared.resolve(),
        final.parent.resolve() / final.name,
        "new",
        None,
    )
    journal.transition("PREPARED", **identity)
    journal.transition("PROCESSING")
    return identity


def _orphan_marker_temp(prepared, fragment="orphan"):
    return prepared / f".{delivery.MARKER_NAME}.{fragment}.tmp"


def test_new_series_is_renamed_without_overwrite(tmp_path, monkeypatch):
    library = tmp_path / "tv"
    prepared = tmp_path / "prepared" / "Serie Nueva"
    final = library / "Serie Nueva"
    library.mkdir()
    _write(prepared / "S01E01.mkv", "new")
    journal = DurableJournal(tmp_path / "journal")
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)

    result = delivery.publish_series("job-new", prepared, final, journal)

    assert result["status"] == "committed"
    assert result["mode"] == "new"
    assert not prepared.exists()
    assert (final / "S01E01.mkv").read_text(encoding="utf-8") == "new"
    assert (final / delivery.MARKER_NAME).is_file()
    assert journal.state == "COMMITTED"


def test_new_series_never_overwrites_a_destination_that_appears(tmp_path, monkeypatch):
    library = tmp_path / "tv"
    prepared = tmp_path / "prepared" / "Serie"
    final = library / "Serie"
    library.mkdir()
    _write(prepared / "new.mkv", "new")
    journal = DurableJournal(tmp_path / "journal")
    generation = "1" * 32
    journal.transition(
        "PREPARED", **_identity("job-race", generation, prepared, final, "new")
    )
    journal.transition("PROCESSING")
    _write(final / "keep.mkv", "do-not-overwrite")
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)

    with pytest.raises(delivery.DeliveryConflict):
        delivery.publish_series("job-race", prepared, final, journal)

    assert (final / "keep.mkv").read_text(encoding="utf-8") == "do-not-overwrite"
    assert (prepared / "new.mkv").read_text(encoding="utf-8") == "new"
    assert not (prepared / delivery.MARKER_NAME).exists()
    assert journal.state == "ROLLED_BACK"


def test_existing_series_uses_complete_shadow_and_never_edits_old_hardlinks(
    tmp_path, monkeypatch
):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    _write(final / "Season 01" / "S01E01.mkv", "old-1")
    _write(final / "Season 01" / "S01E02.mkv", "old-2")
    _write(prepared / "Season 01" / "S01E02.mkv", "new-2")
    _write(prepared / "Season 01" / "S01E03.mkv", "new-3")
    untouched_inode = (final / "Season 01" / "S01E01.mkv").stat().st_ino
    journal = DurableJournal(tmp_path / "journal")
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)
    monkeypatch.setattr(delivery, "_rename_exchange", _portable_test_exchange)

    result = delivery.publish_series("job-exchange", prepared, final, journal)

    assert result["status"] == "committed"
    assert result["mode"] == "exchange"
    assert (final / "Season 01" / "S01E01.mkv").read_text(encoding="utf-8") == "old-1"
    assert (final / "Season 01" / "S01E02.mkv").read_text(encoding="utf-8") == "new-2"
    assert (final / "Season 01" / "S01E03.mkv").read_text(encoding="utf-8") == "new-3"
    assert (final / "Season 01" / "S01E01.mkv").stat().st_ino == untouched_inode
    assert not prepared.exists()
    assert not list(library.glob(".Serie.series-worker.*.shadow"))
    assert journal.state == "COMMITTED"


def test_exchange_rejects_unicode_equivalent_directories_before_publish(
    tmp_path, monkeypatch
):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    _write(final / "Ｓｅａｓｏｎ ０１" / "Serie.S01E01.mkv", "old")
    _write(prepared / "Season 01" / "Serie.S01E02.mkv", "new")
    journal = DurableJournal(tmp_path / "journal")
    exchange_calls = []
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)
    monkeypatch.setattr(
        delivery,
        "_rename_exchange",
        lambda *args: exchange_calls.append(args),
    )

    with pytest.raises(delivery.DeliveryConflict, match="logicamente duplicadas"):
        delivery.publish_series("job-unicode-dirs", prepared, final, journal)

    assert (final / "Ｓｅａｓｏｎ ０１" / "Serie.S01E01.mkv").read_text("utf-8") == "old"
    assert (prepared / "Season 01" / "Serie.S01E02.mkv").read_text("utf-8") == "new"
    assert exchange_calls == []
    assert not list(library.glob(".Serie.series-worker.*.shadow"))
    assert journal.state == "ROLLED_BACK"


def test_exchange_failure_rolls_back_without_a_partial_pack(tmp_path, monkeypatch):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    _write(final / "S01E01.mkv", "old-1")
    _write(final / "S01E02.mkv", "old-2")
    _write(prepared / "S01E02.mkv", "new-2")
    _write(prepared / "S01E03.mkv", "new-3")
    journal = DurableJournal(tmp_path / "journal")
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)

    def unsupported_exchange(left, right):
        raise OSError(errno.EINVAL, "filesystem without exchange")

    monkeypatch.setattr(delivery, "_rename_exchange", unsupported_exchange)

    with pytest.raises(delivery.AtomicDeliveryUnsupported):
        delivery.publish_series("job-fail", prepared, final, journal)

    assert {path.name for path in final.iterdir()} == {"S01E01.mkv", "S01E02.mkv"}
    assert (final / "S01E01.mkv").read_text(encoding="utf-8") == "old-1"
    assert (final / "S01E02.mkv").read_text(encoding="utf-8") == "old-2"
    assert (prepared / "S01E02.mkv").read_text(encoding="utf-8") == "new-2"
    assert (prepared / "S01E03.mkv").read_text(encoding="utf-8") == "new-3"
    assert not list(library.glob(".Serie.series-worker.*.shadow"))
    assert journal.state == "ROLLED_BACK"


def test_preflight_recovers_only_empty_or_owned_orphan_probes(tmp_path, monkeypatch):
    empty = tmp_path / f"{delivery._PREFLIGHT_PREFIX}empty"
    empty.mkdir()
    owned = tmp_path / f"{delivery._PREFLIGHT_PREFIX}owned"
    owned.mkdir()
    (owned / delivery._PREFLIGHT_OWNER_FILE).write_text(
        __import__("json").dumps(delivery._PREFLIGHT_OWNER),
        encoding="utf-8",
    )
    _write(owned / "left" / "partial", "partial")
    monkeypatch.setattr(delivery, "_rename_exchange", _portable_test_exchange)

    def noreplace(source, destination):
        if destination.exists():
            raise OSError(errno.EEXIST, "exists")
        os.rename(source, destination)

    monkeypatch.setattr(delivery, "_rename_noreplace", noreplace)

    result = delivery.preflight_atomic_exchange(tmp_path)

    assert result["supported"] is True
    assert not list(tmp_path.glob(f"{delivery._PREFLIGHT_PREFIX}*"))


def test_preflight_never_deletes_an_unowned_probe(tmp_path):
    foreign = tmp_path / f"{delivery._PREFLIGHT_PREFIX}foreign"
    _write(foreign / "keep.txt", "keep")

    with pytest.raises(delivery.AtomicDeliveryUnsupported, match="no reconocido"):
        delivery.preflight_atomic_exchange(tmp_path)

    assert (foreign / "keep.txt").read_text("utf-8") == "keep"


def test_preflight_rejects_invalid_utf8_owner_without_deleting_probe(tmp_path):
    foreign = tmp_path / f"{delivery._PREFLIGHT_PREFIX}invalid-owner"
    foreign.mkdir()
    marker = foreign / delivery._PREFLIGHT_OWNER_FILE
    marker.write_bytes(b"\xff\xfe\xfa")

    with pytest.raises(delivery.AtomicDeliveryUnsupported, match="no reconocido"):
        delivery.preflight_atomic_exchange(tmp_path)

    assert marker.read_bytes() == b"\xff\xfe\xfa"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requiere procesos fork")
def test_preflight_is_serialized_across_processes(tmp_path, monkeypatch):
    lock_path = tmp_path / "locks" / "atomic-preflight.lock"
    monkeypatch.setenv("SERIES_ATOMIC_PREFLIGHT_LOCK_PATH", str(lock_path))
    context = multiprocessing.get_context("fork")
    active = context.Value("i", 0)
    maximum = context.Value("i", 0)
    results = context.Queue()
    original = delivery._preflight_atomic_exchange_locked

    def observed(root):
        with active.get_lock():
            active.value += 1
            maximum.value = max(maximum.value, active.value)
        try:
            time.sleep(0.15)
            return original(root)
        finally:
            with active.get_lock():
                active.value -= 1

    monkeypatch.setattr(delivery, "_preflight_atomic_exchange_locked", observed)

    def run_preflight():
        try:
            results.put(("ok", delivery.preflight_atomic_exchange(tmp_path)))
        except BaseException as error:
            results.put(("error", f"{type(error).__name__}:{error}"))

    processes = [context.Process(target=run_preflight) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    outcomes = [results.get(timeout=2) for _ in processes]
    assert all(kind == "ok" and payload["supported"] is True for kind, payload in outcomes)
    assert maximum.value == 1


def test_recovery_finishes_commit_when_marker_proves_exchange_happened(tmp_path):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    _write(final / "S01E01.mkv", "new")
    _write(prepared / "source.mkv", "staged")
    job_id = "job-recover"
    generation = "2" * 32
    shadow = delivery._shadow_path(final.resolve(), job_id, generation)
    _write(shadow / "S01E01.mkv", "old")
    delivery._write_marker(final, job_id, generation)
    journal = DurableJournal(tmp_path / "journal")
    _journal_at_committing(
        journal,
        _identity(job_id, generation, prepared, final, "exchange", shadow),
    )

    result = delivery.recover_delivery(job_id, prepared, final, journal)

    assert result["status"] == "committed"
    assert result["recovered"] is True
    assert journal.state == "COMMITTED"
    assert (final / "S01E01.mkv").read_text(encoding="utf-8") == "new"
    assert not shadow.exists()
    assert not prepared.exists()


def test_recovery_completes_exchange_after_crash_before_atomic_syscall(
    tmp_path, monkeypatch
):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    _write(final / "S01E01.mkv", "old")
    _write(prepared / "S01E02.mkv", "new")
    job_id = "job-before-exchange"
    generation = "a" * 32
    shadow = delivery._shadow_path(final.resolve(), job_id, generation)
    delivery._build_shadow(
        prepared,
        final,
        shadow,
        job_id=job_id,
        generation=generation,
    )
    journal = DurableJournal(tmp_path / "journal-before-exchange")
    _journal_at_committing(
        journal,
        _identity(job_id, generation, prepared, final, "exchange", shadow),
    )
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)
    monkeypatch.setattr(delivery, "_rename_exchange", _portable_test_exchange)

    result = delivery.recover_delivery(job_id, prepared, final, journal)

    assert result["status"] == "committed"
    assert result["recovered"] is True
    assert (final / "S01E01.mkv").read_text(encoding="utf-8") == "old"
    assert (final / "S01E02.mkv").read_text(encoding="utf-8") == "new"
    assert journal.state == "COMMITTED"
    assert not shadow.exists()
    assert not prepared.exists()


@pytest.mark.parametrize("mutated_side", ["base", "candidate"])
def test_recovery_refuses_base_or_candidate_drift_before_exchange(
    tmp_path,
    monkeypatch,
    mutated_side,
):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(final / "Season 01/S01E01.mkv", "old")
    _write(prepared / "Season 01/S01E02.mkv", "new")
    job_id = f"job-drift-{mutated_side}"
    generation = "d" * 32
    shadow = delivery._shadow_path(final.resolve(), job_id, generation)
    delivery._build_shadow(
        prepared,
        final,
        shadow,
        job_id=job_id,
        generation=generation,
    )
    journal = DurableJournal(tmp_path / f"journal-{mutated_side}")
    _journal_at_committing(
        journal,
        _identity(job_id, generation, prepared, final, "exchange", shadow),
    )
    target = final if mutated_side == "base" else shadow
    _write(target / "external-change.mkv", "external")
    exchange_calls = []
    monkeypatch.setattr(
        delivery,
        "_rename_exchange",
        lambda *args: exchange_calls.append(args),
    )

    with pytest.raises(delivery.RecoveryAmbiguous, match="cambió desde VERIFIED"):
        delivery.recover_delivery(job_id, prepared, final, journal)

    assert exchange_calls == []
    assert (final / "Season 01/S01E01.mkv").read_text("utf-8") == "old"
    assert (target / "external-change.mkv").read_text("utf-8") == "external"
    assert journal.state == "COMMITTING"


def test_new_series_recovery_refuses_candidate_drift(tmp_path, monkeypatch):
    library = tmp_path / "tv"
    library.mkdir()
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(prepared / "Season 01/S01E01.mkv", "new")
    job_id = "job-new-drift"
    generation = "e" * 32
    delivery._write_marker(prepared, job_id, generation)
    journal = DurableJournal(tmp_path / "journal-new-drift")
    _journal_at_committing(
        journal,
        _identity(job_id, generation, prepared, final, "new"),
    )
    _write(prepared / "external-change.mkv", "external")
    commit_calls = []
    monkeypatch.setattr(
        delivery,
        "_rename_noreplace",
        lambda *args: commit_calls.append(args),
    )

    with pytest.raises(delivery.RecoveryAmbiguous, match="cambió desde VERIFIED"):
        delivery.recover_delivery(job_id, prepared, final, journal)

    assert commit_calls == []
    assert not final.exists()
    assert (prepared / "external-change.mkv").read_text("utf-8") == "external"


def test_committed_cleanup_refuses_drifted_old_shadow(tmp_path):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(final / "new.mkv", "new")
    _write(prepared / "source.mkv", "source")
    job_id = "job-cleanup-drift"
    generation = "f" * 32
    shadow = delivery._shadow_path(final.resolve(), job_id, generation)
    _write(shadow / "old.mkv", "old")
    delivery._write_marker(final, job_id, generation)
    journal = DurableJournal(tmp_path / "journal-cleanup-drift")
    _journal_at_committing(
        journal,
        _identity(job_id, generation, prepared, final, "exchange", shadow),
    )
    journal.transition("COMMITTED")
    displaced_shadow = library / ".displaced-owned-shadow"
    os.rename(shadow, displaced_shadow)
    _write(shadow / "external-change.mkv", "external")

    with pytest.raises(
        delivery.RecoveryAmbiguous,
        match="shadow antiguo cambió de identidad",
    ):
        delivery.recover_delivery(job_id, prepared, final, journal)

    assert (shadow / "external-change.mkv").read_text("utf-8") == "external"
    assert (displaced_shadow / "old.mkv").read_text("utf-8") == "old"
    assert prepared.exists()
    assert journal.state == "COMMITTED"


def test_committed_recovery_preserves_later_final_additions_and_finishes_cleanup(
    tmp_path,
):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(final / "Season 01/S01E01.mkv", "new-1")
    _write(prepared / "source.mkv", "source")
    job_id = "job-committed-later-addition"
    generation = "9" * 32
    shadow = delivery._shadow_path(final.resolve(), job_id, generation)
    _write(shadow / "Season 01/S01E00.mkv", "old")
    delivery._write_marker(final, job_id, generation)
    journal = DurableJournal(tmp_path / "journal-later-addition")
    _journal_at_committing(
        journal,
        _identity(job_id, generation, prepared, final, "exchange", shadow),
    )
    journal.transition("COMMITTED")
    _write(final / "Season 01/S01E02.mkv", "later")

    result = delivery.recover_delivery(job_id, prepared, final, journal)

    assert result["status"] == "committed"
    assert (final / "Season 01/S01E01.mkv").read_text("utf-8") == "new-1"
    assert (final / "Season 01/S01E02.mkv").read_text("utf-8") == "later"
    assert not shadow.exists()
    assert not prepared.exists()
    assert journal.snapshot()["details"]["cleanup_complete"] is True


def test_committed_cleanup_uses_root_identity_after_hardlink_metadata_change(
    tmp_path,
):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    job_id = "job-committed-hardlink-metadata"
    generation = "7" * 32
    shadow = delivery._shadow_path(final.resolve(), job_id, generation)

    _write(shadow / "Season 01/S01E01.mkv", "old-episode")
    _write(prepared / "Season 01/S01E02.mkv", "new-episode")
    (final / "Season 01").mkdir(parents=True)
    os.link(
        shadow / "Season 01/S01E01.mkv",
        final / "Season 01/S01E01.mkv",
    )
    os.link(
        prepared / "Season 01/S01E02.mkv",
        final / "Season 01/S01E02.mkv",
    )
    delivery._write_marker(final, job_id, generation)

    journal = DurableJournal(tmp_path / "journal-hardlink-metadata")
    _journal_at_committing(
        journal,
        _identity(job_id, generation, prepared, final, "exchange", shadow),
    )
    journal.transition("COMMITTED")

    inherited = final / "Season 01/S01E01.mkv"
    before = inherited.stat().st_mtime_ns
    os.utime(inherited, ns=(before + 10_000_000, before + 10_000_000))
    changed = inherited.stat().st_mtime_ns
    assert changed != before
    assert (shadow / "Season 01/S01E01.mkv").stat().st_mtime_ns == changed

    result = delivery.recover_delivery(job_id, prepared, final, journal)

    assert result["status"] == "committed"
    assert result["cleanup_pending"] == []
    assert inherited.read_text("utf-8") == "old-episode"
    assert inherited.stat().st_mtime_ns == changed
    assert (final / "Season 01/S01E02.mkv").read_text("utf-8") == "new-episode"
    assert not shadow.exists()
    assert not prepared.exists()
    assert journal.snapshot()["details"]["cleanup_complete"] is True


def test_recovery_completes_new_series_after_crash_before_noreplace(
    tmp_path, monkeypatch
):
    library = tmp_path / "tv"
    library.mkdir()
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    _write(prepared / "S01E01.mkv", "new")
    job_id = "job-before-noreplace"
    generation = "b" * 32
    delivery._write_marker(prepared, job_id, generation)
    journal = DurableJournal(tmp_path / "journal-before-noreplace")
    _journal_at_committing(
        journal,
        _identity(job_id, generation, prepared, final, "new"),
    )
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)

    result = delivery.recover_delivery(job_id, prepared, final, journal)

    assert result["status"] == "committed"
    assert result["recovered"] is True
    assert (final / "S01E01.mkv").read_text(encoding="utf-8") == "new"
    assert journal.state == "COMMITTED"
    assert not prepared.exists()


def test_new_series_retry_converges_after_marker_is_durable_before_verified(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "tv"
    library.mkdir()
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(prepared / "Season 01/S01E01.mkv", "new")
    journal = DurableJournal(tmp_path / "journal-marker-before-verified")
    job_id = "job-marker-before-verified"
    original_write_marker = delivery._write_marker
    crashed = False

    def write_marker_then_crash(root, marker_job_id, generation):
        nonlocal crashed
        marker = original_write_marker(root, marker_job_id, generation)
        if not crashed:
            crashed = True
            raise _SimulatedCrash("corte tras marcador durable")
        return marker

    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)
    monkeypatch.setattr(delivery, "_write_marker", write_marker_then_crash)

    with pytest.raises(_SimulatedCrash):
        delivery.publish_series(job_id, prepared, final, journal)

    snapshot = journal.snapshot()
    assert snapshot["state"] == "PROCESSING"
    generation = snapshot["details"]["generation"]
    assert delivery._is_generation(
        delivery._read_marker(prepared),
        job_id,
        generation,
    )
    assert not final.exists()

    monkeypatch.setattr(delivery, "_write_marker", original_write_marker)
    result = delivery.publish_series(job_id, prepared, final, journal)

    assert result["status"] == "committed"
    assert (final / "Season 01/S01E01.mkv").read_text("utf-8") == "new"
    assert not prepared.exists()
    assert journal.snapshot()["details"]["cleanup_complete"] is True


def test_new_series_retry_removes_partial_marker_temp_with_exact_manifest(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "tv"
    library.mkdir()
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    episode = "Season 01/S01E01.mkv"
    _write(prepared / episode, "new")
    journal = DurableJournal(tmp_path / "journal-partial-marker-temp")
    job_id = "job-partial-marker-temp"
    generation = "3" * 32
    _journal_at_processing_new(journal, job_id, generation, prepared, final)
    orphan = _orphan_marker_temp(prepared, "partial")
    orphan.write_bytes(b'{"schema_version":1,"job_id":')
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)

    result = delivery.publish_series(
        job_id,
        prepared,
        final,
        journal,
        expected_files=(episode,),
    )

    assert result["status"] == "committed"
    assert (final / episode).read_text("utf-8") == "new"
    assert not orphan.exists()
    assert not prepared.exists()
    assert journal.snapshot()["details"]["cleanup_complete"] is True


def test_new_series_marker_temp_is_never_published_without_exact_manifest(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "tv"
    library.mkdir()
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    episode = "Season 01/S01E01.mkv"
    _write(prepared / episode, "new")
    journal = DurableJournal(tmp_path / "journal-temp-without-manifest")
    job_id = "job-temp-without-manifest"
    generation = "4" * 32
    _journal_at_processing_new(journal, job_id, generation, prepared, final)
    orphan = _orphan_marker_temp(prepared, "without-manifest")
    orphan.write_bytes(b"partial")
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)

    result = delivery.publish_series(
        job_id,
        prepared,
        final,
        journal,
        expected_files=None,
    )

    assert result["status"] == "committed"
    assert (final / episode).read_text("utf-8") == "new"
    assert not (final / orphan.name).exists()
    assert not prepared.exists()
    assert journal.snapshot()["details"]["cleanup_complete"] is True


@pytest.mark.parametrize("hostile_kind", ["directory", "symlink"])
def test_new_series_marker_temp_namespace_fails_closed_for_non_regular_entries(
    tmp_path,
    monkeypatch,
    hostile_kind,
):
    library = tmp_path / "tv"
    library.mkdir()
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    episode = "Season 01/S01E01.mkv"
    _write(prepared / episode, "new")
    journal = DurableJournal(tmp_path / f"journal-hostile-temp-{hostile_kind}")
    job_id = f"job-hostile-temp-{hostile_kind}"
    generation = "5" * 32
    _journal_at_processing_new(journal, job_id, generation, prepared, final)
    hostile = _orphan_marker_temp(prepared, hostile_kind)
    if hostile_kind == "directory":
        hostile.mkdir()
    else:
        target = prepared / "outside-marker-temp.txt"
        target.write_text("keep", encoding="utf-8")
        try:
            os.symlink(target, hostile)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"symlink no disponible: {error}")
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)

    with pytest.raises(delivery.RecoveryAmbiguous, match="archivo regular"):
        delivery.publish_series(
            job_id,
            prepared,
            final,
            journal,
            expected_files=(episode,),
        )

    assert not final.exists()
    assert hostile.is_dir() if hostile_kind == "directory" else hostile.is_symlink()
    assert (prepared / episode).read_text("utf-8") == "new"


def test_new_series_rolled_back_recovery_removes_owned_marker_idempotently(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "tv"
    library.mkdir()
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(prepared / "Season 01/S01E01.mkv", "new")
    journal = DurableJournal(tmp_path / "journal-rollback-marker")
    job_id = "job-rollback-marker"
    original_transition = journal.transition

    def transition_then_crash(state, **details):
        snapshot = original_transition(state, **details)
        if state == "ROLLED_BACK":
            raise _SimulatedCrash("corte tras WAL ROLLED_BACK")
        return snapshot

    def fail_before_noreplace(_prepared, _final):
        raise OSError(errno.EIO, "fallo controlado antes de NOREPLACE")

    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)
    monkeypatch.setattr(delivery, "_commit_new", fail_before_noreplace)
    monkeypatch.setattr(journal, "transition", transition_then_crash)

    with pytest.raises(_SimulatedCrash):
        delivery.publish_series(job_id, prepared, final, journal)

    snapshot = journal.snapshot()
    assert snapshot["state"] == "ROLLED_BACK"
    generation = snapshot["details"]["generation"]
    assert delivery._is_generation(
        delivery._read_marker(prepared),
        job_id,
        generation,
    )
    assert not final.exists()

    monkeypatch.setattr(journal, "transition", original_transition)
    first = delivery.recover_delivery(job_id, prepared, final, journal)
    second = delivery.recover_delivery(job_id, prepared, final, journal)

    assert first["status"] == second["status"] == "rolled_back"
    assert prepared.is_dir()
    assert not (prepared / delivery.MARKER_NAME).exists()
    assert (prepared / "Season 01/S01E01.mkv").read_text("utf-8") == "new"


def test_cleanup_complete_rejects_a_dangling_shadow_symlink(tmp_path):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(final / "Season 01/S01E01.mkv", "new")
    _write(prepared / "Season 01/S01E02.mkv", "source")
    job_id = "job-cleanup-complete-symlink"
    generation = "6" * 32
    shadow = delivery._shadow_path(final.resolve(), job_id, generation)
    _write(shadow / "Season 01/S01E00.mkv", "old")
    delivery._write_marker(final, job_id, generation)
    identity = _identity(job_id, generation, prepared, final, "exchange", shadow)
    journal = DurableJournal(tmp_path / "journal-cleanup-complete-symlink")
    _journal_at_committing(journal, identity)
    journal.transition("COMMITTED")
    journal.transition(
        "COMMITTED",
        cleanup_started=True,
        cleanup_roots=identity["cleanup_identities"],
    )
    shutil.rmtree(prepared)
    shutil.rmtree(shadow)
    journal.transition("COMMITTED", cleanup_complete=True)

    missing_target = library / ".missing-shadow-target"
    try:
        os.symlink(missing_target, shadow, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink no disponible: {error}")

    with pytest.raises(delivery.RecoveryAmbiguous):
        delivery.recover_delivery(job_id, prepared, final, journal)

    assert shadow.is_symlink()
    assert not shadow.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="SIGKILL real requiere fork Linux")
def test_real_sigkill_inside_marker_atomic_write_retries_with_exact_manifest(
    tmp_path,
    monkeypatch,
):
    library = tmp_path / "tv"
    library.mkdir()
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    episode = "Season 01/S01E01.mkv"
    _write(prepared / episode, "new")
    journal_path = tmp_path / "journal-sigkill-marker-temp"
    job_id = "job-sigkill-marker-temp"
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)

    child = os.fork()
    if child == 0:  # pragma: no cover - el padre valida temp y WAL durables
        try:
            from series_worker import journal as journal_module

            original_replace = journal_module.os.replace

            def kill_before_marker_replace(source, destination):
                target = Path(destination)
                if target.parent == prepared and target.name == delivery.MARKER_NAME:
                    os.kill(os.getpid(), signal.SIGKILL)
                return original_replace(source, destination)

            journal_module.os.replace = kill_before_marker_replace
            delivery.publish_series(
                job_id,
                prepared,
                final,
                DurableJournal(journal_path),
                expected_files=(episode,),
            )
        except BaseException:
            os._exit(70)
        os._exit(71)

    _pid, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    journal = DurableJournal(journal_path)
    assert journal.state == "PROCESSING"
    orphan_temps = list(prepared.glob(f".{delivery.MARKER_NAME}.*.tmp"))
    assert len(orphan_temps) == 1
    assert orphan_temps[0].is_file()
    assert not (prepared / delivery.MARKER_NAME).exists()

    result = delivery.publish_series(
        job_id,
        prepared,
        final,
        journal,
        expected_files=(episode,),
    )

    assert result["status"] == "committed"
    assert (final / episode).read_text("utf-8") == "new"
    assert not prepared.exists()
    assert journal.snapshot()["details"]["cleanup_complete"] is True


@pytest.mark.skipif(not hasattr(os, "fork"), reason="SIGKILL real requiere fork Linux")
@pytest.mark.parametrize(
    "crash_point",
    ["before_exchange", "after_exchange", "after_committed"],
)
def test_real_sigkill_converges_without_partial_pack(tmp_path, crash_point):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(final / "Season 01/S01E01.mkv", "old")
    _write(prepared / "Season 01/S01E02.mkv", "new")
    journal_path = tmp_path / "journal-sigkill"
    job_id = f"job-sigkill-{crash_point}"

    try:
        delivery.preflight_atomic_exchange(library)
    except delivery.AtomicDeliveryUnsupported as error:
        pytest.skip(f"renameat2 no disponible: {error}")

    child = os.fork()
    if child == 0:  # pragma: no cover - el padre valida el estado durable
        try:
            delivery.preflight_atomic_exchange = lambda root: _supported_preflight(root)
            original_exchange = delivery._rename_exchange
            original_cleanup = delivery._cleanup_committed_durable

            if crash_point == "before_exchange":
                delivery._rename_exchange = lambda _left, _right: os.kill(
                    os.getpid(), signal.SIGKILL
                )
            elif crash_point == "after_exchange":
                def exchange_then_kill(left, right):
                    original_exchange(left, right)
                    os.kill(os.getpid(), signal.SIGKILL)

                delivery._rename_exchange = exchange_then_kill
            else:
                delivery._cleanup_committed_durable = lambda *_args, **_kwargs: os.kill(
                    os.getpid(), signal.SIGKILL
                )

            delivery.publish_series(
                job_id,
                prepared,
                final,
                DurableJournal(journal_path),
            )
        except BaseException:
            os._exit(70)
        finally:
            delivery._rename_exchange = original_exchange
            delivery._cleanup_committed_durable = original_cleanup
        os._exit(71)

    _pid, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL

    result = delivery.recover_delivery(
        job_id,
        prepared,
        final,
        DurableJournal(journal_path),
    )

    assert result["status"] == "committed"
    assert result["recovered"] is True
    assert (final / "Season 01/S01E01.mkv").read_text("utf-8") == "old"
    assert (final / "Season 01/S01E02.mkv").read_text("utf-8") == "new"
    assert not prepared.exists()
    assert not list(library.glob(".Serie.series-worker.*.shadow"))
    assert DurableJournal(journal_path).state == "COMMITTED"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="SIGKILL real requiere fork Linux")
def test_real_sigkill_after_exchange_preserves_a_later_legitimate_episode(tmp_path):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(final / "Season 01/S01E01.mkv", "old")
    _write(prepared / "Season 01/S01E02.mkv", "new")
    journal_path = tmp_path / "journal-sigkill-later-episode"
    job_id = "job-sigkill-later-episode"

    try:
        delivery.preflight_atomic_exchange(library)
    except delivery.AtomicDeliveryUnsupported as error:
        pytest.skip(f"renameat2 no disponible: {error}")

    child = os.fork()
    if child == 0:  # pragma: no cover - el padre valida el estado durable
        try:
            delivery.preflight_atomic_exchange = lambda root: _supported_preflight(root)
            original_exchange = delivery._rename_exchange

            def exchange_then_kill(left, right):
                original_exchange(left, right)
                os.kill(os.getpid(), signal.SIGKILL)

            delivery._rename_exchange = exchange_then_kill
            delivery.publish_series(
                job_id,
                prepared,
                final,
                DurableJournal(journal_path),
            )
        except BaseException:
            os._exit(70)
        os._exit(71)

    _pid, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    assert DurableJournal(journal_path).state == "COMMITTING"
    assert (final / "Season 01/S01E02.mkv").read_text("utf-8") == "new"

    _write(final / "Season 01/S01E03.mkv", "later")
    result = delivery.recover_delivery(
        job_id,
        prepared,
        final,
        DurableJournal(journal_path),
    )

    assert result["status"] == "committed"
    assert result["recovered"] is True
    assert (final / "Season 01/S01E01.mkv").read_text("utf-8") == "old"
    assert (final / "Season 01/S01E02.mkv").read_text("utf-8") == "new"
    assert (final / "Season 01/S01E03.mkv").read_text("utf-8") == "later"
    assert not prepared.exists()
    assert not list(library.glob(".Serie.series-worker.*.shadow"))
    assert DurableJournal(journal_path).snapshot()["details"]["cleanup_complete"] is True


@pytest.mark.skipif(not hasattr(os, "fork"), reason="SIGKILL real requiere fork Linux")
def test_real_sigkill_during_rollback_recovers_owned_shadow(tmp_path):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(final / "old.mkv", "old")
    _write(prepared / "new.mkv", "new")
    journal_path = tmp_path / "journal-rollback-kill"
    job_id = "job-rollback-kill"

    child = os.fork()
    if child == 0:  # pragma: no cover - el padre valida el WAL
        try:
            delivery.preflight_atomic_exchange = lambda root: _supported_preflight(root)
            delivery._rename_exchange = lambda *_args: (_ for _ in ()).throw(
                OSError(errno.EINVAL, "fallo controlado")
            )
            original_remove_shadow = delivery._remove_owned_shadow
            remove_calls = 0

            def remove_shadow_then_kill_on_rollback(*args, **kwargs):
                nonlocal remove_calls
                remove_calls += 1
                if remove_calls == 1:
                    return original_remove_shadow(*args, **kwargs)
                os.kill(os.getpid(), signal.SIGKILL)

            delivery._remove_owned_shadow = remove_shadow_then_kill_on_rollback
            delivery.publish_series(
                job_id,
                prepared,
                final,
                DurableJournal(journal_path),
            )
        except BaseException:
            os._exit(70)
        os._exit(71)

    _pid, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    journal = DurableJournal(journal_path)
    assert journal.state == "ROLLED_BACK"
    assert list(library.glob(".Serie.series-worker.*.shadow"))

    result = delivery.recover_delivery(job_id, prepared, final, journal)

    assert result["status"] == "rolled_back"
    assert not list(library.glob(".Serie.series-worker.*.shadow"))
    assert (final / "old.mkv").read_text("utf-8") == "old"
    assert prepared.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="SIGKILL real requiere fork Linux")
@pytest.mark.parametrize("mutated_side", ["base", "candidate"])
def test_real_sigkill_then_drift_is_fail_closed(tmp_path, mutated_side):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(final / "old.mkv", "old")
    _write(prepared / "new.mkv", "new")
    journal_path = tmp_path / f"journal-kill-drift-{mutated_side}"
    job_id = f"job-kill-drift-{mutated_side}"

    child = os.fork()
    if child == 0:  # pragma: no cover - el padre fuerza y valida la deriva
        try:
            delivery.preflight_atomic_exchange = lambda root: _supported_preflight(root)
            delivery._rename_exchange = lambda _left, _right: os.kill(
                os.getpid(), signal.SIGKILL
            )
            delivery.publish_series(
                job_id,
                prepared,
                final,
                DurableJournal(journal_path),
            )
        except BaseException:
            os._exit(70)
        os._exit(71)

    _pid, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    shadows = list(library.glob(".Serie.series-worker.*.shadow"))
    assert len(shadows) == 1
    target = final if mutated_side == "base" else shadows[0]
    _write(target / "external-change.mkv", "external")

    with pytest.raises(delivery.RecoveryAmbiguous, match="cambió desde VERIFIED"):
        delivery.recover_delivery(
            job_id,
            prepared,
            final,
            DurableJournal(journal_path),
        )

    assert (target / "external-change.mkv").read_text("utf-8") == "external"
    assert (final / "old.mkv").read_text("utf-8") == "old"
    assert DurableJournal(journal_path).state == "COMMITTING"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="SIGKILL real requiere fork Linux")
@pytest.mark.parametrize("cleanup_target", ["shadow", "prepared"])
def test_real_sigkill_mid_cleanup_resumes_by_owned_inode(tmp_path, cleanup_target):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = library / ".Serie.prepared"
    _write(final / "Season 01/S01E01.mkv", "old")
    _write(prepared / "Season 01/S01E02.mkv", "new-2")
    _write(prepared / "Season 01/S01E03.mkv", "new-3")
    journal_path = tmp_path / f"journal-cleanup-{cleanup_target}"
    job_id = f"job-cleanup-{cleanup_target}"

    child = os.fork()
    if child == 0:  # pragma: no cover - el padre valida la reanudación
        try:
            delivery.preflight_atomic_exchange = lambda root: _supported_preflight(root)
            original_rmtree = delivery.shutil.rmtree

            def partial_rmtree(path, *args, **kwargs):
                candidate = Path(path)
                is_target = (
                    cleanup_target == "prepared" and candidate == prepared
                ) or (
                    cleanup_target == "shadow" and candidate.name.endswith(".shadow")
                )
                if is_target:
                    victim = next(item for item in candidate.rglob("*") if item.is_file())
                    victim.unlink()
                    os.kill(os.getpid(), signal.SIGKILL)
                return original_rmtree(path, *args, **kwargs)

            delivery.shutil.rmtree = partial_rmtree
            delivery.publish_series(
                job_id,
                prepared,
                final,
                DurableJournal(journal_path),
            )
        except BaseException:
            os._exit(70)
        os._exit(71)

    _pid, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    journal = DurableJournal(journal_path)
    snapshot = journal.snapshot()
    assert snapshot["state"] == "COMMITTED"
    assert snapshot["details"]["cleanup_started"] is True

    result = delivery.recover_delivery(job_id, prepared, final, journal)

    assert result["status"] == "committed"
    assert result["cleanup_pending"] == []
    assert not prepared.exists()
    assert not list(library.glob(".Serie.series-worker.*.shadow"))
    assert (final / "Season 01/S01E01.mkv").read_text("utf-8") == "old"
    assert (final / "Season 01/S01E02.mkv").read_text("utf-8") == "new-2"
    assert (final / "Season 01/S01E03.mkv").read_text("utf-8") == "new-3"
    assert DurableJournal(journal_path).snapshot()["details"]["cleanup_complete"] is True


@pytest.mark.skipif(not hasattr(os, "fork"), reason="SIGKILL real requiere fork Linux")
def test_real_sigkill_mid_preflight_cleanup_keeps_owner_until_recovery(tmp_path):
    probe = tmp_path / f"{delivery._PREFLIGHT_PREFIX}kill-cleanup"
    probe.mkdir()
    (probe / delivery._PREFLIGHT_OWNER_FILE).write_text(
        __import__("json").dumps(delivery._PREFLIGHT_OWNER),
        encoding="utf-8",
    )
    _write(probe / "left/a", "a")
    _write(probe / "left/b", "b")

    child = os.fork()
    if child == 0:  # pragma: no cover - el padre valida el marker-last
        original_rmtree = delivery.shutil.rmtree

        def partial_rmtree(path, *args, **kwargs):
            candidate = Path(path)
            victim = next(item for item in candidate.rglob("*") if item.is_file())
            victim.unlink()
            os.kill(os.getpid(), signal.SIGKILL)
            return original_rmtree(path, *args, **kwargs)

        delivery.shutil.rmtree = partial_rmtree
        delivery._cleanup_preflight_probes(tmp_path)
        os._exit(71)

    _pid, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    assert (probe / delivery._PREFLIGHT_OWNER_FILE).is_file()

    delivery._cleanup_preflight_probes(tmp_path)

    assert not probe.exists()


def test_recovery_refuses_contradictory_generation_markers(tmp_path):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    final.mkdir(parents=True)
    prepared.mkdir(parents=True)
    job_id = "job-ambiguous"
    generation = "3" * 32
    shadow = delivery._shadow_path(final.resolve(), job_id, generation)
    shadow.mkdir()
    delivery._write_marker(final, job_id, generation)
    delivery._write_marker(shadow, job_id, generation)
    journal = DurableJournal(tmp_path / "journal")
    _journal_at_committing(
        journal,
        _identity(job_id, generation, prepared, final, "exchange", shadow),
    )

    with pytest.raises(delivery.RecoveryAmbiguous, match="recovery_ambiguous"):
        delivery.recover_delivery(job_id, prepared, final, journal)

    assert final.exists()
    assert shadow.exists()
    assert journal.state == "COMMITTING"


def test_recovery_rejects_traversal_generation_before_any_atomic_syscall(
    tmp_path, monkeypatch
):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    _write(final / "old.mkv", "old")
    _write(prepared / "new.mkv", "new")
    journal = DurableJournal(tmp_path / "journal")
    _journal_at_committing(
        journal,
        _identity(
            "job-traversal",
            "../../outside",
            prepared,
            final,
            "exchange",
            tmp_path / "outside",
        ),
    )
    calls = []
    monkeypatch.setattr(delivery, "_rename_exchange", lambda *args: calls.append(args))
    monkeypatch.setattr(delivery, "_rename_noreplace", lambda *args: calls.append(args))

    with pytest.raises(delivery.RecoveryAmbiguous, match="UUID/hex seguro"):
        delivery.recover_delivery("job-traversal", prepared, final, journal)

    assert calls == []
    assert journal.state == "COMMITTING"


@pytest.mark.parametrize("location", ["external", "wrong-name"])
def test_recovery_never_trusts_a_noncanonical_shadow_root(
    tmp_path, monkeypatch, location
):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    recorded_shadow = (
        tmp_path / "external" / "attacker-shadow"
        if location == "external"
        else library / ".wrong.shadow"
    )
    _write(final / "old.mkv", "old")
    _write(prepared / "new.mkv", "new")
    _write(recorded_shadow / "keep.txt", "keep")
    generation = "4" * 32
    journal = DurableJournal(tmp_path / "journal")
    _journal_at_committing(
        journal,
        _identity(
            "job-external", generation, prepared, final, "exchange", recorded_shadow
        ),
    )
    calls = []
    monkeypatch.setattr(delivery, "_rename_exchange", lambda *args: calls.append(args))
    monkeypatch.setattr(delivery, "_rename_noreplace", lambda *args: calls.append(args))

    expected_error = "fuera del padre" if location == "external" else "no coincide"
    with pytest.raises(delivery.RecoveryAmbiguous, match=expected_error):
        delivery.recover_delivery("job-external", prepared, final, journal)

    assert calls == []
    assert (recorded_shadow / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert journal.state == "COMMITTING"


def test_publish_rejects_a_symlink_as_prepared_root(tmp_path, monkeypatch):
    library = tmp_path / "tv"
    library.mkdir()
    real_prepared = tmp_path / "real-prepared"
    _write(real_prepared / "S01E01.mkv", "episode")
    prepared_link = tmp_path / "prepared-link"
    try:
        prepared_link.symlink_to(real_prepared, target_is_directory=True)
    except OSError:
        path_type = type(prepared_link)
        real_is_symlink = path_type.is_symlink

        def simulated_is_symlink(path):
            return path == prepared_link or real_is_symlink(path)

        monkeypatch.setattr(path_type, "is_symlink", simulated_is_symlink)
    journal = DurableJournal(tmp_path / "journal")

    with pytest.raises(delivery.DeliveryError, match="enlace simbolico"):
        delivery.publish_series("job-symlink", prepared_link, library / "Serie", journal)

    assert journal.snapshot() is None


def test_recovery_keeps_committing_when_durability_retry_fails(
    tmp_path, monkeypatch
):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    _write(final / "new.mkv", "new")
    _write(prepared / "source.mkv", "staged")
    generation = "5" * 32
    shadow = delivery._shadow_path(final.resolve(), "job-fsync", generation)
    _write(shadow / "old.mkv", "old")
    delivery._write_marker(final, "job-fsync", generation)
    journal = DurableJournal(tmp_path / "journal")
    _journal_at_committing(
        journal,
        _identity("job-fsync", generation, prepared, final, "exchange", shadow),
    )
    real_fsync_tree = delivery._fsync_tree
    monkeypatch.setattr(
        delivery,
        "_fsync_tree",
        lambda _path: (_ for _ in ()).throw(OSError(errno.EIO, "fsync failed")),
    )

    with pytest.raises(OSError, match="fsync failed"):
        delivery.recover_delivery("job-fsync", prepared, final, journal)

    assert journal.state == "COMMITTING"
    assert shadow.exists()
    assert prepared.exists()

    monkeypatch.setattr(delivery, "_fsync_tree", real_fsync_tree)
    result = delivery.recover_delivery("job-fsync", prepared, final, journal)

    assert result["status"] == "committed"
    assert result["recovered"] is True
    assert journal.state == "COMMITTED"
    assert not shadow.exists()
    assert not prepared.exists()


def test_publish_rejects_an_empty_prepared_tree(tmp_path, monkeypatch):
    library = tmp_path / "tv"
    prepared = tmp_path / "prepared" / "Serie"
    library.mkdir()
    prepared.mkdir(parents=True)
    journal = DurableJournal(tmp_path / "journal")
    monkeypatch.setattr(
        delivery,
        "preflight_atomic_exchange",
        lambda _path: pytest.fail("preflight no debe ejecutarse"),
    )

    with pytest.raises(delivery.DeliveryError, match="ningun archivo publicable"):
        delivery.publish_series("job-empty", prepared, library / "Serie", journal)

    assert journal.snapshot() is None


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (("S01E01.mkv",), ("S01E01.mkv", "S01E02.mkv")),
        (("S01E01.mkv", "extra.txt"), ("S01E01.mkv",)),
    ],
)
def test_publish_requires_the_exact_expected_file_set(
    tmp_path, monkeypatch, actual, expected
):
    library = tmp_path / "tv"
    prepared = tmp_path / "prepared" / "Serie"
    library.mkdir()
    for relative in actual:
        _write(prepared / relative, relative)
    journal = DurableJournal(tmp_path / "journal")
    monkeypatch.setattr(
        delivery,
        "preflight_atomic_exchange",
        lambda _path: pytest.fail("preflight no debe ejecutarse"),
    )

    with pytest.raises(delivery.DeliveryError, match="no coincide con expected_files"):
        delivery.publish_series(
            "job-incomplete",
            prepared,
            library / "Serie",
            journal,
            expected_files=expected,
        )

    assert journal.snapshot() is None


def test_series_delivery_lock_serializes_two_publishers(tmp_path, monkeypatch):
    library = tmp_path / "tv"
    final = library / "Serie"
    first = tmp_path / "prepared-one" / "Serie"
    second = tmp_path / "prepared-two" / "Serie"
    _write(final / "old.mkv", "old")
    _write(first / "first.mkv", "first")
    _write(second / "second.mkv", "second")
    first_journal = DurableJournal(tmp_path / "journal-one")
    second_journal = DurableJournal(tmp_path / "journal-two")
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)
    monkeypatch.setattr(delivery, "_rename_exchange", _portable_test_exchange)
    real_build_shadow = delivery._build_shadow
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    guard = threading.Lock()
    active = 0
    maximum_active = 0
    calls = 0

    def controlled_build(*args, **kwargs):
        nonlocal active, maximum_active, calls
        with guard:
            calls += 1
            call_number = calls
            active += 1
            maximum_active = max(maximum_active, active)
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(timeout=3)
        else:
            second_entered.set()
        try:
            return real_build_shadow(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(delivery, "_build_shadow", controlled_build)
    results = []
    errors = []

    def run(job_id, prepared, journal, expected):
        try:
            results.append(
                delivery.publish_series(
                    job_id,
                    prepared,
                    final,
                    journal,
                    expected_files=(expected,),
                )
            )
        except Exception as exc:  # pragma: no cover - se muestra en el assert
            errors.append(exc)

    thread_one = threading.Thread(
        target=run, args=("job-one", first, first_journal, "first.mkv")
    )
    thread_two = threading.Thread(
        target=run, args=("job-two", second, second_journal, "second.mkv")
    )
    thread_one.start()
    assert first_entered.wait(timeout=2)
    thread_two.start()
    try:
        assert not second_entered.wait(timeout=0.2)
    finally:
        release_first.set()
    thread_one.join(timeout=5)
    thread_two.join(timeout=5)

    assert not thread_one.is_alive()
    assert not thread_two.is_alive()
    assert errors == []
    assert len(results) == 2
    assert calls == 2
    assert maximum_active == 1
    assert (final / "first.mkv").read_text(encoding="utf-8") == "first"
    assert (final / "second.mkv").read_text(encoding="utf-8") == "second"


def test_final_drift_is_rechecked_immediately_before_exchange(tmp_path, monkeypatch):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    _write(final / "old.mkv", "old")
    _write(prepared / "new.mkv", "new")
    journal = DurableJournal(tmp_path / "journal")
    monkeypatch.setattr(delivery, "preflight_atomic_exchange", _supported_preflight)
    real_build_shadow = delivery._build_shadow

    def build_then_drift(*args, **kwargs):
        signatures = real_build_shadow(*args, **kwargs)
        _write(final / "intruder.mkv", "drift")
        return signatures

    exchange_calls = []
    monkeypatch.setattr(delivery, "_build_shadow", build_then_drift)
    monkeypatch.setattr(
        delivery, "_rename_exchange", lambda *args: exchange_calls.append(args)
    )

    with pytest.raises(delivery.DeliveryConflict, match="cambio antes de EXCHANGE"):
        delivery.publish_series(
            "job-drift",
            prepared,
            final,
            journal,
            expected_files=("new.mkv",),
        )

    assert exchange_calls == []
    assert journal.state == "ROLLED_BACK"
    assert (final / "old.mkv").read_text(encoding="utf-8") == "old"
    assert (final / "intruder.mkv").read_text(encoding="utf-8") == "drift"
    assert not list(library.glob(".Serie.series-worker.*.shadow"))


def test_preflight_checks_both_operations_and_always_cleans(tmp_path, monkeypatch):
    calls = {"exchange": 0, "noreplace": 0}
    real_noreplace = delivery._rename_noreplace

    def exchange(left, right):
        calls["exchange"] += 1
        _portable_test_exchange(left, right)

    def noreplace(source, destination):
        calls["noreplace"] += 1
        real_noreplace(source, destination)

    monkeypatch.setattr(delivery, "_rename_exchange", exchange)
    monkeypatch.setattr(delivery, "_rename_noreplace", noreplace)

    result = delivery.preflight_atomic_exchange(tmp_path)

    assert result["operations"] == [
        "renameat2(RENAME_EXCHANGE)",
        "renameat2(RENAME_NOREPLACE)",
    ]
    assert calls == {"exchange": 1, "noreplace": 2}
    assert not list(tmp_path.glob(".series-worker-exchange-probe-*"))


def test_preflight_cleans_when_setup_fsync_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        delivery,
        "_fsync_tree",
        lambda _path: (_ for _ in ()).throw(OSError(errno.EIO, "probe fsync failed")),
    )

    with pytest.raises(OSError, match="probe fsync failed"):
        delivery.preflight_atomic_exchange(tmp_path)

    assert not list(tmp_path.glob(".series-worker-exchange-probe-*"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="renameat2 es Linux")
def test_linux_preflight_performs_a_real_exchange(tmp_path):
    result = delivery.preflight_atomic_exchange(tmp_path)

    assert result["supported"] is True
    assert result["operation"] == "renameat2(RENAME_EXCHANGE)"
    assert not list(tmp_path.glob(".series-worker-exchange-probe-*"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="renameat2 es Linux")
def test_linux_observer_sees_only_old_or_new_complete_root(tmp_path):
    library = tmp_path / "tv"
    final = library / "Serie"
    prepared = tmp_path / "prepared" / "Serie"
    _write(final / "S01E01.mkv", "old-1")
    _write(final / "S01E02.mkv", "old-2")
    _write(prepared / "S01E02.mkv", "new-2")
    _write(prepared / "S01E03.mkv", "new-3")
    old_inode = (final / "S01E01.mkv").stat().st_ino
    journal = DurableJournal(tmp_path / "journal")
    observations = []
    ready = threading.Event()
    stop = threading.Event()

    def observe():
        ready.set()
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        while not stop.is_set():
            descriptor = os.open(final, os.O_RDONLY | directory_flag)
            try:
                observations.append(frozenset(os.listdir(descriptor)))
            finally:
                os.close(descriptor)

    observer = threading.Thread(target=observe)
    observer.start()
    assert ready.wait(timeout=1)
    try:
        delivery.publish_series("job-linux", prepared, final, journal)
        observations.append(frozenset(os.listdir(final)))
    finally:
        stop.set()
        observer.join(timeout=2)

    old_pack = frozenset({"S01E01.mkv", "S01E02.mkv"})
    new_pack = frozenset(
        {"S01E01.mkv", "S01E02.mkv", "S01E03.mkv", delivery.MARKER_NAME}
    )
    assert observations
    assert set(observations) <= {old_pack, new_pack}
    assert new_pack in observations
    assert (final / "S01E01.mkv").stat().st_ino == old_inode
    assert (final / "S01E02.mkv").read_text(encoding="utf-8") == "new-2"
