from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional


MIN_MOVIE_DURATION_SECONDS = 25 * 60
AMBIGUITY_SECONDS = 180
AMBIGUITY_RATIO = 0.03
DURATION_TOLERANCE_SECONDS = 10
DURATION_TOLERANCE_RATIO = 0.005
MIN_REMUX_SIZE_BYTES = 1024 * 1024
PROBE_TIMEOUT_SECONDS = 300
REMUX_TIMEOUT_SECONDS = 4 * 60 * 60
VERIFY_TIMEOUT_SECONDS = 4 * 60 * 60
MAX_DIAGNOSTIC_CANDIDATES = 20
MAX_LOG_CHARS = 12000
MPLS_AUDIO_CODING_TYPES = {
    0x03,
    0x04,
    0x80,
    0x81,
    0x82,
    0x83,
    0x84,
    0x85,
    0x86,
    0xA1,
    0xA2,
}
MPLS_SUBTITLE_CODING_TYPES = {0x90, 0x91, 0x92}

CommandRunner = Callable[[List[str], int], subprocess.CompletedProcess]
EventCallback = Callable[[str, str, str, Dict[str, object]], None]


def _run_command(argv: List[str], timeout_seconds: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )


def _child_named(folder: Path, name: str) -> Optional[Path]:
    if not folder.exists() or not folder.is_dir():
        return None
    wanted = name.casefold()
    try:
        children = folder.iterdir()
    except OSError:
        return None
    return next((child for child in children if child.name.casefold() == wanted), None)


def _playlist_files(folder: Path) -> List[Path]:
    bdmv = _child_named(folder, "BDMV")
    playlist_dir = _child_named(bdmv, "PLAYLIST") if bdmv else None
    if not playlist_dir or not playlist_dir.is_dir():
        return []
    return sorted(
        (
            path
            for path in playlist_dir.iterdir()
            if path.is_file() and path.suffix.casefold() == ".mpls"
        ),
        key=lambda path: path.name.casefold(),
    )


def _stream_pid(value: object) -> Optional[int]:
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _language_code(value: bytes | str | None) -> Optional[str]:
    try:
        text = value.decode("ascii") if isinstance(value, bytes) else str(value or "")
    except UnicodeDecodeError:
        return None
    text = text.casefold()
    return text if re.fullmatch(r"[a-z]{3}", text) else None


def _mpls_stream_language(coding_info: bytes) -> tuple[Optional[str], Optional[str]]:
    if not coding_info:
        return None, None
    coding_type = coding_info[0]
    if coding_type in MPLS_AUDIO_CODING_TYPES and len(coding_info) >= 5:
        return "audio", _language_code(coding_info[2:5])
    if coding_type in {0x90, 0x91} and len(coding_info) >= 4:
        return "subtitle", _language_code(coding_info[1:4])
    if coding_type == 0x92 and len(coding_info) >= 5:
        return "subtitle", _language_code(coding_info[2:5])
    return None, None


def read_playlist_stream_languages(
    playlist_path: Path,
    expected_streams: Iterable[Dict[str, object]],
) -> Dict[str, object]:
    expected_by_pid: Dict[int, str] = {}
    for stream in expected_streams:
        codec_type = str(stream.get("codec_type") or "")
        pid = _stream_pid(stream.get("pid"))
        if codec_type in {"audio", "subtitle"} and pid is not None:
            expected_by_pid[pid] = codec_type
    if not expected_by_pid:
        return {"status": "stream_ids_unavailable", "streams": [], "conflicts": []}

    try:
        data = Path(playlist_path).read_bytes()
    except OSError as error:
        return {"status": "read_failed", "streams": [], "conflicts": [], "reason": str(error)}
    if len(data) < 8 or not data.startswith(b"MPLS"):
        return {"status": "invalid_playlist", "streams": [], "conflicts": []}

    languages_by_pid: Dict[int, set[str]] = {}
    for offset in range(8, len(data) - 2):
        entry_length = data[offset]
        if entry_length < 3 or entry_length > 32:
            continue
        entry_start = offset + 1
        entry_end = entry_start + entry_length
        if entry_end >= len(data):
            continue
        stream_type = data[entry_start]
        if stream_type == 1 and entry_length >= 3:
            pid_offset = entry_start + 1
        elif stream_type == 2 and entry_length >= 5:
            pid_offset = entry_start + 3
        elif stream_type in {3, 4} and entry_length >= 4:
            pid_offset = entry_start + 2
        else:
            continue
        pid = int.from_bytes(data[pid_offset : pid_offset + 2], "big")
        expected_type = expected_by_pid.get(pid)
        if not expected_type:
            continue
        coding_length = data[entry_end]
        coding_start = entry_end + 1
        coding_end = coding_start + coding_length
        if coding_length < 1 or coding_end > len(data):
            continue
        codec_type, language = _mpls_stream_language(data[coding_start:coding_end])
        if codec_type == expected_type and language:
            languages_by_pid.setdefault(pid, set()).add(language)

    conflicts = [
        {"pid": f"0x{pid:04x}", "languages": sorted(languages)}
        for pid, languages in sorted(languages_by_pid.items())
        if len(languages) > 1
    ]
    streams = [
        {
            "pid": f"0x{pid:04x}",
            "codec_type": expected_by_pid[pid],
            "language": next(iter(languages)),
        }
        for pid, languages in sorted(languages_by_pid.items())
        if len(languages) == 1
    ]
    return {
        "status": "parsed" if streams else "no_languages",
        "streams": streams,
        "conflicts": conflicts,
    }


def _merge_playlist_languages(
    streams: List[Dict[str, object]],
    playlist_languages: Dict[str, object],
) -> List[Dict[str, object]]:
    by_pid = {
        _stream_pid(item.get("pid")): str(item.get("language") or "")
        for item in (playlist_languages.get("streams") or [])
        if isinstance(item, dict)
    }
    type_indexes = {"audio": 0, "subtitle": 0}
    metadata: List[Dict[str, object]] = []
    for stream in streams:
        codec_type = str(stream.get("codec_type") or "")
        if codec_type not in type_indexes:
            continue
        type_index = type_indexes[codec_type]
        type_indexes[codec_type] += 1
        language = _language_code(str(stream.get("language") or ""))
        source = "ffprobe" if language else ""
        if not language:
            language = _language_code(by_pid.get(_stream_pid(stream.get("pid"))))
            source = "mpls" if language else ""
        if not language:
            continue
        stream["language"] = language
        stream["language_source"] = source
        metadata.append(
            {
                "codec_type": codec_type,
                "type_index": type_index,
                "pid": stream.get("pid"),
                "language": language,
                "source": source,
            }
        )
    return metadata


def is_full_bluray_folder(path: Path) -> bool:
    path = Path(path)
    bdmv = _child_named(path, "BDMV")
    return bool(bdmv and bdmv.is_dir() and _playlist_files(path))


def find_full_bluray_folders(path: Path) -> List[Path]:
    path = Path(path)
    if not path.exists() or not path.is_dir():
        return []
    matches: List[Path] = []
    if is_full_bluray_folder(path):
        matches.append(path)
    for child in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
        if child.is_dir() and is_full_bluray_folder(child):
            matches.append(child)
    unique: Dict[str, Path] = {}
    for match in matches:
        unique[str(match.resolve())] = match
    return list(unique.values())


def find_full_bluray_folder(path: Path) -> Optional[Path]:
    matches = find_full_bluray_folders(path)
    return matches[0] if len(matches) == 1 else None


def _float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _duration_from_probe(data: Dict[str, object]) -> float:
    format_data = data.get("format") or {}
    if isinstance(format_data, dict):
        duration = _float(format_data.get("duration"))
        if duration > 0:
            return duration
    streams = data.get("streams") or []
    stream_durations = [
        _float(stream.get("duration"))
        for stream in streams
        if isinstance(stream, dict)
    ]
    chapters = data.get("chapters") or []
    chapter_ends = [
        _float(chapter.get("end_time"))
        for chapter in chapters
        if isinstance(chapter, dict)
    ]
    return max([0.0, *stream_durations, *chapter_ends])


def _probe_summary(data: Dict[str, object]) -> Dict[str, object]:
    streams = [item for item in (data.get("streams") or []) if isinstance(item, dict)]
    chapters = [item for item in (data.get("chapters") or []) if isinstance(item, dict)]
    return {
        "duration": round(_duration_from_probe(data), 3),
        "video_streams": sum(item.get("codec_type") == "video" for item in streams),
        "audio_streams": sum(item.get("codec_type") == "audio" for item in streams),
        "subtitle_streams": sum(item.get("codec_type") == "subtitle" for item in streams),
        "chapter_count": len(chapters),
        "chapter_probe_status": "reported" if chapters else "not_reported_by_ffprobe",
        "streams": [
            {
                "index": item.get("index"),
                "pid": item.get("id"),
                "codec_type": item.get("codec_type"),
                "codec_name": item.get("codec_name"),
                "channels": item.get("channels"),
                "language": (item.get("tags") or {}).get("language")
                if isinstance(item.get("tags"), dict)
                else None,
            }
            for item in streams
        ],
    }


def _bluray_url(root: Path) -> str:
    return f"bluray:{root}"


def _tail(value: object) -> str:
    return str(value or "")[-MAX_LOG_CHARS:]


def _safe_release_name(value: str) -> str:
    text = re.sub(r"[\\/]+", " ", value or "").strip()
    text = re.sub(r"[\x00-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:180] or "pelicula-bluray"


def _event(
    callback: Optional[EventCallback],
    name: str,
    event_type: str,
    message: str,
    structured: Optional[Dict[str, object]] = None,
) -> None:
    if callback:
        callback(name, event_type, message, {"event_name": name, **(structured or {})})


def _playlist_probe_command(root: Path, playlist_id: int) -> List[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-playlist",
        str(playlist_id),
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        _bluray_url(root),
    ]


def _public_candidates(candidates: Iterable[Dict[str, object]]) -> List[Dict[str, object]]:
    return [
        {
            key: value
            for key, value in candidate.items()
            if key not in {"probe", "playlist_path"}
        }
        for candidate in list(candidates)[:MAX_DIAGNOSTIC_CANDIDATES]
    ]


def select_main_playlist(
    bluray_root: Path,
    runner: CommandRunner = _run_command,
    min_duration_seconds: int = MIN_MOVIE_DURATION_SECONDS,
    ambiguity_seconds: int = AMBIGUITY_SECONDS,
    ambiguity_ratio: float = AMBIGUITY_RATIO,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
) -> Dict[str, object]:
    bluray_root = Path(bluray_root)
    candidates: List[Dict[str, object]] = []
    rejected: List[Dict[str, object]] = []
    playlists = _playlist_files(bluray_root)
    for playlist in playlists:
        try:
            playlist_id = int(playlist.stem)
        except ValueError:
            rejected.append({"playlist": playlist.name, "reason": "playlist_id_invalid"})
            continue
        argv = _playlist_probe_command(bluray_root, playlist_id)
        started = time.monotonic()
        try:
            completed = runner(argv, timeout_seconds)
        except (OSError, subprocess.SubprocessError) as error:
            rejected.append(
                {"playlist": playlist.name, "playlist_id": playlist_id, "reason": "probe_error", "error": str(error)}
            )
            continue
        elapsed = round(time.monotonic() - started, 3)
        if completed.returncode != 0:
            rejected.append(
                {
                    "playlist": playlist.name,
                    "playlist_id": playlist_id,
                    "reason": "probe_failed",
                    "returncode": completed.returncode,
                    "elapsed_seconds": elapsed,
                    "stderr_tail": _tail(completed.stderr),
                }
            )
            continue
        try:
            probe = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as error:
            rejected.append(
                {"playlist": playlist.name, "playlist_id": playlist_id, "reason": "probe_json_invalid", "error": str(error)}
            )
            continue
        summary = _probe_summary(probe)
        playlist_languages = read_playlist_stream_languages(playlist, summary["streams"])
        summary["stream_languages"] = _merge_playlist_languages(
            summary["streams"],
            playlist_languages,
        )
        summary["playlist_language_metadata"] = playlist_languages
        if int(summary["video_streams"]) < 1:
            rejected.append({"playlist": playlist.name, "playlist_id": playlist_id, "reason": "no_video", **summary})
            continue
        if float(summary["duration"]) < float(min_duration_seconds):
            rejected.append({"playlist": playlist.name, "playlist_id": playlist_id, "reason": "too_short", **summary})
            continue
        candidates.append(
            {
                "playlist": playlist.name,
                "playlist_id": playlist_id,
                "elapsed_seconds": elapsed,
                **summary,
                "probe": probe,
                "playlist_path": str(playlist),
            }
        )

    candidates.sort(
        key=lambda item: (
            float(item["duration"]),
            int(item["chapter_count"]),
            int(item["audio_streams"]) + int(item["subtitle_streams"]),
        ),
        reverse=True,
    )
    public = _public_candidates(candidates)
    base = {
        "playlists_scanned": len(playlists),
        "valid_candidates": len(candidates),
        "candidates": public,
        "rejected": rejected[:MAX_DIAGNOSTIC_CANDIDATES],
        "thresholds": {
            "min_duration_seconds": min_duration_seconds,
            "ambiguity_seconds": ambiguity_seconds,
            "ambiguity_ratio": ambiguity_ratio,
        },
    }
    if not candidates:
        return {"status": "no_safe_playlist", "reason": "No hay playlists de pelicula legibles", **base}

    selected = candidates[0]
    if len(candidates) > 1:
        second = candidates[1]
        difference = float(selected["duration"]) - float(second["duration"])
        ambiguity_limit = max(float(ambiguity_seconds), float(selected["duration"]) * ambiguity_ratio)
        if difference <= ambiguity_limit:
            return {
                "status": "ambiguous",
                "reason": "Las dos playlists principales tienen duraciones demasiado cercanas",
                "duration_difference_seconds": round(difference, 3),
                "ambiguity_limit_seconds": round(ambiguity_limit, 3),
                **base,
            }
    return {"status": "selected", "selected": _public_candidates([selected])[0], **base}


def build_remux_command(
    bluray_root: Path,
    playlist_id: int,
    output_tmp: Path,
    stream_languages: Optional[Iterable[Dict[str, object]]] = None,
) -> List[str]:
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-n",
        "-playlist",
        str(playlist_id),
        "-i",
        _bluray_url(Path(bluray_root)),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-map_chapters",
        "0",
        "-map_metadata",
        "0",
        "-ignore_unknown",
        "-dn",
        "-c",
        "copy",
    ]
    stream_specifiers = {"audio": "a", "subtitle": "s"}
    for item in stream_languages or []:
        codec_type = str(item.get("codec_type") or "")
        language = _language_code(str(item.get("language") or ""))
        try:
            type_index = int(item.get("type_index", -1))
        except (TypeError, ValueError):
            continue
        specifier = stream_specifiers.get(codec_type)
        if specifier and type_index >= 0 and language:
            argv.extend([f"-metadata:s:{specifier}:{type_index}", f"language={language}"])
    argv.append(str(output_tmp))
    return argv


def _sanitize_command(argv: Iterable[str], root: Path, output: Path) -> List[str]:
    root_text = str(root)
    output_text = str(output)
    return [
        str(item).replace(_bluray_url(root), "bluray:<BLURAY_ROOT>").replace(root_text, "<BLURAY_ROOT>").replace(output_text, "<BLURAY_OUTPUT>")
        for item in argv
    ]


def remux_playlist_to_mkv(
    bluray_root: Path,
    playlist_id: int,
    output_tmp: Path,
    stream_languages: Optional[Iterable[Dict[str, object]]] = None,
    runner: CommandRunner = _run_command,
    timeout_seconds: int = REMUX_TIMEOUT_SECONDS,
) -> Dict[str, object]:
    bluray_root = Path(bluray_root)
    output_tmp = Path(output_tmp)
    if output_tmp.exists():
        return {"status": "remux_failed", "reason": "temporary_output_exists", "returncode": None}
    language_metadata = list(stream_languages or [])
    argv = build_remux_command(bluray_root, playlist_id, output_tmp, language_metadata)
    started = time.monotonic()
    try:
        completed = runner(argv, timeout_seconds)
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "status": "remux_failed",
            "reason": "ffmpeg_exception",
            "error": str(error),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "command_preview": _sanitize_command(argv, bluray_root, output_tmp),
        }
    return {
        "status": "remux_finished" if completed.returncode == 0 else "remux_failed",
        "returncode": completed.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
        "command_preview": {
            "argv": _sanitize_command(argv, bluray_root, output_tmp),
            "timeout_sec": timeout_seconds,
            "stream_copy": True,
            "mapped_streams": ["video", "audio", "subtitle"],
            "chapters_mapped": True,
            "data_streams_excluded": True,
            "language_tags": language_metadata,
        },
        "output_size": output_tmp.stat().st_size if output_tmp.exists() else 0,
    }


def _verify_probe_command(output_tmp: Path) -> List[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(output_tmp),
    ]


def _verify_read_command(output_tmp: Path) -> List[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-i",
        str(output_tmp),
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-f",
        "null",
        "-",
    ]


def verify_bluray_remux(
    output_tmp: Path,
    expected_duration: float,
    expected_chapters: int,
    expected_stream_languages: Optional[Iterable[Dict[str, object]]] = None,
    runner: CommandRunner = _run_command,
    probe_timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    read_timeout_seconds: int = VERIFY_TIMEOUT_SECONDS,
    min_output_bytes: int = MIN_REMUX_SIZE_BYTES,
    duration_tolerance_seconds: int = DURATION_TOLERANCE_SECONDS,
    duration_tolerance_ratio: float = DURATION_TOLERANCE_RATIO,
) -> Dict[str, object]:
    output_tmp = Path(output_tmp)
    problems: List[str] = []
    if not output_tmp.exists():
        return {"status": "verification_failed", "problems": ["output_missing"]}
    size = output_tmp.stat().st_size
    if size < min_output_bytes:
        problems.append("output_too_small")

    probe_argv = _verify_probe_command(output_tmp)
    try:
        probe_result = runner(probe_argv, probe_timeout_seconds)
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "status": "verification_failed",
            "output_size": size,
            "problems": ["ffprobe_exception"],
            "error": str(error),
        }
    probe: Dict[str, object] = {}
    if probe_result.returncode != 0:
        problems.append("ffprobe_failed")
    else:
        try:
            probe = json.loads(probe_result.stdout or "{}")
        except json.JSONDecodeError:
            problems.append("ffprobe_json_invalid")
    summary = _probe_summary(probe) if probe else {
        "duration": 0.0,
        "video_streams": 0,
        "audio_streams": 0,
        "subtitle_streams": 0,
        "chapter_count": 0,
        "chapter_probe_status": "probe_unavailable",
        "streams": [],
    }
    if int(summary["video_streams"]) < 1:
        problems.append("video_stream_missing")
    actual_duration = float(summary["duration"])
    tolerance = max(float(duration_tolerance_seconds), float(expected_duration) * duration_tolerance_ratio)
    duration_difference = abs(actual_duration - float(expected_duration))
    if expected_duration <= 0 or actual_duration <= 0 or duration_difference > tolerance:
        problems.append("duration_mismatch")
    if expected_chapters > 0 and int(summary["chapter_count"]) < 1:
        problems.append("chapters_missing")

    language_checks: List[Dict[str, object]] = []
    output_by_type = {
        codec_type: [
            stream
            for stream in summary["streams"]
            if stream.get("codec_type") == codec_type
        ]
        for codec_type in ("audio", "subtitle")
    }
    for expected in expected_stream_languages or []:
        codec_type = str(expected.get("codec_type") or "")
        expected_language = _language_code(str(expected.get("language") or ""))
        try:
            type_index = int(expected.get("type_index", -1))
        except (TypeError, ValueError):
            continue
        typed_streams = output_by_type.get(codec_type, [])
        actual_language = None
        if 0 <= type_index < len(typed_streams):
            actual_language = _language_code(str(typed_streams[type_index].get("language") or ""))
        passed = bool(expected_language and actual_language == expected_language)
        language_checks.append(
            {
                "codec_type": codec_type,
                "type_index": type_index,
                "pid": expected.get("pid"),
                "expected": expected_language,
                "actual": actual_language,
                "passed": passed,
            }
        )
        if not passed:
            problems.append("stream_language_mismatch")

    read_result: Optional[subprocess.CompletedProcess] = None
    if not problems:
        read_argv = _verify_read_command(output_tmp)
        try:
            read_result = runner(read_argv, read_timeout_seconds)
        except (OSError, subprocess.SubprocessError) as error:
            problems.append("full_read_exception")
            read_result = subprocess.CompletedProcess(read_argv, 1, "", str(error))
        if read_result.returncode != 0:
            problems.append("full_read_failed")

    return {
        "status": "verification_passed" if not problems else "verification_failed",
        "output_size": size,
        "expected_duration": round(float(expected_duration), 3),
        "actual_duration": round(actual_duration, 3),
        "duration_difference_seconds": round(duration_difference, 3),
        "duration_tolerance_seconds": round(tolerance, 3),
        "expected_chapters": expected_chapters,
        "chapter_check": (
            "preserved"
            if expected_chapters > 0 and int(summary["chapter_count"]) > 0
            else "missing"
            if expected_chapters > 0
            else "not_enforced_source_ffprobe_reported_zero"
        ),
        "language_check": (
            "preserved"
            if language_checks and all(item["passed"] for item in language_checks)
            else "mismatch"
            if language_checks
            else "not_enforced_no_source_languages"
        ),
        "language_checks": language_checks,
        "probe_summary": summary,
        "ffprobe_returncode": probe_result.returncode,
        "ffprobe_stderr_tail": _tail(probe_result.stderr),
        "full_read_returncode": read_result.returncode if read_result else None,
        "full_read_stderr_tail": _tail(read_result.stderr) if read_result else "",
        "problems": list(dict.fromkeys(problems)),
    }


def _cleanup_targets(bluray_root: Path) -> List[Path]:
    targets = [_child_named(bluray_root, "BDMV"), _child_named(bluray_root, "CERTIFICATE")]
    return [target for target in targets if target and target.exists()]


def _preflight_removal(targets: Iterable[Path]) -> None:
    for target in targets:
        if not os.access(target.parent, os.W_OK | os.X_OK):
            raise PermissionError(f"No hay permisos para retirar {target.name}")
        for folder, directories, _files in os.walk(target):
            current = Path(folder)
            if not os.access(current, os.W_OK | os.X_OK):
                raise PermissionError(f"No hay permisos para limpiar {current.name}")
            for name in directories:
                child = current / name
                if not os.access(child, os.W_OK | os.X_OK):
                    raise PermissionError(f"No hay permisos para limpiar {child.name}")


def _stage_sources_for_removal(targets: Iterable[Path]) -> List[tuple[Path, Path]]:
    staged: List[tuple[Path, Path]] = []
    try:
        for target in targets:
            staged_path = target.with_name(f".{target.name}.arr-bluray-remove")
            if staged_path.exists():
                raise FileExistsError(f"Ya existe staging de limpieza: {staged_path.name}")
            target.rename(staged_path)
            staged.append((target, staged_path))
    except Exception:
        for original, staged_path in reversed(staged):
            if staged_path.exists() and not original.exists():
                staged_path.rename(original)
        raise
    return staged


def _restore_staged_sources(bluray_root: Path) -> None:
    for name in ("BDMV", "CERTIFICATE"):
        original = bluray_root / name
        staged = bluray_root / f".{name}.arr-bluray-remove"
        if staged.exists() and not original.exists():
            staged.rename(original)


def _remove_bluray_sources(
    bluray_root: Path,
    remove_tree: Callable[[Path], None] = shutil.rmtree,
) -> List[str]:
    targets = _cleanup_targets(bluray_root)
    _preflight_removal(targets)
    staged = _stage_sources_for_removal(targets)
    try:
        for _original, staged_path in staged:
            remove_tree(staged_path)
    except Exception:
        for original, staged_path in reversed(staged):
            if staged_path.exists() and not original.exists():
                staged_path.rename(original)
        raise
    remaining = [name for name in ("BDMV", "CERTIFICATE") if _child_named(bluray_root, name)]
    if remaining:
        raise RuntimeError(f"Quedan carpetas Blu-ray tras la limpieza: {', '.join(remaining)}")
    return [original.name for original, _staged in staged]


def normalize_bluray_folder(
    source: Path,
    runner: CommandRunner = _run_command,
    event_callback: Optional[EventCallback] = None,
    remove_tree: Callable[[Path], None] = shutil.rmtree,
) -> Dict[str, object]:
    source = Path(source)
    if source.exists() and source.is_dir():
        _restore_staged_sources(source)
        for child in source.iterdir():
            if child.is_dir():
                _restore_staged_sources(child)
    matches = find_full_bluray_folders(source)
    if not matches:
        return {"status": "not_bluray", "normalized": False, "source_removed": False}
    if len(matches) > 1:
        result = {
            "status": "ambiguous",
            "normalized": False,
            "source_removed": False,
            "reason": "Hay mas de una estructura Blu-ray en la entrada",
            "bluray_folders": [match.name for match in matches],
        }
        _event(event_callback, "bluray_playlist_ambiguous", "warning", result["reason"], result)
        return result

    bluray_root = matches[0]
    _event(
        event_callback,
        "bluray_detected",
        "decision",
        "Estructura Blu-ray completa detectada",
        {"source": source.name, "bluray_root": bluray_root.name, "certificate": bool(_child_named(bluray_root, "CERTIFICATE"))},
    )
    selection = select_main_playlist(bluray_root, runner=runner)
    _event(
        event_callback,
        "bluray_playlists_scanned",
        "finished",
        f"Playlists analizadas: {selection.get('playlists_scanned', 0)}",
        selection,
    )
    if selection.get("status") != "selected":
        event_name = "bluray_playlist_ambiguous" if selection.get("status") == "ambiguous" else "bluray_normalization_failed"
        result = {**selection, "normalized": False, "source_removed": False}
        _event(event_callback, event_name, "warning" if event_name.endswith("ambiguous") else "error", str(selection.get("reason") or "No hay playlist segura"), result)
        return result

    selected = dict(selection["selected"])
    stream_languages = list(selected.get("stream_languages") or [])
    _event(
        event_callback,
        "bluray_playlist_selected",
        "decision",
        f"Playlist principal seleccionada: {selected['playlist']}",
        selected,
    )
    release_name = _safe_release_name(bluray_root.name)
    output_tmp = bluray_root / f"{release_name}.arr-bluray.tmp.mkv"
    output_final = bluray_root / f"{release_name}.mkv"
    if output_final.exists():
        result = {
            "status": "finalization_failed",
            "normalized": False,
            "source_removed": False,
            "reason": "Ya existe el MKV definitivo; no se sobrescribe",
            "result_file": output_final.name,
        }
        _event(event_callback, "bluray_normalization_failed", "error", result["reason"], result)
        return result

    _event(
        event_callback,
        "bluray_remux_started",
        "started",
        "Remux Blu-ray iniciado",
        {
            "playlist_id": selected["playlist_id"],
            "temporary_file": output_tmp.name,
            "language_tags": stream_languages,
        },
    )
    remux = remux_playlist_to_mkv(
        bluray_root,
        int(selected["playlist_id"]),
        output_tmp,
        stream_languages=stream_languages,
        runner=runner,
    )
    _event(
        event_callback,
        "bluray_remux_finished",
        "finished" if remux.get("status") == "remux_finished" else "error",
        "Remux Blu-ray terminado" if remux.get("status") == "remux_finished" else "Remux Blu-ray fallido",
        remux,
    )
    if remux.get("status") != "remux_finished" or remux.get("returncode") != 0:
        _event(
            event_callback,
            "bluray_normalization_failed",
            "error",
            "Normalizacion Blu-ray detenida por fallo de remux",
            remux,
        )
        return {**remux, "normalized": False, "source_removed": False, "playlist": selected}

    _event(
        event_callback,
        "bluray_verification_started",
        "started",
        "Verificacion del MKV temporal iniciada",
        {"temporary_file": output_tmp.name},
    )
    verification = verify_bluray_remux(
        output_tmp,
        float(selected["duration"]),
        int(selected["chapter_count"]),
        expected_stream_languages=stream_languages,
        runner=runner,
    )
    verification_event = "bluray_verification_passed" if verification.get("status") == "verification_passed" else "bluray_verification_failed"
    _event(
        event_callback,
        verification_event,
        "finished" if verification_event.endswith("passed") else "error",
        "MKV temporal verificado" if verification_event.endswith("passed") else "Verificacion del MKV temporal fallida",
        verification,
    )
    if verification.get("status") != "verification_passed":
        return {
            "status": "verification_failed",
            "normalized": False,
            "source_removed": False,
            "playlist": selected,
            "remux": remux,
            "verification": verification,
            "temporary_file": output_tmp.name,
        }

    try:
        output_tmp.rename(output_final)
        if not output_final.exists() or output_final.stat().st_size != int(verification["output_size"]):
            raise RuntimeError("El MKV definitivo no coincide con el temporal verificado")
        removed = _remove_bluray_sources(bluray_root, remove_tree=remove_tree)
    except Exception as error:
        if output_final.exists() and not output_tmp.exists():
            try:
                output_final.rename(output_tmp)
            except OSError:
                pass
        result = {
            "status": "finalization_failed",
            "normalized": False,
            "source_removed": False,
            "reason": str(error),
            "temporary_file": output_tmp.name if output_tmp.exists() else "",
            "playlist": selected,
            "verification": verification,
        }
        _event(event_callback, "bluray_normalization_failed", "error", "No se pudo confirmar la normalizacion Blu-ray", result)
        return result

    _event(
        event_callback,
        "bluray_source_removed",
        "finished",
        "Origen Blu-ray retirado tras verificar el MKV",
        {"removed": removed, "result_file": output_final.name},
    )
    result = {
        "status": "normalized",
        "normalized": True,
        "source_removed": True,
        "source_path": str(source),
        "bluray_root": str(bluray_root),
        "result_file": str(output_final),
        "playlist": selected,
        "remux": remux,
        "verification": verification,
        "removed": removed,
    }
    _event(event_callback, "bluray_normalization_completed", "finished", "Normalizacion Blu-ray completada", result)
    return result
