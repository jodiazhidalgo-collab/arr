"""Preparacion determinista de lotes audiovisuales dentro del taller ARR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence

from .filesystem import MEDIA_EXTENSIONS, full_bluray_folders


BATCH_SCHEMA = "arr-batch-v1"
_AUXILIARY_WORDS = {
    "sample",
    "samples",
    "muestra",
    "muestras",
    "trailer",
    "trailers",
    "extra",
    "extras",
    "featurette",
    "featurettes",
    "makingof",
}
_MULTIPART_PATTERN = re.compile(
    r"(?i)(?:^|[\W_])(?:cd|disc|disk|part|pt)[ ._-]*\d+(?=$|[\W_])"
)


class BatchPreparationError(RuntimeError):
    pass


@dataclass
class BatchItem:
    key: str
    name: str
    sources: List[str]
    kind: str = "video"
    index: int = 0
    total: int = 0
    episode_intent: Optional[Dict[str, object]] = None
    episode_validation: str = "UNKNOWN"
    episode_reason: str = ""
    filebot_outputs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "BatchItem":
        return cls(
            key=str(payload.get("key") or ""),
            name=str(payload.get("name") or ""),
            sources=[str(value) for value in payload.get("sources") or []],
            kind=str(payload.get("kind") or "video"),
            index=int(payload.get("index") or 0),
            total=int(payload.get("total") or 0),
            episode_intent=(
                dict(payload["episode_intent"])
                if isinstance(payload.get("episode_intent"), dict)
                else None
            ),
            episode_validation=str(payload.get("episode_validation") or "UNKNOWN"),
            episode_reason=str(payload.get("episode_reason") or ""),
            filebot_outputs=[str(value) for value in payload.get("filebot_outputs") or []],
        )


@dataclass
class BatchPlan:
    category: str
    input_root: str
    items: List[BatchItem]
    discarded: List[str] = field(default_factory=list)
    removed_non_video: int = 0
    schema: str = BATCH_SCHEMA
    digest: str = ""

    @property
    def should_split(self) -> bool:
        return len(self.items) > 1

    def finalize(self) -> "BatchPlan":
        total = len(self.items)
        for index, item in enumerate(self.items, start=1):
            item.index = index
            item.total = total
        payload = {
            "schema": self.schema,
            "category": self.category,
            "items": [item.to_dict() for item in self.items],
            "discarded": list(self.discarded),
        }
        self.digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return self

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": self.schema,
            "category": self.category,
            "input_root": self.input_root,
            "items": [item.to_dict() for item in self.items],
            "discarded": list(self.discarded),
            "removed_non_video": self.removed_non_video,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "BatchPlan":
        if str(payload.get("schema") or "") != BATCH_SCHEMA:
            raise BatchPreparationError("El plan de lote usa un esquema incompatible")
        plan = cls(
            category=str(payload.get("category") or ""),
            input_root=str(payload.get("input_root") or ""),
            items=[
                BatchItem.from_dict(dict(item))
                for item in payload.get("items") or []
                if isinstance(item, dict)
            ],
            discarded=[str(value) for value in payload.get("discarded") or []],
            removed_non_video=int(payload.get("removed_non_video") or 0),
            digest=str(payload.get("digest") or ""),
        )
        expected = plan.digest
        plan.finalize()
        if expected and expected != plan.digest:
            raise BatchPreparationError("La huella del plan de lote no coincide")
        return plan


def clean_and_plan_batch(input_root: Path, category: str) -> BatchPlan:
    """Limpia una vez y devuelve las unidades fisicas que deben procesarse."""

    root = Path(input_root)
    if category not in {"movies", "tv"}:
        return BatchPlan(category, str(root), []).finalize()
    if not root.exists() or root.is_symlink() or not root.is_dir():
        raise BatchPreparationError("La entrada del lote no es un directorio fisico")

    blurays = (
        sorted(full_bluray_folders(root), key=lambda path: str(path).casefold())
        if category == "movies"
        else []
    )
    bluray_roots = [path.resolve() for path in blurays]
    videos: List[Path] = []
    discarded: List[str] = []
    removed_non_video = 0

    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        if _inside_any(path, bluray_roots):
            continue
        try:
            info = path.lstat()
        except OSError as error:
            raise BatchPreparationError(f"No se puede inspeccionar {path.name}") from error
        if stat.S_ISLNK(info.st_mode):
            path.unlink(missing_ok=True)
            discarded.append(_relative(root, path))
            removed_non_video += 1
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if path.suffix.casefold() not in MEDIA_EXTENSIONS:
            path.unlink()
            discarded.append(_relative(root, path))
            removed_non_video += 1
            continue
        if _is_auxiliary_video(root, path):
            path.unlink()
            discarded.append(_relative(root, path))
            continue
        videos.append(path)

    _remove_empty_directories(root, bluray_roots)
    items = _movie_items(root, blurays, videos) if category == "movies" else [
        _item(root, [path], "video") for path in videos
    ]
    return BatchPlan(
        category=category,
        input_root=str(root),
        items=sorted(items, key=lambda item: item.sources[0].casefold()),
        discarded=discarded,
        removed_non_video=removed_non_video,
    ).finalize()


def materialize_item(item: BatchItem, input_root: Path, child_root: Path) -> Path:
    """Mueve una unidad al taller hijo; es repetible tras un reinicio."""

    root = Path(input_root)
    original = Path(child_root) / "original"
    original.mkdir(parents=True, exist_ok=True)
    destinations: List[Path] = []
    for raw in item.sources:
        relative = _safe_relative(raw)
        source = root.joinpath(*relative.parts)
        destination = original / source.name
        source_exists = source.exists() or source.is_symlink()
        destination_exists = destination.exists() or destination.is_symlink()
        if source_exists and destination_exists:
            raise BatchPreparationError(
                f"El origen y el destino existen a la vez para {source.name}"
            )
        if source_exists:
            shutil.move(str(source), str(destination))
        elif not destination_exists:
            raise BatchPreparationError(f"No se localiza {source.name} al preparar el hijo")
        destinations.append(destination)
    _remove_empty_directories(root, [])
    if len(destinations) == 1:
        return destinations[0]
    return original


def materialize_filebot_item(
    item: BatchItem,
    output_root: Path,
    child_root: Path,
) -> Path:
    """Mueve el nombre FileBot ya calculado por el padre al taller del hijo."""

    if not item.filebot_outputs:
        raise BatchPreparationError("El elemento no contiene un mapa FileBot heredado")
    root = Path(output_root)
    destination_root = Path(child_root) / "series_filebot_output"
    destinations: List[Path] = []
    for raw in item.filebot_outputs:
        relative = _safe_relative(raw)
        source = root.joinpath(*relative.parts)
        destination = destination_root.joinpath(*relative.parts)
        source_exists = source.exists() or source.is_symlink()
        destination_exists = destination.exists() or destination.is_symlink()
        if source_exists and destination_exists:
            raise BatchPreparationError(
                f"El origen y el destino FileBot existen a la vez para {source.name}"
            )
        if source_exists:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        elif not destination_exists:
            raise BatchPreparationError(
                f"No se localiza {source.name} en el mapa FileBot del padre"
            )
        destinations.append(destination)
    _remove_empty_directories(root, [])
    return destination_root


def child_source_uid(parent_job_id: str, item: BatchItem) -> str:
    return f"batch:{parent_job_id}:{item.key}"


def _movie_items(root: Path, blurays: Sequence[Path], videos: Sequence[Path]) -> List[BatchItem]:
    items = [_item(root, [path], "bluray") for path in blurays]
    multipart: Dict[str, List[Path]] = {}
    singles: List[Path] = []
    for path in videos:
        marker = _MULTIPART_PATTERN.search(path.stem)
        if not marker:
            singles.append(path)
            continue
        key = _MULTIPART_PATTERN.sub(" ", path.stem)
        key = re.sub(r"[^a-z0-9]+", " ", key.casefold()).strip()
        multipart.setdefault(key or path.stem.casefold(), []).append(path)
    items.extend(_item(root, [path], "video") for path in singles)
    items.extend(
        _item(root, sorted(paths, key=lambda path: path.name.casefold()), "multipart")
        for paths in multipart.values()
    )
    return items


def _item(root: Path, sources: Sequence[Path], kind: str) -> BatchItem:
    relatives = [_relative(root, path) for path in sources]
    digest = hashlib.sha256(
        (kind + "\0" + "\0".join(relatives)).encode("utf-8")
    ).hexdigest()[:24]
    return BatchItem(
        key=digest,
        name=sources[0].name if len(sources) == 1 else Path(sources[0]).stem,
        sources=relatives,
        kind=kind,
    )


def _is_auxiliary_video(root: Path, path: Path) -> bool:
    relative = _relative(root, path)
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", relative.casefold())
        if token
    }
    compact = "".join(tokens)
    return bool(tokens.intersection(_AUXILIARY_WORDS) or "behindthescenes" in compact)


def _inside_any(path: Path, roots: Iterable[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(str(value or "").replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise BatchPreparationError("El plan contiene una ruta relativa no segura")
    return relative


def _remove_empty_directories(root: Path, preserved: Sequence[Path]) -> None:
    preserved_roots = [path.resolve() for path in preserved]
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        if _inside_any(directory, preserved_roots):
            continue
        try:
            directory.rmdir()
        except OSError:
            continue


__all__ = [
    "BATCH_SCHEMA",
    "BatchItem",
    "BatchPlan",
    "BatchPreparationError",
    "child_source_uid",
    "clean_and_plan_batch",
    "materialize_item",
    "materialize_filebot_item",
]
