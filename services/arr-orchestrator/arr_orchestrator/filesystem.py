import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import math
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .name_parser import parse_release_name


MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
TRAILER_VIDEO_EXTENSIONS = MEDIA_EXTENSIONS | {".webm"}
ARCHIVE_EXTENSIONS = {".rar", ".zip", ".7z", ".001"}
MULTIPART_RAR_PATTERN = re.compile(r"\.part\d+\.rar$", re.IGNORECASE)
MULTIPART_RAR_FIRST_PATTERN = re.compile(r"\.part0*1\.rar$", re.IGNORECASE)
JUNK_EXTENSIONS = {".url", ".nfo", ".sfv", ".txt"}
REASON_TEXT_FILES = {
    "Pelicula repetida.txt",
    "Serie repetida.txt",
    "Error de extraccion.txt",
    "Error de FileBot.txt",
    "Revision manual.txt",
}
SIDECAR_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx"}
MAX_EXTRACTION_LAYERS = 3
EXTRACTION_LAYER_MARKER = ".arr-extraction-layer.json"
DEFAULT_EXTRACTION_TIMEOUT_SECONDS = 7200
# Un trabajo completo dispone de tres horas: conserva las dos horas históricas
# por comando sin permitir que tres capas acumulen seis horas sin control.
DEFAULT_TOTAL_EXTRACTION_TIMEOUT_SECONDS = 10800
# Un torrent normal rara vez contiene tantos paquetes independientes en una capa.
MAX_ARCHIVE_CANDIDATES_PER_LAYER = 32
# Los archivos multimedia apenas comprimen: se reserva un 25 % adicional y 512 MiB.
EXTRACTION_SPACE_MARGIN_RATIO = 1.25
EXTRACTION_SPACE_RESERVE_BYTES = 512 * 1024 * 1024


class ExtractionError(RuntimeError):
    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {
            "error_code": code,
            "error_message": message,
            "classified_reason": code,
            "extract_layer": None,
            "archive": None,
            "tool": None,
            "return_code": None,
            "output_tail": "",
            "input_root": None,
            "output_root": None,
            "duration_sec": 0.0,
            **details,
        }


def manifest(path: Path) -> Tuple[str, List[Dict[str, object]]]:
    entries: List[Dict[str, object]] = []
    if not path.exists():
        return "missing", entries
    files: Iterable[Path] = [path] if path.is_file() else path.rglob("*")
    for item in files:
        if not item.is_file():
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        relative = item.name if path.is_file() else str(item.relative_to(path))
        entries.append(
            {
                "path": relative,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    entries.sort(key=lambda entry: str(entry["path"]).lower())
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), entries


def _manifest_for_files(root: Path, files: Iterable[Path]) -> Tuple[str, List[Dict[str, object]]]:
    entries: List[Dict[str, object]] = []
    for item in sorted({path for path in files}, key=lambda path: path.name.lower()):
        if not item.exists() or not item.is_file():
            continue
        try:
            stat = item.stat()
            try:
                relative = str(item.relative_to(root))
            except ValueError:
                relative = item.name
        except OSError:
            continue
        entries.append(
            {
                "path": relative,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), entries


def top_level_item(root: Path, changed_path: Path) -> Optional[Path]:
    try:
        relative = changed_path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if not relative.parts:
        return None
    return root / relative.parts[0]


def matching_root(path: Path, roots: Iterable[Path]) -> Optional[Path]:
    for root in roots:
        try:
            path.resolve().relative_to(root.resolve())
            return root
        except (OSError, ValueError):
            continue
    return None


def unique_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination
    stamp = time.strftime("%Y%m%d_%H%M%S")
    if destination.suffix:
        return destination.with_name(f"{destination.stem}__{stamp}{destination.suffix}")
    return destination.with_name(f"{destination.name}__{stamp}")


def safe_folder_name(value: str) -> str:
    text = re.sub(r"[\\/]+", " ", value or "").strip()
    text = re.sub(r"[\x00-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:180] or "item"


def numbered_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination
    for index in range(1, 10000):
        candidate = destination.with_name(f"{destination.name} ({index})")
        if not candidate.exists():
            return candidate
    return destination.with_name(f"{destination.name} ({int(time.time())})")


def move_into_job(source: Path, workshop_root: Path, job_id: str) -> Path:
    job_root = workshop_root / job_id
    original_root = job_root / "original"
    original_root.mkdir(parents=True, exist_ok=True)
    destination = unique_destination(original_root / source.name)
    shutil.move(str(source), str(destination))
    return job_root


def trailer_package_files(source: Path) -> Optional[Tuple[Path, Path, Path]]:
    if not source.exists():
        return None
    package = source if source.is_dir() else source.parent
    metas = [source] if source.is_file() and source.suffix.lower() == ".json" else sorted(package.glob("*.json"))
    for meta_path in metas:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        wanted = str(meta.get("video_file") or "").strip()
        if not wanted:
            continue
        video = package / wanted
        if video.exists() and video.is_file() and video.suffix.lower() in TRAILER_VIDEO_EXTENSIONS:
            return package, meta_path, video
    return None


def trailer_ready_source(item: Path) -> Optional[Path]:
    if not item.exists():
        return None
    if item.is_file() and item.suffix.lower() != ".json":
        return None
    package = trailer_package_files(item)
    if not package:
        return None
    return item


def trailer_package_manifest(source: Path) -> Tuple[str, List[Dict[str, object]]]:
    package = trailer_package_files(source)
    if not package:
        return "missing", []
    package_root, meta_path, video = package
    return _manifest_for_files(package_root, [meta_path, video])


def move_trailer_package_into_job(source: Path, workshop_root: Path, job_id: str) -> Tuple[Path, Path]:
    package = trailer_package_files(source)
    if not package:
        raise FileNotFoundError(f"paquete de trailer incompleto: {source}")
    package_root, meta_path, video = package
    job_root = workshop_root / job_id
    original_root = job_root / "original"
    original_root.mkdir(parents=True, exist_ok=True)

    if source.is_dir() and source.resolve() == package_root.resolve():
        destination = unique_destination(original_root / source.name)
        shutil.move(str(source), str(destination))
        return job_root, destination

    destination = unique_destination(original_root / safe_folder_name(meta_path.stem))
    destination.mkdir(parents=True, exist_ok=True)
    shutil.move(str(video), str(destination / video.name))
    shutil.move(str(meta_path), str(destination / meta_path.name))
    return job_root, destination


def archive_candidates(root: Path) -> List[Path]:
    candidates: List[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        suffix = path.suffix.lower()
        if MULTIPART_RAR_FIRST_PATTERN.search(name):
            candidates.append(path)
        elif suffix == ".rar" and not MULTIPART_RAR_PATTERN.search(name):
            candidates.append(path)
        elif suffix in {".zip", ".7z", ".001"}:
            candidates.append(path)
    return sorted(set(candidates))


def extraction_command_previews(
    job_root: Path,
    timeout_seconds: int = DEFAULT_EXTRACTION_TIMEOUT_SECONDS,
) -> List[Dict[str, object]]:
    original_root = job_root / "original"
    archives = archive_candidates(original_root)
    extracted_root = job_root / "extracted" / "layer_01.tmp"
    return [
        {
            "argv": _extract_command(archive, extracted_root),
            "archive": str(archive),
            "output_root": str(extracted_root),
            "cwd": str(job_root),
            "timeout_sec": timeout_seconds,
        }
        for archive in archives
    ]


def extract_archives(
    job_root: Path,
    timeout_seconds: int = DEFAULT_EXTRACTION_TIMEOUT_SECONDS,
    total_timeout_seconds: int = DEFAULT_TOTAL_EXTRACTION_TIMEOUT_SECONDS,
    event_callback: Optional[Callable[[str, str, Dict[str, object]], None]] = None,
) -> Path:
    original_root = job_root / "original"
    extracted_root = job_root / "extracted"
    extracted_root.mkdir(parents=True, exist_ok=True)
    _cleanup_incomplete_layers(extracted_root)

    started_at = time.monotonic()
    scan_root = original_root
    for layer in range(1, MAX_EXTRACTION_LAYERS + 1):
        if media_files(scan_root):
            return scan_root
        archives = archive_candidates(scan_root)
        if not archives:
            raise ExtractionError(
                "extract_no_media",
                "la extracción terminó sin producir archivos multimedia",
                extract_layer=layer,
                input_root=str(scan_root),
                duration_sec=round(time.monotonic() - started_at, 3),
            )
        if len(archives) > MAX_ARCHIVE_CANDIDATES_PER_LAYER:
            raise ExtractionError(
                "extract_too_many_candidates",
                f"la capa contiene {len(archives)} iniciadores; máximo permitido: "
                f"{MAX_ARCHIVE_CANDIDATES_PER_LAYER}",
                extract_layer=layer,
                candidate_count=len(archives),
                candidate_limit=MAX_ARCHIVE_CANDIDATES_PER_LAYER,
                input_root=str(scan_root),
                duration_sec=round(time.monotonic() - started_at, 3),
            )

        layer_root = extracted_root / f"layer_{layer:02d}"
        layer_tmp = extracted_root / f"layer_{layer:02d}.tmp"
        signature = _layer_input_signature(scan_root, archives)
        input_size = sum(int(item["size"]) for item in signature["input_files"])
        required_space = math.ceil(input_size * EXTRACTION_SPACE_MARGIN_RATIO) + EXTRACTION_SPACE_RESERVE_BYTES
        free_space = shutil.disk_usage(extracted_root).free
        reused = _layer_marker_matches(layer_root, layer, signature)
        layer_started_at = time.monotonic()
        if event_callback:
            preview_outputs = [
                layer_tmp
                if len(archives) == 1
                else layer_tmp / _archive_output_key(scan_root, archive)
                for archive in archives
            ]
            previews = [
                {
                    "argv": _sanitized_command_preview(
                        job_root,
                        _extract_command(archive, output_root),
                    ),
                    "archive": _job_path_alias(job_root, archive),
                    "output_root": _job_path_alias(job_root, output_root),
                }
                for archive, output_root in zip(archives, preview_outputs)
            ]
            tools = sorted({str(preview["argv"][0]) for preview in previews})
            total_remaining = max(0.0, total_timeout_seconds - (time.monotonic() - started_at))
            event_callback(
                "command",
                f"Capa de extracción {layer} preparada",
                {
                    "extract_layer": layer,
                    "scan_root": _job_path_alias(job_root, scan_root),
                    "selected_archives": [
                        _job_path_alias(job_root, archive) for archive in archives
                    ],
                    "tool": tools[0] if len(tools) == 1 else "multiple",
                    "command_preview": previews,
                    "cwd": "<JOB_ROOT>",
                    "timeout_sec": round(min(float(timeout_seconds), total_remaining), 3),
                    "total_budget_remaining_sec": round(total_remaining, 3),
                    "available_space_bytes": free_space,
                    "required_space_bytes": required_space,
                    "input_size_bytes": input_size,
                    "reused": reused,
                },
            )
        if not reused and free_space < required_space:
            raise ExtractionError(
                "extract_no_space",
                "no existe espacio libre suficiente para iniciar la capa de extracción",
                extract_layer=layer,
                input_size_bytes=input_size,
                required_space_bytes=required_space,
                available_space_bytes=free_space,
                input_root=str(scan_root),
                output_root=str(layer_tmp),
                duration_sec=round(time.monotonic() - started_at, 3),
            )
        if not reused:
            _invalidate_layers_from(extracted_root, layer)
            layer_tmp.mkdir(parents=True, exist_ok=True)
            for archive in archives:
                output_root = layer_tmp
                if len(archives) > 1:
                    output_root = layer_tmp / _archive_output_key(scan_root, archive)
                    output_root.mkdir(parents=True, exist_ok=True)
                command = _extract_command(archive, output_root)
                elapsed = time.monotonic() - started_at
                remaining_total = total_timeout_seconds - elapsed
                if remaining_total <= 0:
                    raise ExtractionError(
                        "extract_timeout",
                        "se agotó el presupuesto total de extracción",
                        extract_layer=layer,
                        archive=str(archive),
                        tool=command[0],
                        timeout_scope="total",
                        total_timeout_sec=total_timeout_seconds,
                        duration_sec=round(elapsed, 3),
                        input_root=str(scan_root),
                        output_root=str(output_root),
                    )
                command_timeout = min(float(timeout_seconds), remaining_total)
                command_started = time.monotonic()
                try:
                    result = subprocess.run(
                        command,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=command_timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    duration = time.monotonic() - command_started
                    output_tail = _timeout_output_tail(error)
                    raise ExtractionError(
                        "extract_timeout",
                        f"la herramienta superó el timeout de {round(command_timeout, 3)} segundos",
                        extract_layer=layer,
                        archive=str(archive),
                        tool=command[0],
                        timeout_scope="command",
                        timeout_sec=round(command_timeout, 3),
                        duration_sec=round(duration, 3),
                        output_tail=output_tail,
                        input_root=str(scan_root),
                        output_root=str(output_root),
                    ) from error
                except OSError as error:
                    duration = time.monotonic() - command_started
                    raise ExtractionError(
                        "extract_tool_failed",
                        f"no se pudo ejecutar {command[0]}: {error}",
                        extract_layer=layer,
                        archive=str(archive),
                        tool=command[0],
                        return_code=None,
                        output_tail=str(error)[-2000:],
                        input_root=str(scan_root),
                        output_root=str(output_root),
                        duration_sec=round(duration, 3),
                    ) from error
                if result.returncode != 0:
                    duration = time.monotonic() - command_started
                    output_tail = _combined_output_tail(result.stdout, result.stderr)
                    error_code = _classify_extraction_failure(command[0], result.returncode, output_tail)
                    raise ExtractionError(
                        error_code,
                        f"extracción fallida ({result.returncode}) {archive.name}",
                        extract_layer=layer,
                        archive=str(archive),
                        tool=command[0],
                        return_code=result.returncode,
                        output_tail=output_tail,
                        input_root=str(scan_root),
                        output_root=str(output_root),
                        duration_sec=round(duration, 3),
                    )

            marker = {
                "schema": "arr-extraction-layer-v1",
                "extract_layer": layer,
                "selected_archives": [str(path.relative_to(scan_root)) for path in archives],
                "input_files": signature["input_files"],
                "input_fingerprint": signature["input_fingerprint"],
                "tools": sorted({_extract_command(path, layer_tmp)[0] for path in archives}),
                "result": "complete",
            }
            (layer_tmp / EXTRACTION_LAYER_MARKER).write_text(
                json.dumps(marker, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            layer_tmp.replace(layer_root)

        produced_media = media_files(layer_root)
        nested_archives = archive_candidates(layer_root)
        if event_callback:
            event_callback(
                "finished",
                f"Capa de extracción {layer} terminada",
                {
                    "extract_layer": layer,
                    "reused": reused,
                    "produced_files": _file_count(layer_root),
                    "produced_media": len(produced_media),
                    "new_archives": len(nested_archives),
                    "output_root": _job_path_alias(job_root, layer_root),
                    "duration_sec": round(time.monotonic() - layer_started_at, 3),
                    "return_code": 0,
                    "decision": "finish" if produced_media else "continue" if nested_archives else "fail",
                },
            )
        if produced_media:
            return layer_root
        if not nested_archives:
            raise ExtractionError(
                "extract_no_media",
                "la extracción terminó sin producir archivos multimedia",
                extract_layer=layer,
                input_root=str(scan_root),
                output_root=str(layer_root),
                duration_sec=round(time.monotonic() - started_at, 3),
            )
        if layer == MAX_EXTRACTION_LAYERS:
            raise ExtractionError(
                "extract_depth_limit",
                f"se alcanzó el límite de {MAX_EXTRACTION_LAYERS} capas de extracción",
                extract_layer=layer,
                input_root=str(scan_root),
                output_root=str(layer_root),
                new_archives=len(nested_archives),
                duration_sec=round(time.monotonic() - started_at, 3),
            )
        scan_root = layer_root

    raise ExtractionError(
        "extract_depth_limit",
        f"se alcanzó el límite de {MAX_EXTRACTION_LAYERS} capas de extracción",
        extract_layer=MAX_EXTRACTION_LAYERS,
        duration_sec=round(time.monotonic() - started_at, 3),
    )


def _cleanup_incomplete_layers(extracted_root: Path) -> None:
    for layer in range(1, MAX_EXTRACTION_LAYERS + 1):
        temporary = extracted_root / f"layer_{layer:02d}.tmp"
        if temporary.exists():
            shutil.rmtree(temporary)


def _invalidate_layers_from(extracted_root: Path, first_layer: int) -> None:
    for layer in range(first_layer, MAX_EXTRACTION_LAYERS + 1):
        for path in (
            extracted_root / f"layer_{layer:02d}",
            extracted_root / f"layer_{layer:02d}.tmp",
        ):
            if path.exists():
                shutil.rmtree(path)


def _layer_input_signature(scan_root: Path, archives: List[Path]) -> Dict[str, object]:
    input_files: List[Dict[str, object]] = []
    all_files = sorted(
        {member for archive in archives for member in _archive_set_members(archive)},
        key=lambda path: str(path).casefold(),
    )
    for path in all_files:
        stat = path.stat()
        input_files.append(
            {
                "path": str(path.relative_to(scan_root)),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    encoded = json.dumps(input_files, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {
        "input_files": input_files,
        "input_fingerprint": hashlib.sha256(encoded).hexdigest(),
    }


def _archive_set_members(archive: Path) -> List[Path]:
    name = archive.name
    multipart = re.match(r"^(?P<prefix>.+\.part)(?P<number>\d+)(?P<suffix>\.rar)$", name, re.IGNORECASE)
    if multipart:
        prefix = multipart.group("prefix").casefold()
        suffix = multipart.group("suffix").casefold()
        return sorted(
            path
            for path in archive.parent.iterdir()
            if path.is_file()
            and (match := re.match(r"^(?P<prefix>.+\.part)(?P<number>\d+)(?P<suffix>\.rar)$", path.name, re.IGNORECASE))
            and match.group("prefix").casefold() == prefix
            and match.group("suffix").casefold() == suffix
        )
    if archive.suffix.casefold() == ".rar":
        stem = archive.stem.casefold()
        members = [archive]
        members.extend(
            path
            for path in archive.parent.iterdir()
            if path.is_file() and re.fullmatch(re.escape(stem) + r"\.r\d+", path.name.casefold())
        )
        return sorted(set(members), key=lambda path: path.name.casefold())
    if archive.suffix.casefold() == ".001":
        prefix = archive.name[:-3].casefold()
        return sorted(
            path
            for path in archive.parent.iterdir()
            if path.is_file() and re.fullmatch(re.escape(prefix) + r"\d{3}", path.name.casefold())
        )
    return [archive]


def _layer_marker_matches(layer_root: Path, layer: int, signature: Dict[str, object]) -> bool:
    marker_path = layer_root / EXTRACTION_LAYER_MARKER
    if not marker_path.is_file():
        return False
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        marker.get("result") == "complete"
        and marker.get("extract_layer") == layer
        and marker.get("input_fingerprint") == signature["input_fingerprint"]
    )


def _archive_output_key(scan_root: Path, archive: Path) -> str:
    relative = str(archive.relative_to(scan_root)).replace("\\", "/")
    digest = hashlib.sha256(relative.casefold().encode("utf-8")).hexdigest()[:10]
    return f"{safe_folder_name(archive.stem)[:80]}__{digest}"


def _file_count(root: Path) -> int:
    return sum(1 for path in root.rglob("*") if path.is_file() and path.name != EXTRACTION_LAYER_MARKER)


def _job_path_alias(job_root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(job_root.resolve())
    except (OSError, ValueError):
        return path.name
    suffix = relative.as_posix()
    return "<JOB_ROOT>" if not suffix else f"<JOB_ROOT>/{suffix}"


def _sanitized_command_preview(job_root: Path, command: List[str]) -> List[str]:
    root_text = str(job_root)
    return [str(value).replace(root_text, "<JOB_ROOT>").replace("\\", "/") for value in command]


def _extract_command(archive: Path, extracted_root: Path) -> List[str]:
    suffix = archive.suffix.lower()
    if suffix == ".rar":
        return [
            "unrar",
            "x",
            "-o+",
            "-idq",
            "-p-",
            "-y",
            str(archive),
            str(extracted_root) + os.sep,
        ]
    return ["7z", "x", "-y", "-p-", f"-o{extracted_root}", str(archive)]


def _timeout_output_tail(error: subprocess.TimeoutExpired) -> str:
    output = error.stderr or error.stdout or ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return str(output)[-2000:]


def _combined_output_tail(stdout: object, stderr: object) -> str:
    parts: List[str] = []
    for value in (stdout, stderr):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value:
            parts.append(str(value))
    return "\n".join(parts)[-2000:]


def _classify_extraction_failure(tool: str, return_code: int, output: str) -> str:
    text = output.casefold()
    password_patterns = (
        "wrong password",
        "incorrect password",
        "password is incorrect",
        "enter password",
        "password required",
        "encrypted file",
        "encrypted archive",
        "can not open encrypted",
        "cannot open encrypted",
    )
    volume_patterns = (
        "cannot find volume",
        "can't find volume",
        "missing volume",
        "next volume is required",
        "insert disk with",
        "you need to start extraction from a previous volume",
    )
    corrupt_patterns = (
        "checksum error",
        "crc failed",
        "crc error",
        "data error",
        "unexpected end of archive",
        "unexpected end of data",
        "archive is corrupt",
        "corrupt archive",
        "broken archive",
        "headers error",
        "bad archive",
    )
    if any(pattern in text for pattern in password_patterns):
        return "extract_password_required"
    if any(pattern in text for pattern in volume_patterns):
        return "extract_volume_missing"
    if any(pattern in text for pattern in corrupt_patterns):
        return "extract_archive_corrupt"
    if tool == "unrar" and return_code == 3:
        return "extract_archive_corrupt"
    return "extract_tool_failed"


def media_files(root: Path) -> List[Path]:
    if not root.exists():
        return []
    files = [root] if root.is_file() else root.rglob("*")
    return [
        path
        for path in files
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    ]


def _child_named(folder: Path, name: str) -> Optional[Path]:
    if not folder.exists() or not folder.is_dir():
        return None
    wanted = name.casefold()
    try:
        return next(
            (child for child in folder.iterdir() if child.name.casefold() == wanted),
            None,
        )
    except OSError:
        return None


def is_full_bluray_folder(path: Path) -> bool:
    bdmv = _child_named(path, "BDMV")
    playlist = _child_named(bdmv, "PLAYLIST") if bdmv else None
    return bool(
        playlist
        and playlist.is_dir()
        and any(
            item.is_file() and item.suffix.casefold() == ".mpls"
            for item in playlist.iterdir()
        )
    )


def full_bluray_folders(path: Path) -> List[Path]:
    if not path.exists() or not path.is_dir():
        return []
    matches = [path] if is_full_bluray_folder(path) else []
    matches.extend(
        child
        for child in sorted(path.iterdir(), key=lambda item: item.name.casefold())
        if child.is_dir() and is_full_bluray_folder(child)
    )
    return matches


def _semantic_name_for_filebot(source_path: Path, job_name: str, media: List[Path]) -> str:
    if job_name:
        candidate = Path(job_name).stem if Path(job_name).suffix else job_name
        if candidate and candidate.lower() not in {"original", "extracted"}:
            return safe_folder_name(candidate)
    if len(media) == 1:
        return safe_folder_name(media[0].stem)
    if source_path.name.lower() not in {"original", "extracted"}:
        return safe_folder_name(source_path.name)
    return "item"


def prepare_filebot_input(source_path: Path, job_root: Path, job_name: str) -> Path:
    is_technical_root = _is_filebot_technical_root(source_path, job_root)
    if source_path.is_file():
        media = media_files(source_path)
        destination_root = unique_destination(
            job_root / "filebot_input" / _semantic_name_for_filebot(source_path, job_name, media)
        )
        destination_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(unique_destination(destination_root / source_path.name)))
        return destination_root
    if not is_technical_root:
        return source_path

    if is_full_bluray_folder(source_path):
        media = media_files(source_path)
        destination_root = unique_destination(
            job_root / "filebot_input" / _semantic_name_for_filebot(source_path, job_name, media)
        )
        destination_root.mkdir(parents=True, exist_ok=True)
        for item in sorted(source_path.iterdir(), key=lambda child: child.name.lower()):
            shutil.move(str(item), str(unique_destination(destination_root / item.name)))
        return destination_root

    children = sorted(
        (item for item in source_path.iterdir() if item.name != EXTRACTION_LAYER_MARKER),
        key=lambda item: item.name.lower(),
    )
    media = media_files(source_path)
    media_dirs = [
        item
        for item in children
        if item.is_dir() and media_files(item)
    ]
    loose_media = [
        item
        for item in children
        if item.is_file() and item.suffix.lower() in MEDIA_EXTENSIONS
    ]
    if len(media_dirs) == 1 and not loose_media:
        return media_dirs[0]

    destination_root = unique_destination(
        job_root / "filebot_input" / _semantic_name_for_filebot(source_path, job_name, media)
    )
    destination_root.mkdir(parents=True, exist_ok=True)
    for item in children:
        shutil.move(str(item), str(unique_destination(destination_root / item.name)))
    return destination_root


def _is_filebot_technical_root(source_path: Path, job_root: Path) -> bool:
    if not source_path.exists():
        return False
    resolved = source_path.resolve()
    original_root = (job_root / "original").resolve()
    extracted_root = (job_root / "extracted").resolve()
    if resolved in {original_root, extracted_root}:
        return True
    try:
        relative = resolved.relative_to(extracted_root)
    except ValueError:
        return False
    return bool(
        len(relative.parts) == 1
        and re.fullmatch(r"layer_0[1-3]", relative.parts[0], re.IGNORECASE)
    )


def media_worker_source(root: Path) -> Path:
    children = sorted(root.iterdir(), key=lambda item: item.name.lower())
    if len(children) == 1:
        return children[0]
    media = media_files(root)
    if len(media) == 1:
        return media[0].parent
    media_dirs = sorted(
        {
            item
            for item in children
            if item.is_dir() and media_files(item)
        },
        key=lambda item: item.name.lower(),
    )
    if len(media_dirs) == 1:
        return media_dirs[0]
    return root


def clean_junk(root: Path) -> None:
    if not root.exists() or root.is_file():
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in JUNK_EXTENSIONS:
            try:
                path.unlink()
            except OSError:
                pass


def move_job_to(job_root: Path, destination_root: Path, name: str) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = numbered_destination(destination_root / safe_folder_name(name))
    shutil.move(str(job_root), str(destination))
    return destination


def move_job_to_review_clean(job_root: Path, destination_root: Path, name: str) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = numbered_destination(destination_root / safe_folder_name(name))
    destination.mkdir(parents=True, exist_ok=True)

    source_root = _review_content_root(job_root)
    moved = False
    if source_root.exists() and source_root.is_file():
        shutil.move(str(source_root), str(unique_destination(destination / source_root.name)))
        moved = True
    elif source_root.exists():
        for item in sorted(source_root.iterdir(), key=lambda child: child.name.lower()):
            shutil.move(str(item), str(unique_destination(destination / item.name)))
            moved = True

    if not moved and job_root.exists():
        for item in sorted(job_root.iterdir(), key=lambda child: child.name.lower()):
            if item.name in {"original", "extracted", "filebot_input", "filebot_output"}:
                continue
            shutil.move(str(item), str(unique_destination(destination / item.name)))

    shutil.rmtree(job_root, ignore_errors=True)
    return destination


def move_extraction_failure_to_review(
    job_root: Path,
    destination_root: Path,
    name: str,
) -> Tuple[Path, Dict[str, object]]:
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = numbered_destination(destination_root / safe_folder_name(name))
    destination.mkdir(parents=True, exist_ok=True)

    preservation_errors: List[str] = []
    preserved: Dict[str, bool] = {"original": False, "extracted": False}
    for folder_name in ("original", "extracted"):
        source = job_root / folder_name
        if not source.exists():
            continue
        try:
            shutil.move(str(source), str(destination / folder_name))
            preserved[folder_name] = True
        except OSError as error:
            detail = error.strerror or error.__class__.__name__
            preservation_errors.append(f"{folder_name}: {detail}")

    preserved_total_bytes = _tree_size(destination / "original") + _tree_size(destination / "extracted")
    residual_job_root = job_root.exists() and any(job_root.iterdir())
    if job_root.exists() and not residual_job_root:
        job_root.rmdir()
    return destination, {
        "preserved_original": preserved["original"],
        "preserved_extracted": preserved["extracted"],
        "preserved_total_bytes": preserved_total_bytes,
        "preservation_errors": preservation_errors,
        "residual_job_root": str(job_root) if residual_job_root else None,
    }


def _tree_size(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    files = [root] if root.is_file() else root.rglob("*")
    for path in files:
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _review_content_root(job_root: Path) -> Path:
    for root in (
        job_root / "filebot_input",
        job_root / "extracted",
        job_root / "original",
    ):
        if not root.exists():
            continue
        root = _collapse_single_review_folder(root)
        if _has_review_content(root):
            return root
    return job_root


def _collapse_single_review_folder(root: Path) -> Path:
    if not root.exists() or root.is_file():
        return root
    children = sorted(root.iterdir(), key=lambda item: item.name.lower())
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return root


def _has_review_content(root: Path) -> bool:
    if not root.exists():
        return False
    if root.is_file():
        return True
    return any(root.iterdir())


def move_tv_job_to_review(job_root: Path, destination_root: Path, name: str) -> Path:
    original_root = job_root / "original"
    source_root = original_root if original_root.exists() else job_root
    videos = media_files(source_root)
    if not videos:
        return move_job_to(job_root, destination_root, name)

    parsed = parse_release_name(name, "tv")
    title = safe_folder_name(parsed.display_title or Path(name).stem or name)
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = numbered_destination(destination_root / title)
    destination.mkdir(parents=True, exist_ok=True)

    season_dir = _tv_review_season_dir(destination, parsed.season or parsed.season_pack)
    used_names: set[str] = set()
    for index, video in enumerate(sorted(videos, key=lambda path: str(path).lower()), start=1):
        target_stem = _tv_review_stem(parsed, video, index)
        target = unique_destination(season_dir / f"{target_stem}{video.suffix}")
        shutil.move(str(video), str(target))
        used_names.add(video.name)
        for sidecar in _related_sidecars(video):
            if not sidecar.exists() or sidecar.name in used_names:
                continue
            shutil.move(str(sidecar), str(unique_destination(season_dir / f"{target_stem}{sidecar.suffix}")))
            used_names.add(sidecar.name)

    shutil.rmtree(job_root, ignore_errors=True)
    return destination


def _tv_review_season_dir(destination: Path, season: Optional[int]) -> Path:
    if season is None:
        return destination
    season_dir = destination / f"Season {int(season):02d}"
    season_dir.mkdir(parents=True, exist_ok=True)
    return season_dir


def _tv_review_stem(parsed, video: Path, index: int) -> str:
    title = safe_folder_name(parsed.display_title or video.stem)
    season = parsed.season
    episodes = list(parsed.episodes or [])
    episode = episodes[min(index - 1, len(episodes) - 1)] if episodes else None
    if season is not None and episode is not None:
        return safe_folder_name(f"{title} - S{int(season):02d}E{int(episode):02d}")
    if season is not None:
        return safe_folder_name(f"{title} - {video.stem}")
    if parsed.absolute_episode is not None:
        return safe_folder_name(f"{title} - E{int(parsed.absolute_episode):02d}")
    return safe_folder_name(video.stem)


def _related_sidecars(video: Path) -> List[Path]:
    return sorted(
        path
        for path in video.parent.glob(f"{video.stem}.*")
        if path.is_file() and path != video and path.suffix.lower() in SIDECAR_EXTENSIONS
    )


def write_reason(
    destination: Path,
    payload: Dict[str, object],
    reason_filename: Optional[str] = None,
    reason_lines: Optional[List[str]] = None,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "reason.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    if not reason_filename:
        return
    for old_reason in REASON_TEXT_FILES:
        try:
            (destination / old_reason).unlink(missing_ok=True)
        except OSError:
            pass
    lines = [reason_filename.removesuffix(".txt")]
    lines.extend(str(line) for line in (reason_lines or []) if str(line).strip())
    (destination / reason_filename).write_text(
        "\n".join(lines).strip() + "\n",
        encoding="utf-8",
    )
