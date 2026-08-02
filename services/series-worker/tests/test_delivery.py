import errno
from pathlib import Path

import pytest
import series_worker.delivery as delivery

from series_worker.delivery import (
    DeliveryConflict,
    DeliveryError,
    MARKER_NAME,
    preflight_atomic_exchange,
    publish_series,
    recover_delivery,
)
from series_worker.journal import DurableJournal


def _write(path: Path, content: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _journal(path: Path, job_id: str = "job-1") -> DurableJournal:
    journal = DurableJournal(path)
    journal.transition(
        "PREPARED",
        job_id=job_id,
        generation="1" * 32,
        mode="direct_move",
    )
    return journal


def test_new_episode_moves_directly_without_hidden_library_artifacts(tmp_path):
    prepared = tmp_path / "taller/job-1/series_work/processed/Serie"
    final = tmp_path / "tv/Serie"
    final.parent.mkdir(parents=True)
    source = _write(prepared / "Season 01/Serie.S01E01.mkv")
    source_inode = source.stat().st_ino

    result = publish_series(
        "job-1",
        prepared,
        final,
        _journal(tmp_path / "journal"),
        expected_files=["Season 01/Serie.S01E01.mkv"],
    )

    destination = final / "Season 01/Serie.S01E01.mkv"
    assert result["status"] == "committed"
    assert result["mode"] == "direct_move"
    assert destination.is_file()
    assert destination.stat().st_ino == source_inode
    assert not source.exists()
    assert not list((tmp_path / "tv").glob(".*series-worker*"))
    assert not list((tmp_path / "tv").rglob(MARKER_NAME))


def test_existing_series_receives_only_new_episode_and_keeps_old_one(tmp_path):
    prepared = tmp_path / "taller/job-1/series_work/processed/Serie"
    final = tmp_path / "tv/Serie"
    final.parent.mkdir(parents=True)
    old = _write(final / "Season 01/Serie.S01E01.mkv", b"old")
    new = _write(prepared / "Season 01/Serie.S01E02.mkv", b"new")
    old_inode = old.stat().st_ino
    new_inode = new.stat().st_ino

    publish_series(
        "job-1",
        prepared,
        final,
        _journal(tmp_path / "journal"),
        expected_files=["Season 01/Serie.S01E02.mkv"],
    )

    assert old.read_bytes() == b"old"
    assert old.stat().st_ino == old_inode
    assert (final / "Season 01/Serie.S01E02.mkv").stat().st_ino == new_inode


def test_video_and_srt_move_together_from_workshop(tmp_path):
    prepared = tmp_path / "taller/job-1/series_work/processed/Serie"
    final = tmp_path / "tv/Serie"
    final.parent.mkdir(parents=True)
    _write(prepared / "Season 01/Serie.S01E01.mkv", b"mkv")
    _write(prepared / "Season 01/Serie.S01E01.es.forced.srt", b"hola")

    publish_series(
        "job-1",
        prepared,
        final,
        _journal(tmp_path / "journal"),
        expected_files=[
            "Season 01/Serie.S01E01.mkv",
            "Season 01/Serie.S01E01.es.forced.srt",
        ],
    )

    assert (final / "Season 01/Serie.S01E01.mkv").read_bytes() == b"mkv"
    assert (final / "Season 01/Serie.S01E01.es.forced.srt").read_bytes() == b"hola"


def test_cross_mount_fallback_copies_once_and_removes_workshop_file(
    tmp_path,
    monkeypatch,
):
    source = _write(tmp_path / "source.mkv", b"episode")
    destination = tmp_path / "tv/Serie/Season 01/episode.mkv"
    calls = []

    monkeypatch.setattr(
        Path,
        "rename",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EXDEV, "cross mount")
        ),
    )

    def move_once(raw_source, raw_destination):
        calls.append((raw_source, raw_destination))
        target = Path(raw_destination)
        target.write_bytes(Path(raw_source).read_bytes())
        Path(raw_source).unlink()

    monkeypatch.setattr(delivery.shutil, "move", move_once)

    delivery._move_one(source, destination)

    assert calls == [(str(source), str(destination))]
    assert destination.read_bytes() == b"episode"
    assert not source.exists()


def test_existing_destination_is_not_overwritten(tmp_path):
    prepared = tmp_path / "taller/job-1/series_work/processed/Serie"
    final = tmp_path / "tv/Serie"
    relative = "Season 01/Serie.S01E01.mkv"
    _write(prepared / relative, b"new")
    destination = _write(final / relative, b"old")

    with pytest.raises(DeliveryConflict):
        publish_series(
            "job-1",
            prepared,
            final,
            _journal(tmp_path / "journal"),
            expected_files=[relative],
        )

    assert destination.read_bytes() == b"old"
    assert (prepared / relative).read_bytes() == b"new"


def test_recovery_finishes_remaining_direct_moves(tmp_path):
    prepared = tmp_path / "taller/job-1/series_work/processed/Serie"
    final = tmp_path / "tv/Serie"
    first = "Season 01/Serie.S01E01.mkv"
    second = "Season 01/Serie.S01E02.mkv"
    _write(final / first, b"one")
    _write(prepared / second, b"two")
    journal = _journal(tmp_path / "journal")
    journal.transition("PROCESSING")
    journal.transition(
        "VERIFIED",
        expected_files=[first, second],
        prepared_series_root=str(prepared),
        final_series_root=str(final),
    )
    journal.transition("COMMITTING")

    result = recover_delivery("job-1", prepared, final, journal)

    assert result["status"] == "committed"
    assert (final / first).read_bytes() == b"one"
    assert (final / second).read_bytes() == b"two"


def test_preflight_is_read_only_and_creates_no_probe(tmp_path):
    root = tmp_path / "tv"
    root.mkdir()

    result = preflight_atomic_exchange(root)

    assert result["operation"] == "direct_move"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    "relative",
    ["../escape.mkv", "/absolute.mkv", ".series-worker-generation.json"],
)
def test_unsafe_expected_path_is_rejected(tmp_path, relative):
    prepared = tmp_path / "taller/job-1/series_work/processed/Serie"
    prepared.mkdir(parents=True)
    final = tmp_path / "tv/Serie"
    final.parent.mkdir(parents=True)

    with pytest.raises(DeliveryError):
        publish_series(
            "job-1",
            prepared,
            final,
            _journal(tmp_path / "journal"),
            expected_files=[relative],
        )
