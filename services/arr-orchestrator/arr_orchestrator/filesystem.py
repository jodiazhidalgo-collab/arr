import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .name_parser import parse_release_name


MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
TRAILER_VIDEO_EXTENSIONS = MEDIA_EXTENSIONS | {".webm"}
ARCHIVE_EXTENSIONS = {".rar", ".zip", ".7z", ".001"}
JUNK_EXTENSIONS = {".url", ".nfo", ".sfv", ".txt"}
REASON_TEXT_FILES = {
    "Pelicula repetida.txt",
    "Serie repetida.txt",
    "Error de extraccion.txt",
    "Error de FileBot.txt",
    "Revision manual.txt",
}
SIDECAR_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx"}


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
        if name.endswith(".part1.rar"):
            candidates.append(path)
        elif suffix == ".rar" and not re.search(r"\.part\d+\.rar$", name):
            candidates.append(path)
        elif suffix in {".zip", ".7z", ".001"}:
            candidates.append(path)
    return sorted(set(candidates))


def extraction_command_previews(job_root: Path, timeout_seconds: int = 7200) -> List[Dict[str, object]]:
    original_root = job_root / "original"
    archives = archive_candidates(original_root)
    extracted_root = job_root / "extracted"
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


def extract_archives(job_root: Path, timeout_seconds: int = 7200) -> Path:
    original_root = job_root / "original"
    archives = archive_candidates(original_root)
    if not archives:
        return original_root
    extracted_root = job_root / "extracted"
    extracted_root.mkdir(parents=True, exist_ok=True)
    for archive in archives:
        command = _extract_command(archive, extracted_root)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"extracción fallida ({result.returncode}) {archive.name}: "
                f"{(result.stderr or result.stdout)[-2000:]}"
            )
    if not media_files(extracted_root):
        raise RuntimeError("la extracción terminó sin producir archivos multimedia")
    return extracted_root


def _extract_command(archive: Path, extracted_root: Path) -> List[str]:
    suffix = archive.suffix.lower()
    if suffix == ".rar" or archive.name.lower().endswith(".part1.rar"):
        return ["unrar", "x", "-o+", "-idq", str(archive), str(extracted_root) + os.sep]
    return ["7z", "x", "-y", f"-o{extracted_root}", str(archive)]


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
    technical_roots = {job_root / "original", job_root / "extracted"}
    is_technical_root = any(
        source_path.resolve() == root.resolve()
        for root in technical_roots
        if root.exists()
    )
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

    children = sorted(source_path.iterdir(), key=lambda item: item.name.lower())
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
