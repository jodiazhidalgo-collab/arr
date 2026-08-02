"""Descubrimiento seguro y determinista de paquetes FileBot de series."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


EPISODE_RE = re.compile(
    r"(?i)(?<![A-Z0-9])S(?P<season>\d{1,3})(?P<body>E\d{1,3}(?:(?:[ ._-]*E|[ ._-]+)\d{1,3})*)"
)
EPISODE_NUMBER_RE = re.compile(r"(?i)E?(\d{1,3})")
SEASON_DIR_RE = re.compile(r"(?i)^(?:season|temporada)[ ._-]*\d{1,3}$")
DEFAULT_VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov",
    ".wmv", ".ts", ".m2ts", ".mts", ".webm",
}
SUBTITLE_SIDECAR_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}


class ManifestError(ValueError):
    """La raíz o una ruta física no es segura para construir el manifiesto."""


@dataclass(frozen=True)
class ManifestSidecar:
    source_relpath: str
    size: int
    mtime_ns: int
    content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManifestEntry:
    source_relpath: str
    target_relpath: str
    series_name: str
    series_key: str
    season: int
    episodes: tuple[int, ...]
    size: int
    mtime_ns: int
    source_fingerprint: str
    content_sha256: str
    subtitle_sidecars: tuple[ManifestSidecar, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["episodes"] = list(self.episodes)
        payload["subtitle_sidecars"] = [
            sidecar.to_dict() for sidecar in self.subtitle_sidecars
        ]
        return payload


@dataclass(frozen=True)
class SeriesManifest:
    status: str
    digest: str
    entries: tuple[ManifestEntry, ...]
    series_name: str | None
    series_key: str | None
    review_reasons: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "series-manifest-v1",
            "status": self.status,
            "digest": self.digest,
            "series_name": self.series_name,
            "series_key": self.series_key,
            "review_reasons": list(self.review_reasons),
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _extensions(value: Any) -> set[str]:
    if hasattr(value, "rules"):
        value = value.rules
    if isinstance(value, dict):
        value = value.get("entrada", {}).get("extensiones_video", [])
    if value is None:
        return set(DEFAULT_VIDEO_EXTENSIONS)
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise ManifestError("Las extensiones de vídeo no son válidas.")
    result = {str(item).strip().lower() for item in value if str(item).strip()}
    if not result or any(not item.startswith(".") for item in result):
        raise ManifestError("Las extensiones de vídeo no son válidas.")
    return result


def validate_relative_path(value: str) -> str:
    """Devuelve una ruta POSIX relativa o falla ante escape/traversal."""

    if not isinstance(value, str) or not value.strip():
        raise ManifestError("La ruta relativa está vacía.")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ManifestError("La ruta relativa contiene traversal.")
    if any("\x00" in part for part in path.parts):
        raise ManifestError("La ruta relativa contiene NUL.")
    return path.as_posix()


def _series_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _path_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _clean_series_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -")
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ManifestError("No se pudo obtener un nombre de serie seguro.")
    return value[:180]


def episode_cluster_numbers(value: str) -> tuple[int, ...]:
    """Lee un bloque de episodios y expande solo rangos ascendentes explícitos."""

    match = EPISODE_RE.search(value)
    body = match.group("body") if match is not None else value
    matches = list(EPISODE_NUMBER_RE.finditer(body))
    episodes: list[int] = []
    index = 0
    while index < len(matches):
        current = int(matches[index].group(1))
        if index + 1 < len(matches):
            following = int(matches[index + 1].group(1))
            separator = body[matches[index].end() : matches[index + 1].start()]
            if (
                current <= following
                and re.fullmatch(r"[ ._]*-[ ._]*", separator) is not None
            ):
                episodes.extend(range(current, following + 1))
                index += 2
                continue
        episodes.append(current)
        index += 1
    return tuple(dict.fromkeys(episodes))


def _episode_data(path: Path, relative: Path) -> tuple[str, str, int, tuple[int, ...]] | None:
    match = EPISODE_RE.search(path.stem)
    if match is None:
        return None
    season = int(match.group("season"))
    body = match.group("body")
    episodes = episode_cluster_numbers(body)
    if not episodes:
        return None

    first_part = relative.parts[0] if len(relative.parts) > 1 else ""
    if first_part and not SEASON_DIR_RE.fullmatch(first_part):
        series_name = _clean_series_name(first_part)
    else:
        series_name = _clean_series_name(path.stem[: match.start()])
    return series_name, _series_key(series_name), season, episodes


def _source_fingerprint(relative: str, stat: os.stat_result) -> str:
    data = f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _matching_sidecars(
    video: Path,
    root: Path,
    physical_files: list[Path],
) -> tuple[ManifestSidecar, ...]:
    prefix = _path_key(video.stem)
    sidecars: list[ManifestSidecar] = []
    for candidate in physical_files:
        if candidate.parent != video.parent:
            continue
        if candidate.suffix.casefold() not in SUBTITLE_SIDECAR_EXTENSIONS:
            continue
        stem = _path_key(candidate.stem)
        if stem != prefix and not stem.startswith(prefix + "."):
            continue
        relative = validate_relative_path(candidate.relative_to(root).as_posix())
        info = candidate.stat()
        sidecars.append(
            ManifestSidecar(
                source_relpath=relative,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                # Compatibilidad del documento durable. La validacion activa
                # usa tamaño y mtime, sin releer el subtitulo para hashearlo.
                content_sha256="",
            )
        )
    return tuple(
        sorted(
            sidecars,
            key=lambda item: (_path_key(item.source_relpath), item.source_relpath),
        )
    )


def _manifest_digest(entries: list[ManifestEntry], reasons: list[str]) -> str:
    payload = {
        "entries": [entry.to_dict() for entry in entries],
        "review_reasons": sorted(reasons),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _physical_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for dirname in list(dirnames):
            candidate = current_path / dirname
            if candidate.is_symlink():
                raise ManifestError(f"No se admiten enlaces simbólicos: {dirname}")
        for filename in filenames:
            candidate = current_path / filename
            if candidate.is_symlink():
                raise ManifestError(f"No se admiten enlaces simbólicos: {filename}")
            if candidate.is_file():
                files.append(candidate)
    return sorted(
        files,
        key=lambda item: (
            _path_key(item.relative_to(root).as_posix()),
            item.relative_to(root).as_posix(),
        ),
    )


def discover_manifest(
    source_root: Path | str,
    rules_or_extensions: Any = None,
) -> SeriesManifest:
    """Crea un manifiesto físico; los conflictos de contenido van a revisión."""

    lexical = Path(source_root)
    if lexical.is_symlink():
        raise ManifestError("source_root no puede ser un enlace simbólico.")
    try:
        root = lexical.resolve(strict=True)
    except OSError as error:
        raise ManifestError("source_root no existe.") from error
    if not root.is_dir():
        raise ManifestError("source_root debe ser una carpeta.")

    extensions = _extensions(rules_or_extensions)
    entries: list[ManifestEntry] = []
    reasons: list[str] = []
    target_casefold: dict[str, str] = {}
    episode_owners: dict[tuple[str, int, int], str] = {}
    canonical_series_names: dict[str, str] = {}
    classified_files: set[str] = set()

    physical_files = _physical_files(root)
    for path in physical_files:
        if path.suffix.lower() not in extensions:
            continue
        relative_path = path.relative_to(root)
        source_relative = validate_relative_path(relative_path.as_posix())
        parsed = _episode_data(path, relative_path)
        if parsed is None:
            reasons.append(f"episodio_no_reconocido:{source_relative}")
            continue
        series_name, series_key, season, episodes = parsed
        series_name = canonical_series_names.setdefault(series_key, series_name)
        filename = f"{path.stem}.mkv"
        target_relative = validate_relative_path(
            PurePosixPath(series_name, f"Season {season:02d}", filename).as_posix()
        )
        folded_target = _path_key(target_relative)
        previous_target = target_casefold.get(folded_target)
        if previous_target is not None and previous_target != source_relative:
            reasons.append(f"colision_casefold:{previous_target}:{source_relative}")
        else:
            target_casefold[folded_target] = source_relative

        for episode in episodes:
            identity = (series_key, season, episode)
            previous_owner = episode_owners.get(identity)
            if previous_owner is not None and previous_owner != source_relative:
                reasons.append(
                    f"episodio_duplicado:S{season:02d}E{episode:02d}:"
                    f"{previous_owner}:{source_relative}"
                )
            else:
                episode_owners[identity] = source_relative

        stat = path.stat()
        sidecars = _matching_sidecars(path, root, physical_files)
        sidecar_keys = [_path_key(sidecar.source_relpath) for sidecar in sidecars]
        if len(set(sidecar_keys)) != len(sidecar_keys):
            reasons.append(f"colision_sidecar_casefold:{source_relative}")
        classified_files.add(source_relative)
        classified_files.update(sidecar.source_relpath for sidecar in sidecars)
        entries.append(
            ManifestEntry(
                source_relpath=source_relative,
                target_relpath=target_relative,
                series_name=series_name,
                series_key=series_key,
                season=season,
                episodes=episodes,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                source_fingerprint=_source_fingerprint(source_relative, stat),
                # No se hace una lectura completa extra del episodio. El
                # fingerprint ligero ya congela ruta, tamaño y mtime.
                content_sha256="",
                subtitle_sidecars=sidecars,
            )
        )

    for path in physical_files:
        relative = validate_relative_path(path.relative_to(root).as_posix())
        if relative not in classified_files and path.suffix.lower() not in extensions:
            reasons.append(f"archivo_no_clasificado:{relative}")

    if not entries:
        reasons.append("sin_episodios_validos")
    series = {entry.series_key: entry.series_name for entry in entries}
    if len(series) > 1:
        reasons.append("varias_series:" + ",".join(sorted(series)))

    unique_reasons = list(dict.fromkeys(reasons))
    entries.sort(key=lambda entry: (_path_key(entry.target_relpath), entry.source_relpath))
    sole_key = next(iter(series), None) if len(series) == 1 else None
    return SeriesManifest(
        status="review" if unique_reasons else "ready",
        digest=_manifest_digest(entries, unique_reasons),
        entries=tuple(entries),
        series_name=series.get(sole_key) if sole_key else None,
        series_key=sole_key,
        review_reasons=tuple(unique_reasons),
    )


__all__ = [
    "DEFAULT_VIDEO_EXTENSIONS",
    "ManifestEntry",
    "ManifestError",
    "ManifestSidecar",
    "SeriesManifest",
    "SUBTITLE_SIDECAR_EXTENSIONS",
    "discover_manifest",
    "episode_cluster_numbers",
    "validate_relative_path",
]
