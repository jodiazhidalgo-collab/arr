import json
import os
import shutil
import subprocess
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from series_worker.manifest import discover_manifest
from series_worker.processing import (
    BASE_TOOLS,
    OCR_TOOLS,
    TOOL_SMOKE_ARGS,
    EpisodeProcessingError,
    ProcessingError,
    SeriesProcessor,
    SubprocessRunner,
    ReviewRequiredError,
    analyze_episode,
    process_manifest,
    unavailable_tools,
)
from series_worker.rules import RulesSnapshot, RulesStore, rules_fingerprint


SRT = (
    "1\n00:00:00,000 --> 00:00:01,000\nHola\n\n"
    "2\n00:00:02,000 --> 00:00:03,000\nAdiós\n\n"
)


def _stream_probe(*, subtitle=None, audio_channels=2, audio_codec="aac", extra_audio=False):
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "tags": {"language": "spa"},
            "disposition": {"attached_pic": 0},
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": audio_codec,
            "channels": audio_channels,
            "bit_rate": "640000",
            "tags": {"language": "spa"},
            "disposition": {},
        },
    ]
    if extra_audio:
        streams.append(
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "truehd",
                "channels": 8,
                "bit_rate": "4000000",
                "tags": {"language": "eng"},
                "disposition": {},
            }
        )
    if subtitle:
        streams.append(subtitle)
    return {"format": {"duration": "10.0", "format_name": "matroska"}, "streams": streams}


class FakeRunner:
    def __init__(self, probes=None, fail_source=""):
        self.probes = probes or {}
        self.fail_source = fail_source
        self.commands = []
        self.generated_subtitles = {}

    def which(self, executable):
        return f"/fake/{executable}"

    def run(self, argv, *, timeout, cwd=None):
        argv = [str(value) for value in argv]
        self.commands.append((argv, Path(cwd) if cwd else None))
        executable = argv[0]
        if executable == "ffprobe":
            path = Path(argv[-1])
            if path in self.generated_subtitles:
                has_subtitle = self.generated_subtitles[path]
                probe = _stream_probe(
                    subtitle=(
                        {
                            "index": 2,
                            "codec_type": "subtitle",
                            "codec_name": "subrip",
                            "tags": {"language": "es"},
                            "disposition": {},
                        }
                        if has_subtitle
                        else None
                    ),
                    audio_codec="ac3",
                    audio_channels=6,
                )
            else:
                probe = self.probes.get(path.name, _stream_probe())
            return subprocess.CompletedProcess(argv, 0, json.dumps(probe), "")
        if executable == "ffmpeg":
            if argv[-3:] == ["-f", "null", "-"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[-1] == "-" and "srt" in argv:
                return subprocess.CompletedProcess(argv, 0, SRT, "")
            output = Path(argv[-1])
            if self.fail_source and any(self.fail_source in item for item in argv):
                return subprocess.CompletedProcess(argv, 1, "", "fallo controlado")
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.suffix.casefold() in {".mkv", ".srt"}:
                output.write_bytes(b"mkv-provisional" if output.suffix.casefold() == ".mkv" else SRT.encode())
            if output.suffix.casefold() == ".mkv":
                self.generated_subtitles[output] = "-c:s" in argv
            return subprocess.CompletedProcess(argv, 0, "", "")
        if executable == "mkvmerge":
            path = Path(argv[-1])
            tracks = [{"type": "video"}, {"type": "audio"}]
            if self.generated_subtitles.get(path):
                tracks.append({"type": "subtitles"})
            return subprocess.CompletedProcess(argv, 0, json.dumps({"tracks": tracks}), "")
        if executable == "mkvpropedit":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if executable == "seconv":
            name = next(
                item.split(":", 1)[1]
                for item in argv
                if item.startswith("--output-filename:")
            )
            destination = (Path(cwd) if cwd else Path.cwd()) / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(SRT, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if executable == "mkvextract":
            destination = Path(argv[-1].split(":", 1)[1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("idx", encoding="utf-8")
            destination.with_suffix(".sub").write_bytes(b"sub")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if executable == "vobsubocr":
            destination = Path(argv[argv.index("--output") + 1])
            destination.write_text(SRT, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


class ToolProbeRunner:
    def __init__(self, *, absent=(), outcomes=None):
        self.absent = set(absent)
        self.outcomes = outcomes or {}
        self.calls = []

    def which(self, executable):
        return None if executable in self.absent else f"/fake/{executable}"

    def run(self, argv, *, timeout, cwd=None):
        argv = [str(value) for value in argv]
        self.calls.append((argv, timeout, cwd))
        outcome = self.outcomes.get(argv[0], 0)
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(argv, outcome, "version", "")


def test_unavailable_tools_executes_every_smoke_probe_with_bounded_timeout() -> None:
    names = (*BASE_TOOLS, *OCR_TOOLS)
    runner = ToolProbeRunner()

    assert unavailable_tools(runner, names, timeout=7) == []
    assert runner.calls == [
        ([name, *TOOL_SMOKE_ARGS[name]], 7, None)
        for name in names
    ]


def test_unavailable_tools_catches_absent_crash_timeout_and_nonzero() -> None:
    runner = ToolProbeRunner(
        absent={"ffmpeg"},
        outcomes={
            "ffprobe": OSError("exec format error"),
            "mkvmerge": subprocess.TimeoutExpired(["mkvmerge", "--version"], 3),
            "mkvpropedit": 2,
        },
    )

    assert unavailable_tools(
        runner,
        ("mkvpropedit", "ffmpeg", "mkvmerge", "ffprobe"),
        timeout=3,
    ) == ["ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit"]
    assert all(call[0][0] != "ffmpeg" for call in runner.calls)


def test_vobsubocr_on_path_but_missing_dynamic_library_is_unavailable() -> None:
    runner = ToolProbeRunner(outcomes={"vobsubocr": 127})

    missing = unavailable_tools(runner, ("vobsubocr",), timeout=5)

    assert missing == ["vobsubocr"]
    assert runner.calls == [(["vobsubocr", "--help"], 5, None)]


def test_unavailable_tools_can_probe_all_binaries_in_parallel() -> None:
    names = BASE_TOOLS
    barrier = threading.Barrier(len(names))
    thread_ids: set[int] = set()
    mutex = threading.Lock()

    class ParallelRunner(ToolProbeRunner):
        def run(self, argv, *, timeout, cwd=None):
            with mutex:
                thread_ids.add(threading.get_ident())
            barrier.wait(timeout=2)
            return super().run(argv, timeout=timeout, cwd=cwd)

    runner = ParallelRunner()

    assert unavailable_tools(runner, names, timeout=2, parallel=True) == []
    assert len(thread_ids) == len(names)
    assert sorted(call[0][0] for call in runner.calls) == sorted(names)


def _snapshot(tmp_path: Path, change=None) -> RulesSnapshot:
    rules = deepcopy(RulesStore(config_path=tmp_path / "rules.json").snapshot().rules)
    if change:
        change(rules)
    return RulesSnapshot(rules, rules_fingerprint(rules))


def _job(tmp_path: Path, names):
    job = tmp_path / "job-1"
    source = job / "series_filebot_output"
    for name in names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("original-" + name).encode())
    return job, source, discover_manifest(source)


@pytest.mark.parametrize("suffix", [".mkv", ".mp4", ".avi"])
def test_supported_inputs_are_processed_to_mkv_without_touching_original(
    tmp_path: Path, suffix: str
) -> None:
    relative = f"Serie/Season 01/Serie.S01E01{suffix}"
    job, source, manifest = _job(tmp_path, [relative])
    original = (source / relative).read_bytes()

    result = process_manifest(manifest, source, job, _snapshot(tmp_path), FakeRunner())

    output = job / "series_work/processed/Serie/Season 01/Serie.S01E01.mkv"
    assert result.status == "verified"
    assert output.is_file()
    assert (source / relative).read_bytes() == original
    assert result.episodes[0].provisional_relpath == "series_work/processed/Serie/Season 01/Serie.S01E01.mkv"


def test_entries_run_sequentially_and_failure_never_publishes(tmp_path: Path) -> None:
    first = "Serie/Season 01/Serie.S01E01.mkv"
    second = "Serie/Season 01/Serie.S01E02.mkv"
    job, source, manifest = _job(tmp_path, [first, second])
    runner = FakeRunner(fail_source="S01E02")

    with pytest.raises(EpisodeProcessingError) as captured:
        SeriesProcessor(runner).process(
            manifest=manifest,
            source_root=source,
            job_root=job,
            rules_snapshot=_snapshot(tmp_path),
        )

    assert len(captured.value.partial_results) == 1
    assert (job / "series_work/processed/Serie/Season 01/Serie.S01E01.mkv").exists()
    assert not (job / "series_work/processed/Serie/Season 01/Serie.S01E02.mkv").exists()
    assert not (job / "tv").exists()
    assert (source / first).exists() and (source / second).exists()


def test_video_hash_drift_fails_even_with_same_size_and_mtime(tmp_path: Path) -> None:
    relative = "Serie/Season 01/Serie.S01E01.mkv"
    job, source, manifest = _job(tmp_path, [relative])
    video = source / relative
    original_stat = video.stat()
    changed = bytearray(video.read_bytes())
    changed[0] ^= 1
    video.write_bytes(changed)
    os.utime(
        video,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    with pytest.raises(EpisodeProcessingError, match="hash de la entrada"):
        process_manifest(manifest, source, job, _snapshot(tmp_path), FakeRunner())


def test_selects_spanish_audio_converts_six_channels_and_discards_english(
    tmp_path: Path,
) -> None:
    relative = "Serie/Season 01/Serie.S01E01.mkv"
    job, source, manifest = _job(tmp_path, [relative])
    probe = _stream_probe(audio_channels=6, audio_codec="dts", extra_audio=True)
    runner = FakeRunner({"Serie.S01E01.mkv": probe})

    result = process_manifest(manifest, source, job, _snapshot(tmp_path), runner)

    remux = next(command for command, _ in runner.commands if command[0] == "ffmpeg" and command[-1].endswith(".mkv"))
    assert result.episodes[0].audio_mode == "convert_ac3_5_1"
    assert ["-map", "0:1"] == remux[remux.index("-map", remux.index("-map") + 1):][:2]
    assert remux[remux.index("-c:a") + 1] == "ac3"
    assert "0:2" not in remux


def test_single_untagged_audio_is_accepted_by_legacy_video_es_flag(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Serie.S01E01.mkv"
    source.write_bytes(b"episode")
    probe = _stream_probe()
    audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    audio["tags"] = {}
    runner = FakeRunner({source.name: probe})
    snapshot = _snapshot(
        tmp_path,
        lambda rules: rules["audio"].update(
            {
                "aceptar_indeterminado_si_video_es": True,
                "idiomas_condicionales_si_video_es": ["und", "unknown"],
            }
        ),
    )

    plan = analyze_episode(source, tmp_path / "output.mkv", snapshot, runner)

    assert plan.audio["index"] == audio["index"] == 1
    assert plan.audio["tags"] == {}
    assert plan.audio_mode == "copy"


def test_single_untagged_audio_is_rejected_when_legacy_flag_is_disabled(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Serie.S01E01.mkv"
    source.write_bytes(b"episode")
    probe = _stream_probe()
    audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    audio["tags"] = {}
    runner = FakeRunner({source.name: probe})
    snapshot = _snapshot(
        tmp_path,
        lambda rules: rules["audio"].update(
            {
                "aceptar_indeterminado_si_video_es": False,
                "idiomas_condicionales_si_video_es": ["und", "unknown"],
            }
        ),
    )

    with pytest.raises(ProcessingError, match="No hay audio español válido"):
        analyze_episode(source, tmp_path / "output.mkv", snapshot, runner)


def test_internal_delay_subtitle_is_prioritized_and_exported(tmp_path: Path) -> None:
    relative = "Serie/Season 01/Serie.S01E01.mkv"
    job, source, manifest = _job(tmp_path, [relative])
    subtitle = {
        "index": 2,
        "codec_type": "subtitle",
        "codec_name": "subrip",
        "tags": {"language": "spa", "title": "ESPAÑOL delay audio"},
        "disposition": {"forced": 0},
    }
    runner = FakeRunner({"Serie.S01E01.mkv": _stream_probe(subtitle=subtitle)})

    result = process_manifest(manifest, source, job, _snapshot(tmp_path), runner)

    assert result.episodes[0].subtitle_mode == "internal_delay"
    assert (job / "series_work/processed/Serie/Season 01/Serie.S01E01.es.forced.srt").exists()


def test_external_spanish_subtitle_is_embedded(tmp_path: Path) -> None:
    relative = "Serie/Season 01/Serie.S01E01.mp4"
    job, source, _manifest = _job(tmp_path, [relative])
    external = source / "Serie/Season 01/Serie.S01E01.es.forced.srt"
    external.write_text(SRT, encoding="utf-8")
    manifest = discover_manifest(source)
    runner = FakeRunner({"Serie.S01E01.mp4": _stream_probe()})

    result = process_manifest(manifest, source, job, _snapshot(tmp_path), runner)

    assert result.episodes[0].subtitle_mode == "external_text"
    remux = next(command for command, _ in runner.commands if command[0] == "ffmpeg" and command[-1].endswith(".mkv"))
    assert str(external) in remux
    assert "1:0" in remux


def test_frozen_sidecar_changed_after_manifest_is_rejected(tmp_path: Path) -> None:
    relative = "Serie/Season 01/Serie.S01E01.mkv"
    job, source, _manifest = _job(tmp_path, [relative])
    sidecar = source / "Serie/Season 01/Serie.S01E01.es.srt"
    sidecar.write_text(SRT, encoding="utf-8")
    manifest = discover_manifest(source)
    sidecar.write_text(SRT.replace("Hola", "Texto cambiado"), encoding="utf-8")

    with pytest.raises(EpisodeProcessingError, match="sidecar cambió"):
        process_manifest(manifest, source, job, _snapshot(tmp_path), FakeRunner())


def test_french_and_german_sidecars_are_never_selected_as_spanish(tmp_path: Path) -> None:
    relative = "Serie/Season 01/Serie.S01E01.mkv"
    job, source, _manifest = _job(tmp_path, [relative])
    french = source / "Serie/Season 01/Serie.S01E01.fr.srt"
    german = source / "Serie/Season 01/Serie.S01E01.de.forced.srt"
    french.write_text(SRT, encoding="utf-8")
    german.write_text(SRT, encoding="utf-8")
    manifest = discover_manifest(source)
    runner = FakeRunner()

    result = process_manifest(manifest, source, job, _snapshot(tmp_path), runner)

    assert result.episodes[0].subtitle_mode == "none"
    remux = next(command for command, _ in runner.commands if command[0] == "ffmpeg" and command[-1].endswith(".mkv"))
    assert str(french) not in remux
    assert str(german) not in remux


def test_no_subtitles_is_valid_when_snapshot_allows_it(tmp_path: Path) -> None:
    relative = "Serie/Season 01/Serie.S01E01.avi"
    job, source, manifest = _job(tmp_path, [relative])

    result = process_manifest(manifest, source, job, _snapshot(tmp_path), FakeRunner())

    assert result.episodes[0].subtitle_mode == "none"


def test_image_subtitle_uses_real_ocr_route_contract(tmp_path: Path) -> None:
    relative = "Serie/Season 01/Serie.S01E01.mkv"
    job, source, manifest = _job(tmp_path, [relative])
    subtitle = {
        "index": 2,
        "codec_type": "subtitle",
        "codec_name": "hdmv_pgs_subtitle",
        "tags": {"language": "spa"},
        "disposition": {},
    }
    runner = FakeRunner({"Serie.S01E01.mkv": _stream_probe(subtitle=subtitle)})

    result = process_manifest(manifest, source, job, _snapshot(tmp_path), runner)

    assert result.episodes[0].subtitle_mode == "ocr"
    assert any(command[0] == "seconv" and "--ocr-engine:tesseract" in command for command, _ in runner.commands)


def test_unknown_spanish_subtitle_codec_requires_review(tmp_path: Path) -> None:
    relative = "Serie/Season 01/Serie.S01E01.mkv"
    job, source, manifest = _job(tmp_path, [relative])
    subtitle = {
        "index": 2,
        "codec_type": "subtitle",
        "codec_name": "mystery_subtitle",
        "tags": {"language": "spa"},
        "disposition": {},
    }
    runner = FakeRunner({"Serie.S01E01.mkv": _stream_probe(subtitle=subtitle)})

    with pytest.raises(EpisodeProcessingError) as captured:
        process_manifest(manifest, source, job, _snapshot(tmp_path), runner)

    assert isinstance(captured.value.__cause__, ReviewRequiredError)


def test_ocr_result_reapplies_cue_limit_before_embedding(tmp_path: Path) -> None:
    relative = "Serie/Season 01/Serie.S01E01.mkv"
    job, source, manifest = _job(tmp_path, [relative])
    subtitle = {
        "index": 2,
        "codec_type": "subtitle",
        "codec_name": "hdmv_pgs_subtitle",
        "tags": {"language": "spa"},
        "disposition": {},
    }

    class LongOCRRunner(FakeRunner):
        def run(self, argv, *, timeout, cwd=None):
            result = super().run(argv, timeout=timeout, cwd=cwd)
            if str(argv[0]) == "seconv":
                name = next(
                    str(item).split(":", 1)[1]
                    for item in argv
                    if str(item).startswith("--output-filename:")
                )
                cues = []
                for number in range(1, 152):
                    second = number % 50
                    cues.append(
                        f"{number}\n00:00:{second:02d},000 --> 00:00:{second:02d},500\nTexto\n"
                    )
                (Path(cwd) / name).write_text("\n".join(cues), encoding="utf-8")
            return result

    runner = LongOCRRunner({"Serie.S01E01.mkv": _stream_probe(subtitle=subtitle)})
    result = process_manifest(manifest, source, job, _snapshot(tmp_path), runner)

    assert result.episodes[0].subtitle_mode == "none"
    remux = next(command for command, _ in runner.commands if command[0] == "ffmpeg" and command[-1].endswith(".mkv"))
    assert "-c:s" not in remux


def test_changed_file_after_manifest_is_rejected(tmp_path: Path) -> None:
    relative = "Serie/Season 01/Serie.S01E01.mkv"
    job, source, manifest = _job(tmp_path, [relative])
    (source / relative).write_bytes(b"changed-after-freeze")

    with pytest.raises(EpisodeProcessingError, match="cambió"):
        process_manifest(manifest, source, job, _snapshot(tmp_path), FakeRunner())


class AnalyzeWithRealProbe(SubprocessRunner):
    def which(self, executable):
        return shutil.which(executable) or f"/contract/{executable}"


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg/ffprobe no disponibles",
)
def test_real_ffmpeg_fixture_is_probed_and_planned(tmp_path: Path) -> None:
    fixture = tmp_path / "Serie.S01E01.mp4"
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=2",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
        "-t", "1", "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
        "-metadata:s:v:0", "language=spa", "-metadata:s:a:0", "language=spa",
        str(fixture),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr

    plan = analyze_episode(
        fixture,
        tmp_path / "out.mkv",
        _snapshot(tmp_path),
        AnalyzeWithRealProbe(),
    )

    assert plan.video["codec_type"] == "video"
    assert plan.audio["codec_type"] == "audio"
    assert plan.subtitle is None
    assert plan.duration > 0


@pytest.mark.skipif(
    any(not shutil.which(tool) for tool in ("ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit")),
    reason="Toolchain audiovisual completa no disponible",
)
def test_real_remux_verifies_tracks_metadata_dispositions_chapters_and_delay(
    tmp_path: Path,
) -> None:
    job = tmp_path / "job-real"
    source_root = job / "series_filebot_output"
    source = source_root / "Serie/Season 01/Serie.S01E01.mkv"
    source.parent.mkdir(parents=True)
    subtitle_input = tmp_path / "internal.srt"
    subtitle_input.write_text(SRT, encoding="utf-8")
    create = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=96x64:r=2",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=5.1:sample_rate=48000",
        "-i", str(subtitle_input), "-t", "4", "-shortest",
        "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
        "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "srt",
        "-metadata", "title=ORIGINAL_SECRET",
        "-metadata:s:v:0", "language=spa",
        "-metadata:s:a:0", "language=spa",
        "-metadata:s:s:0", "language=spa",
        "-metadata:s:s:0", "title=ESPAÑOL delay audio",
        str(source),
    ]
    created = subprocess.run(create, capture_output=True, text=True, timeout=60)
    assert created.returncode == 0, created.stderr
    manifest = discover_manifest(source_root)

    result = process_manifest(
        manifest,
        source_root,
        job,
        _snapshot(tmp_path),
        SubprocessRunner(),
    )
    output = job / "series_work/processed/Serie/Season 01/Serie.S01E01.mkv"
    inspected = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_format", "-show_streams",
            "-show_chapters", "-print_format", "json", str(output),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert inspected.returncode == 0, inspected.stderr
    probe = json.loads(inspected.stdout)
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    subtitle = next(stream for stream in probe["streams"] if stream["codec_type"] == "subtitle")

    assert result.episodes[0].audio_mode == "convert_ac3_5_1"
    assert result.episodes[0].subtitle_mode == "internal_delay"
    assert video["tags"]["language"] == "es"
    assert audio["tags"]["language"] == "es"
    assert audio["codec_name"] == "ac3" and audio["channels"] == 6
    assert subtitle["tags"]["language"] == "es"
    assert subtitle["tags"]["title"] == "Forzados"
    assert video["disposition"]["default"] == 1
    assert audio["disposition"]["default"] == 1
    assert (probe.get("format", {}).get("tags") or {}).get("title") != "ORIGINAL_SECRET"
    assert probe.get("chapters")
    assert output.with_name("Serie.S01E01.es.forced.srt").is_file()
