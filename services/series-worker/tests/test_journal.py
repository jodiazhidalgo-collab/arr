import json
import os

import pytest

from series_worker import journal


def test_journal_persists_all_states_in_json_and_jsonl(tmp_path):
    durable = journal.DurableJournal(tmp_path / "job-1")

    durable.transition("PREPARED", job_id="job-1", generation="gen-1")
    durable.transition("PROCESSING", source_count=3)
    durable.transition("VERIFIED", verified_count=3)
    durable.transition("COMMITTING", mode="exchange")
    final = durable.transition("COMMITTED")

    assert durable.state == "COMMITTED"
    assert final["sequence"] == 5
    snapshot = json.loads(durable.snapshot_path.read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in durable.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert snapshot == final
    assert events[-1] == snapshot
    assert [event["state"] for event in events] == [
        "PREPARED",
        "PROCESSING",
        "VERIFIED",
        "COMMITTING",
        "COMMITTED",
    ]


def test_journal_rejects_impossible_transition_and_identity_change(tmp_path):
    durable = journal.DurableJournal(tmp_path / "job-2")
    durable.transition("PREPARED", job_id="job-2", generation="one")

    with pytest.raises(journal.JournalContradiction):
        durable.transition("VERIFIED")
    with pytest.raises(journal.JournalContradiction):
        durable.transition("PREPARED", generation="two")

    assert durable.state == "PREPARED"
    assert len(durable.history()) == 1


def test_snapshot_repairs_a_crash_between_jsonl_and_json(tmp_path):
    durable = journal.DurableJournal(tmp_path / "job-3")
    first = durable.transition("PREPARED", job_id="job-3")
    latest = durable.transition("PROCESSING")
    durable.snapshot_path.write_text(json.dumps(first), encoding="utf-8")

    assert durable.snapshot() == latest
    assert json.loads(durable.snapshot_path.read_text(encoding="utf-8")) == latest


def test_truncated_final_jsonl_append_is_discarded_and_recovered(tmp_path):
    durable = journal.DurableJournal(tmp_path / "job-4")
    expected = durable.transition("PREPARED", job_id="job-4")
    with durable.events_path.open("ab") as handle:
        handle.write(b'{"sequence":2')

    assert durable.snapshot() == expected
    assert durable.events_path.read_bytes().endswith(b"\n")
    assert b'"sequence":2' not in durable.events_path.read_bytes()


def test_complete_corrupt_jsonl_line_is_a_contradiction(tmp_path):
    durable = journal.DurableJournal(tmp_path / "job-corrupt")
    durable.transition("PREPARED", job_id="job-corrupt")
    with durable.events_path.open("ab") as handle:
        handle.write(b'{"sequence":2 invalid}\n')

    with pytest.raises(journal.JournalContradiction):
        durable.snapshot()


def test_valid_json_without_final_newline_is_still_discarded_as_torn_tail(tmp_path):
    durable = journal.DurableJournal(tmp_path / "job-valid-tail")
    expected = durable.transition("PREPARED", job_id="job-valid-tail")
    with durable.events_path.open("ab") as handle:
        handle.write(b'{"details":{}}')

    assert durable.snapshot() == expected
    assert durable.events_path.read_bytes().endswith(b"\n")
    assert b'{"details":{}}' not in durable.events_path.read_bytes()


def test_write_json_atomic_uses_temporary_replace_and_safe_relative_name(
    tmp_path, monkeypatch
):
    durable = journal.DurableJournal(tmp_path / "job-5")
    replacements = []
    synced_directories = []
    real_replace = journal.os.replace
    real_sync_directory = journal.fsync_directory

    def tracked_replace(source, destination):
        replacements.append((source, destination))
        return real_replace(source, destination)

    def tracked_sync_directory(path):
        synced_directories.append(journal.Path(path))
        return real_sync_directory(path)

    monkeypatch.setattr(journal.os, "replace", tracked_replace)
    monkeypatch.setattr(journal, "fsync_directory", tracked_sync_directory)

    destination = durable.write_json_atomic("result.json", {"ok": True})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"ok": True}
    assert replacements and journal.Path(replacements[-1][0]) != destination
    assert destination.parent in synced_directories
    with pytest.raises(ValueError):
        durable.write_json_atomic("../outside.json", {})


def test_dangling_job_directory_symlink_is_rejected(tmp_path):
    target = tmp_path / "missing-outside"
    job_dir = tmp_path / "job-link"
    try:
        os.symlink(target, job_dir, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink no disponible: {error}")
    durable = journal.DurableJournal(job_dir)

    with pytest.raises(journal.JournalError, match="enlace simbólico"):
        durable.snapshot()
    with pytest.raises(journal.JournalError, match="enlace simbólico"):
        durable.transition("PREPARED", job_id="job-link")

    assert not target.exists()


def test_symlinked_jsonl_is_never_read_or_truncated(tmp_path):
    job_dir = tmp_path / "job-jsonl-link"
    job_dir.mkdir()
    outside = tmp_path / "outside.jsonl"
    original = b'{"external":"keep"}'
    outside.write_bytes(original)
    events = job_dir / "journal.jsonl"
    try:
        os.symlink(outside, events)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink no disponible: {error}")
    durable = journal.DurableJournal(job_dir)

    with pytest.raises(journal.JournalError, match="enlace simbólico"):
        durable.snapshot()

    assert outside.read_bytes() == original
