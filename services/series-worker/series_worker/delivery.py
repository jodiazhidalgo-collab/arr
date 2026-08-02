"""Publicacion directa de episodios desde el taller a la biblioteca TV.

La limpieza audiovisual termina dentro de ``taller/<job_id>/series_work``.
Despues se mueve cada MKV/SRT verificado a ``Serie/Season`` igual que hace el
flujo de peliculas: sin sombras de la serie, marcadores ni copias completas de
la biblioteca.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .journal import DurableJournal


# Se conserva exportado para poder leer resultados antiguos, pero el flujo
# directo no crea ni consulta este archivo.
MARKER_NAME = ".series-worker-generation.json"


class DeliveryError(RuntimeError):
    code = "delivery_failed"


class AtomicDeliveryUnsupported(DeliveryError):
    """Compatibilidad con consumidores antiguos; ya no se exige intercambio."""

    code = "atomic_exchange_unsupported"


class DeliveryConflict(DeliveryError):
    code = "delivery_conflict"


class RecoveryAmbiguous(DeliveryError):
    code = "recovery_ambiguous"


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _require_plain_directory(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    info = resolved.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DeliveryError(f"{label} no es una carpeta fisica")
    return resolved


def _canonical_paths(
    prepared_series_root: Path | str,
    final_series_root: Path | str,
    *,
    prepared_may_be_missing: bool = False,
) -> tuple[Path, Path]:
    prepared_input = Path(prepared_series_root)
    if prepared_input.is_symlink():
        raise DeliveryError("prepared_series_root no puede ser un enlace simbolico")
    if prepared_input.exists():
        prepared = _require_plain_directory(prepared_input, "prepared_series_root")
    elif prepared_may_be_missing:
        prepared_parent = _require_plain_directory(
            prepared_input.parent,
            "padre de prepared_series_root",
        )
        prepared = prepared_parent / prepared_input.name
    else:
        raise DeliveryError("prepared_series_root no existe")

    final_input = Path(final_series_root)
    if final_input.name in {"", ".", ".."}:
        raise DeliveryError("final_series_root no tiene un nombre valido")
    final_parent = _require_plain_directory(final_input.parent, "padre de final_series_root")
    final = final_parent / final_input.name
    if final.exists() and (final.is_symlink() or not final.is_dir()):
        raise DeliveryConflict("La raiz final de la serie no es una carpeta normal")
    try:
        prepared.relative_to(final)
    except ValueError:
        pass
    else:
        raise DeliveryError("El taller no puede estar dentro de la serie final")
    try:
        final.relative_to(prepared)
    except ValueError:
        pass
    else:
        raise DeliveryError("La serie final no puede estar dentro del taller")
    return prepared, final


def _normalize_expected_files(value: Iterable[Path | str] | None) -> tuple[str, ...]:
    if value is None or isinstance(value, (str, bytes, os.PathLike)):
        raise DeliveryError("expected_files debe contener las salidas procesadas")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in value:
        relative = PurePosixPath(os.fspath(raw).replace("\\", "/"))
        if (
            relative.is_absolute()
            or relative == PurePosixPath(".")
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.name == MARKER_NAME
        ):
            raise DeliveryError(f"Ruta de publicacion no segura: {raw!r}")
        text = relative.as_posix()
        folded = text.casefold()
        if folded in seen:
            raise DeliveryError(f"Ruta de publicacion duplicada: {text}")
        seen.add(folded)
        normalized.append(text)
    if not normalized:
        raise DeliveryError("No hay archivos procesados para publicar")
    return tuple(sorted(normalized, key=lambda item: (item.casefold(), item)))


def _source_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink():
        raise DeliveryError(f"La salida procesada es un enlace: {relative}")
    return path


def _destination_path(root: Path, relative: str) -> Path:
    return root.joinpath(*PurePosixPath(relative).parts)


def _check_before_move(prepared: Path, final: Path, expected: tuple[str, ...]) -> None:
    for relative in expected:
        source = _source_path(prepared, relative)
        destination = _destination_path(final, relative)
        if not source.is_file() or source.stat().st_size <= 0:
            raise DeliveryError(f"Falta la salida procesada: {relative}")
        if _path_exists(destination):
            raise DeliveryConflict(f"Ya existe el episodio final: {relative}")


def _move_one(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.is_symlink():
        raise DeliveryConflict("La temporada final no puede ser un enlace simbolico")
    if _path_exists(destination):
        raise DeliveryConflict(f"Ya existe el destino final: {destination.name}")
    try:
        source.rename(destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        # Docker publica taller y biblioteca como bind mounts distintos. En
        # ese salto, igual que películas, se copia una vez y se borra origen.
        shutil.move(str(source), str(destination))


def _remove_empty_tree(root: Path) -> None:
    if not root.exists():
        return
    for current, directories, files in os.walk(root, topdown=False):
        if files:
            continue
        path = Path(current)
        try:
            path.rmdir()
        except OSError:
            pass


def _move_expected(
    prepared: Path,
    final: Path,
    expected: tuple[str, ...],
    *,
    recovery: bool,
) -> None:
    final.mkdir(parents=True, exist_ok=True)
    for relative in expected:
        source = _source_path(prepared, relative)
        destination = _destination_path(final, relative)
        source_exists = source.is_file()
        destination_exists = destination.is_file() and not destination.is_symlink()
        if source_exists and not destination_exists:
            _move_one(source, destination)
            continue
        if recovery and not source_exists and destination_exists:
            continue
        if source_exists and destination_exists:
            raise RecoveryAmbiguous(f"Origen y destino existen a la vez: {relative}")
        raise RecoveryAmbiguous(f"No existe ni origen ni destino: {relative}")
    _remove_empty_tree(prepared)


def _result(job_id: str, generation: str, final: Path, *, recovered: bool) -> dict[str, Any]:
    return {
        "status": "committed",
        "job_id": job_id,
        "generation": generation,
        "mode": "direct_move",
        "final_series_root": str(final),
        "recovered": recovered,
        "cleanup_pending": [],
        "marker_retired": True,
    }


def preflight_atomic_exchange(root: Path | str) -> dict[str, Any]:
    """Compatibilidad: valida la biblioteca sin crear probes ni hacer escrituras."""

    target = _require_plain_directory(Path(root), "biblioteca TV")
    return {
        "supported": True,
        "operation": "direct_move",
        "operations": ["direct_move"],
        "st_dev": int(target.stat().st_dev),
    }


def publish_series(
    job_id: str,
    prepared_series_root: Path | str,
    final_series_root: Path | str,
    journal: DurableJournal,
    *,
    expected_files: Iterable[Path | str] | None = None,
    expected_file_digests: object | None = None,
    allowed_existing_files: object | None = None,
) -> dict[str, Any]:
    """Mueve las salidas ya verificadas sin copiar ni volver a leer el video."""

    del expected_file_digests, allowed_existing_files
    job = str(job_id).strip()
    if not job:
        raise ValueError("job_id no puede estar vacio")
    prepared, final = _canonical_paths(prepared_series_root, final_series_root)
    expected = _normalize_expected_files(expected_files)
    snapshot = journal.snapshot()
    if snapshot is None:
        raise DeliveryError("El trabajo no tiene journal preparado")
    state = str(snapshot["state"])
    details = dict(snapshot["details"])
    generation = str(details.get("generation") or uuid.uuid4().hex)
    if state == "COMMITTED":
        return _result(job, generation, final, recovered=True)
    if state == "ROLLED_BACK":
        raise RecoveryAmbiguous("El trabajo ya esta cerrado como ROLLED_BACK")
    if state == "PREPARED":
        journal.transition("PROCESSING")
        state = "PROCESSING"
    if state == "PROCESSING":
        _check_before_move(prepared, final, expected)
        journal.transition(
            "VERIFIED",
            job_id=job,
            generation=generation,
            prepared_series_root=str(prepared),
            final_series_root=str(final),
            mode="direct_move",
            expected_files=list(expected),
        )
        state = "VERIFIED"
    if state == "VERIFIED":
        journal.transition("COMMITTING")
        state = "COMMITTING"
    if state != "COMMITTING":
        raise RecoveryAmbiguous(f"Estado de publicacion no soportado: {state}")
    _move_expected(prepared, final, expected, recovery=True)
    journal.transition("COMMITTED", cleanup_complete=True, marker_retired=True)
    return _result(job, generation, final, recovered=False)


def recover_delivery(
    job_id: str,
    prepared_series_root: Path | str,
    final_series_root: Path | str,
    journal: DurableJournal,
) -> dict[str, Any]:
    snapshot = journal.snapshot()
    if snapshot is None:
        return {"status": "resume_processing", "job_id": str(job_id)}
    state = str(snapshot["state"])
    details = dict(snapshot["details"])
    if state in {"PREPARED", "PROCESSING"}:
        return {"status": "resume_processing", "job_id": str(job_id)}
    if state == "ROLLED_BACK":
        return {"status": "rolled_back", "job_id": str(job_id), "recovered": True}
    expected = details.get("expected_files")
    if not isinstance(expected, list):
        raise RecoveryAmbiguous("La publicacion directa no conserva expected_files")
    prepared, final = _canonical_paths(
        prepared_series_root,
        final_series_root,
        prepared_may_be_missing=True,
    )
    generation = str(details.get("generation") or "")
    if state == "COMMITTED":
        _remove_empty_tree(prepared)
        return _result(str(job_id), generation, final, recovered=True)
    if state == "VERIFIED":
        journal.transition("COMMITTING")
        state = "COMMITTING"
    if state != "COMMITTING":
        raise RecoveryAmbiguous(f"Estado de recuperacion no soportado: {state}")
    _move_expected(prepared, final, _normalize_expected_files(expected), recovery=True)
    journal.transition("COMMITTED", cleanup_complete=True, marker_retired=True)
    return _result(str(job_id), generation, final, recovered=True)


__all__ = [
    "AtomicDeliveryUnsupported",
    "DeliveryConflict",
    "DeliveryError",
    "MARKER_NAME",
    "RecoveryAmbiguous",
    "preflight_atomic_exchange",
    "publish_series",
    "recover_delivery",
]
