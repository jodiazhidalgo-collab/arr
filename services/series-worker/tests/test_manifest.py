import os
from pathlib import Path

import pytest

from series_worker.manifest import ManifestError, discover_manifest, validate_relative_path


def _video(root: Path, relative: str, content: bytes = b"video") -> Path:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_single_episode_builds_relative_mkv_target(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Mi Serie/Season 01/Mi.Serie.S01E03.mp4")

    manifest = discover_manifest(source)

    assert manifest.ready
    assert manifest.series_name == "Mi Serie"
    assert len(manifest.entries) == 1
    entry = manifest.entries[0]
    assert entry.season == 1
    assert entry.episodes == (3,)
    assert entry.source_relpath == "Mi Serie/Season 01/Mi.Serie.S01E03.mp4"
    assert entry.target_relpath == "Mi Serie/Season 01/Mi.Serie.S01E03.mkv"
    assert len(entry.content_sha256) == 64
    assert not Path(entry.target_relpath).is_absolute()


def test_multiepisode_is_one_physical_entry_with_multiple_ids(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Serie/Season 02/Serie.S02E04-E05.mkv")

    manifest = discover_manifest(source)

    assert manifest.ready
    assert len(manifest.entries) == 1
    assert manifest.entries[0].episodes == (4, 5)


def test_specials_keep_season_zero_instead_of_treating_it_as_missing(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Serie/Season 00/Serie.S00E01.mkv")

    manifest = discover_manifest(source)

    assert manifest.ready
    assert manifest.entries[0].season == 0
    assert manifest.entries[0].target_relpath.startswith("Serie/Season 00/")


def test_multiple_seasons_of_same_series_are_accepted(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Serie/Season 00/Serie.S00E02.mkv")
    _video(source, "Serie/Season 01/Serie.S01E01.mkv")
    _video(source, "Serie/Season 12/Serie.S12E07.mkv")

    manifest = discover_manifest(source)

    assert manifest.ready
    assert {entry.season for entry in manifest.entries} == {0, 1, 12}


def test_multiple_series_are_sent_to_review_as_one_pack(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Serie A/Season 01/Serie.A.S01E01.mkv")
    _video(source, "Serie B/Season 01/Serie.B.S01E01.mkv")

    manifest = discover_manifest(source)

    assert manifest.status == "review"
    assert manifest.series_name is None
    assert any(reason.startswith("varias_series:") for reason in manifest.review_reasons)
    assert len(manifest.entries) == 2


def test_casefold_series_names_share_one_canonical_root(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Mi Serie/Season 01/Mi.Serie.S01E01.mkv")
    _video(source, "mi serie/Season 02/Mi.Serie.S02E01.mkv")

    manifest = discover_manifest(source)

    assert manifest.ready
    roots = {entry.target_relpath.split("/", 1)[0] for entry in manifest.entries}
    assert roots == {manifest.series_name}


def test_unicode_equivalent_targets_are_detected_as_collision(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Serie/Season 01/Café.S01E01.mkv", b"one")
    _video(source, "Serie/Season 01/Cafe\u0301.S01E01.mp4", b"two")

    manifest = discover_manifest(source)

    assert manifest.status == "review"
    assert any(reason.startswith("colision_casefold:") for reason in manifest.review_reasons)


def test_overlapping_episode_files_and_unknown_video_are_review(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Serie/Season 01/Serie.S01E01.mkv", b"one")
    _video(source, "Serie/Season 01/Serie.S01E01.1080p.mp4", b"two")
    _video(source, "Serie/bonus.mkv", b"bonus")

    manifest = discover_manifest(source)

    assert manifest.status == "review"
    assert any(reason.startswith("episodio_duplicado:") for reason in manifest.review_reasons)
    assert any(reason.startswith("episodio_no_reconocido:") for reason in manifest.review_reasons)


def test_unclassified_file_or_orphan_subtitle_sends_whole_pack_to_review(
    tmp_path: Path,
) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Serie/Season 01/Serie.S01E01.mkv")
    (source / "Serie/Season 01/bonus.txt").write_text("extra", encoding="utf-8")
    (source / "Serie/Season 01/Otra.S01E09.es.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHola\n",
        encoding="utf-8",
    )

    manifest = discover_manifest(source)

    assert manifest.status == "review"
    reasons = [
        reason
        for reason in manifest.review_reasons
        if reason.startswith("archivo_no_clasificado:")
    ]
    assert len(reasons) == 2


def test_digest_changes_when_a_physical_file_changes(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    video = _video(source, "Serie/Season 01/Serie.S01E01.mkv", b"one")
    first = discover_manifest(source)
    video.write_bytes(b"longer")
    os.utime(video, None)

    second = discover_manifest(source)

    assert first.digest != second.digest


def test_video_content_hash_detects_same_size_and_mtime_mutation(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    video = _video(source, "Serie/Season 01/Serie.S01E01.mkv", b"one")
    first = discover_manifest(source)
    original_stat = video.stat()

    video.write_bytes(b"two")
    os.utime(video, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    second = discover_manifest(source)

    assert video.stat().st_size == first.entries[0].size
    assert video.stat().st_mtime_ns == first.entries[0].mtime_ns
    assert first.entries[0].content_sha256 != second.entries[0].content_sha256
    assert first.digest != second.digest


def test_sidecars_are_frozen_with_content_hash_and_change_manifest_digest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Serie/Season 01/Serie.S01E01.mkv")
    sidecar = source / "Serie/Season 01/Serie.S01E01.es.srt"
    sidecar.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHola\n", encoding="utf-8"
    )
    first = discover_manifest(source)
    original_stat = sidecar.stat()

    assert len(first.entries[0].subtitle_sidecars) == 1
    frozen = first.entries[0].subtitle_sidecars[0]
    assert frozen.source_relpath.endswith(".es.srt")
    assert len(frozen.content_sha256) == 64

    sidecar.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nAdio\n", encoding="utf-8"
    )
    os.utime(sidecar, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert sidecar.stat().st_size == frozen.size
    assert sidecar.stat().st_mtime_ns == frozen.mtime_ns
    second = discover_manifest(source)
    assert first.digest != second.digest


def test_unicode_equivalent_sidecars_are_reviewed_and_ordered_deterministically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "series_filebot_output"
    _video(source, "Serie/Season 01/Serie.S01E01.mkv")
    for name in (
        "Serie.S01E01.Café.srt",
        "Serie.S01E01.Cafe\u0301.srt",
    ):
        (source / "Serie/Season 01" / name).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHola\n",
            encoding="utf-8",
        )

    manifest = discover_manifest(source)

    assert manifest.status == "review"
    assert any(
        reason.startswith("colision_sidecar_casefold:")
        for reason in manifest.review_reasons
    )
    assert len(manifest.entries[0].subtitle_sidecars) == 2


def test_relative_path_rejects_absolute_and_traversal() -> None:
    with pytest.raises(ManifestError):
        validate_relative_path("../escape.mkv")
    with pytest.raises(ManifestError):
        validate_relative_path("/absolute/file.mkv")
    with pytest.raises(ManifestError):
        validate_relative_path("folder/../escape.mkv")


def test_symlink_is_rejected_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "series_filebot_output"
    source.mkdir()
    target = _video(tmp_path, "outside/Serie.S01E01.mkv")
    link = source / "Serie.S01E01.mkv"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("El entorno no permite crear symlinks")

    with pytest.raises(ManifestError, match="enlaces simbólicos"):
        discover_manifest(source)
