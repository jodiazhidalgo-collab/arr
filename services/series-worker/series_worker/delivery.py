"""Publicacion atomica de una raiz completa de serie.

No existe fallback secuencial. Una serie nueva usa ``RENAME_NOREPLACE`` y una
existente se publica intercambiando su raiz con una sombra completa mediante
``RENAME_EXCHANGE``. Un marcador durable permite resolver un corte entre el
syscall atomico y la escritura final del journal.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from .journal import DurableJournal, JournalContradiction, fsync_directory
from .journal import write_json_file_atomic


AT_FDCWD = -100
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
MARKER_NAME = ".series-worker-generation.json"
MARKER_SCHEMA_VERSION = 1
_UNSUPPORTED_ERRNOS = {errno.ENOSYS, errno.EINVAL, errno.EXDEV}
_GENERATION_RE = re.compile(
    r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})",
    re.ASCII,
)
_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PREFLIGHT_GUARD = threading.Lock()
_PREFLIGHT_PREFIX = ".series-worker-exchange-probe-"
_PREFLIGHT_OWNER_FILE = ".series-worker-preflight.json"
_PREFLIGHT_OWNER = {"schema_version": 1, "owner": "series-worker-preflight"}
_PREFLIGHT_LOCK_NAME = ".series-worker-preflight.lock"


class DeliveryError(RuntimeError):
    code = "delivery_failed"

    def __init__(self, message: str, *, errno_value: int | None = None):
        super().__init__(message)
        self.errno = errno_value


class AtomicDeliveryUnsupported(DeliveryError):
    code = "atomic_exchange_unsupported"


class DeliveryConflict(DeliveryError):
    code = "delivery_conflict"


class RecoveryAmbiguous(DeliveryError):
    code = "recovery_ambiguous"


def _raise_ambiguous(message: str) -> None:
    raise RecoveryAmbiguous(f"recovery_ambiguous: {message}")


def _valid_generation(value: Any) -> bool:
    return isinstance(value, str) and _GENERATION_RE.fullmatch(value) is not None


def _require_generation(value: Any, *, recovery: bool) -> str:
    if _valid_generation(value):
        return value
    if recovery:
        _raise_ambiguous("la generacion no es un UUID/hex seguro")
    raise DeliveryError("generation debe ser un UUID/hex seguro")


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _series_lock_path(final: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(_path_key(final))).hexdigest()[:32]
    return final.parent / f".series-worker-publish-{digest}.lock"


@contextmanager
def _series_delivery_lock(final: Path) -> Iterator[Path]:
    """Serializa publicacion y recovery por serie, tambien entre procesos."""

    lock_path = _series_lock_path(final)
    key = _path_key(lock_path)
    with _LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(key, threading.Lock())
    with process_lock:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            yield lock_path
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@contextmanager
def _preflight_process_lock(root: Path) -> Iterator[Path]:
    """Serializa el probe atómico también entre contenedores/procesos."""

    configured = str(os.environ.get("SERIES_ATOMIC_PREFLIGHT_LOCK_PATH") or "").strip()
    selected = Path(configured) if configured else root / _PREFLIGHT_LOCK_NAME
    lock_path = Path(os.path.abspath(os.path.normpath(os.fspath(selected))))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = _path_key(lock_path)
    with _LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(key, threading.Lock())
    with process_lock:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise AtomicDeliveryUnsupported("El lock del preflight no es un archivo regular")
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            yield lock_path
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _renameat2(old: Path, new: Path, flags: int) -> None:
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOSYS, "renameat2 solo esta disponible en Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise OSError(errno.ENOSYS, "libc no expone renameat2")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        AT_FDCWD,
        os.fsencode(old),
        AT_FDCWD,
        os.fsencode(new),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(old), str(new))


def _rename_exchange(left: Path, right: Path) -> None:
    _renameat2(left, right, RENAME_EXCHANGE)


def _rename_noreplace(source: Path, destination: Path) -> None:
    if sys.platform.startswith("linux"):
        _renameat2(source, destination, RENAME_NOREPLACE)
        return
    if os.name == "nt":
        # MoveFile en Windows falla si el destino existe; no reemplaza.
        os.rename(source, destination)
        return
    raise OSError(errno.ENOSYS, "No hay rename atomico NOREPLACE seguro")


def _unsupported(operation: str, exc: OSError) -> AtomicDeliveryUnsupported:
    number = exc.errno if exc.errno is not None else errno.EIO
    return AtomicDeliveryUnsupported(
        f"{operation} no esta disponible de forma atomica: [{number}] {exc.strerror or exc}",
        errno_value=number,
    )


def _require_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise DeliveryError(f"{label} no puede ser un enlace simbolico")
    if not path.is_dir():
        raise DeliveryError(f"{label} no existe o no es un directorio: {path}")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _canonical_paths(
    prepared_series_root: Path | str, final_series_root: Path | str
) -> tuple[Path, Path]:
    prepared_input = Path(prepared_series_root)
    if prepared_input.is_symlink():
        raise DeliveryError("prepared_series_root no puede ser un enlace simbolico")
    prepared = prepared_input.resolve(strict=True)
    final_input = Path(final_series_root)
    if final_input.name in {"", ".", ".."}:
        raise DeliveryError("final_series_root no tiene un nombre de serie valido")
    final_parent = final_input.parent.resolve(strict=True)
    final = final_parent / final_input.name
    _require_directory(prepared, "prepared_series_root")
    _require_directory(final_parent, "padre de final_series_root")
    if prepared == final or _is_within(prepared, final) or _is_within(final, prepared):
        raise DeliveryError("Staging y biblioteca deben ser arboles independientes")
    return prepared, final


def _normalize_expected_files(
    expected_files: Iterable[Path | str] | None,
) -> frozenset[str] | None:
    if expected_files is None:
        return None
    if isinstance(expected_files, (str, bytes, os.PathLike)):
        raise TypeError("expected_files debe ser una coleccion de rutas relativas")
    normalized: set[str] = set()
    for raw in expected_files:
        text = os.fspath(raw)
        if not isinstance(text, str):
            raise TypeError("expected_files solo admite rutas de texto")
        relative = PurePosixPath(text.replace("\\", "/"))
        if (
            relative.is_absolute()
            or relative == PurePosixPath(".")
            or ".." in relative.parts
            or relative.parts == (MARKER_NAME,)
        ):
            raise DeliveryError(f"Ruta esperada no segura: {text!r}")
        value = relative.as_posix()
        if value in normalized:
            raise DeliveryError(f"Ruta esperada duplicada: {value}")
        normalized.add(value)
    if not normalized:
        raise DeliveryError("expected_files no puede estar vacio")
    return frozenset(normalized)


def _prepared_file_set(root: Path, *, allow_marker: bool) -> frozenset[str]:
    files: set[str] = set()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise DeliveryError(f"prepared_series_root contiene un symlink: {current_path}")
        relative_root = current_path.relative_to(root)
        for name in directories:
            directory = current_path / name
            if directory.is_symlink():
                raise DeliveryError(
                    f"prepared_series_root contiene un symlink: {directory}"
                )
        for name in filenames:
            path = current_path / name
            relative = relative_root / name
            if _reserved_marker(relative):
                if allow_marker:
                    continue
                raise DeliveryError(f"{MARKER_NAME} esta reservado al series-worker")
            if _reserved_marker_temp(relative):
                raise DeliveryError(
                    f"El temporal interno de {MARKER_NAME} esta reservado al series-worker"
                )
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise DeliveryError(f"prepared_series_root contiene un archivo no valido: {path}")
            files.add(PurePosixPath(*relative.parts).as_posix())
    return frozenset(files)


def _validate_prepared_contents(
    root: Path,
    expected_files: frozenset[str] | None,
    *,
    allow_marker: bool = False,
) -> frozenset[str]:
    actual = _prepared_file_set(root, allow_marker=allow_marker)
    if not actual:
        raise DeliveryError("prepared_series_root no contiene ningun archivo publicable")
    if expected_files is not None and actual != expected_files:
        missing = sorted(expected_files - actual)[:5]
        unexpected = sorted(actual - expected_files)[:5]
        raise DeliveryError(
            "prepared_series_root no coincide con expected_files "
            f"(faltan={missing}, sobran={unexpected})"
        )
    return actual


def _validate_tree_device(root: Path, expected_device: int, label: str) -> None:
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise DeliveryError(f"{label} contiene un symlink: {current_path}")
        if current_path.stat().st_dev != expected_device:
            raise AtomicDeliveryUnsupported(
                f"{label} cruza dispositivos; se exige un unico st_dev",
                errno_value=errno.EXDEV,
            )
        for name in [*directories, *filenames]:
            entry = current_path / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise DeliveryError(f"{label} contiene un symlink: {entry}")
            if info.st_dev != expected_device:
                raise AtomicDeliveryUnsupported(
                    f"{label} cruza dispositivos; se exige un unico st_dev",
                    errno_value=errno.EXDEV,
                )
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise DeliveryError(f"{label} contiene un tipo no soportado: {entry}")


def _validate_same_device(prepared: Path, final: Path) -> int:
    device = final.parent.stat().st_dev
    if prepared.stat().st_dev != device:
        raise AtomicDeliveryUnsupported(
            "prepared_series_root y final_series_root no comparten st_dev",
            errno_value=errno.EXDEV,
        )
    if final.exists() and final.stat().st_dev != device:
        raise AtomicDeliveryUnsupported(
            "La serie final y su padre no comparten st_dev",
            errno_value=errno.EXDEV,
        )
    _validate_tree_device(prepared, device, "prepared_series_root")
    if final.exists():
        _validate_tree_device(final, device, "final_series_root")
    return device


def _fsync_file(path: Path) -> None:
    # Windows rechaza fsync sobre un descriptor abierto solo para lectura.
    flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for current, child_directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories.append(current_path)
        for name in filenames:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise DeliveryError(f"Arbol no sincronizable: {path}")
            _fsync_file(path)
        for name in child_directories:
            if (current_path / name).is_symlink():
                raise DeliveryError(f"Arbol no sincronizable: {current_path / name}")
    for directory in reversed(directories):
        fsync_directory(directory)


def _cleanup_preflight_probes(root: Path) -> None:
    for candidate in sorted(root.iterdir()):
        if not candidate.name.startswith(_PREFLIGHT_PREFIX):
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise AtomicDeliveryUnsupported("Probe atómico huérfano no es seguro")
        entries = list(candidate.iterdir())
        marker_path = candidate / _PREFLIGHT_OWNER_FILE
        marker: Any = None
        if marker_path.is_file() and not marker_path.is_symlink():
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                marker = None
        only_marker_temps = bool(entries) and all(
            entry.is_file()
            and entry.name.startswith(f".{_PREFLIGHT_OWNER_FILE}.")
            and entry.name.endswith(".tmp")
            for entry in entries
        )
        if entries and marker != _PREFLIGHT_OWNER and not only_marker_temps:
            raise AtomicDeliveryUnsupported("Probe atómico huérfano no reconocido")
        if marker == _PREFLIGHT_OWNER:
            for entry in entries:
                if entry.name == _PREFLIGHT_OWNER_FILE:
                    continue
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    raise AtomicDeliveryUnsupported(
                        "Probe atómico contiene una entrada no regular"
                    )
            fsync_directory(candidate)
            marker_path.unlink()
            fsync_directory(candidate)
        elif only_marker_temps:
            for entry in entries:
                entry.unlink()
            fsync_directory(candidate)
        candidate.rmdir()
        fsync_directory(root)


def preflight_atomic_exchange(library_root: Path | str) -> dict[str, Any]:
    """Ejecuta ``RENAME_EXCHANGE`` y ``RENAME_NOREPLACE`` en el destino."""

    root = Path(library_root).resolve(strict=True)
    with _PREFLIGHT_GUARD, _preflight_process_lock(root):
        return _preflight_atomic_exchange_locked(root)


def _preflight_atomic_exchange_locked(library_root: Path | str) -> dict[str, Any]:

    root = Path(library_root).resolve(strict=True)
    _require_directory(root, "library_root")
    _cleanup_preflight_probes(root)
    probe_root = Path(tempfile.mkdtemp(prefix=_PREFLIGHT_PREFIX, dir=root))
    try:
        write_json_file_atomic(probe_root / _PREFLIGHT_OWNER_FILE, _PREFLIGHT_OWNER)
        left = probe_root / "left"
        right = probe_root / "right"
        left.mkdir()
        right.mkdir()
        (left / "side").write_text("left", encoding="ascii")
        (right / "side").write_text("right", encoding="ascii")
        _fsync_tree(left)
        _fsync_tree(right)
        fsync_directory(probe_root)
        fsync_directory(root)
        try:
            _rename_exchange(left, right)
        except OSError as exc:
            raise _unsupported("renameat2(RENAME_EXCHANGE)", exc) from exc
        fsync_directory(probe_root)
        if (left / "side").read_text(encoding="ascii") != "right":
            raise AtomicDeliveryUnsupported("RENAME_EXCHANGE no intercambio el lado izquierdo")
        if (right / "side").read_text(encoding="ascii") != "left":
            raise AtomicDeliveryUnsupported("RENAME_EXCHANGE no intercambio el lado derecho")

        source = probe_root / "noreplace-source"
        destination = probe_root / "noreplace-destination"
        source.write_text("source", encoding="ascii")
        _fsync_file(source)
        fsync_directory(probe_root)
        try:
            _rename_noreplace(source, destination)
        except OSError as exc:
            raise _unsupported("renameat2(RENAME_NOREPLACE)", exc) from exc
        fsync_directory(probe_root)
        if source.exists() or destination.read_text(encoding="ascii") != "source":
            raise AtomicDeliveryUnsupported("RENAME_NOREPLACE no movio la fuente")

        conflict_source = probe_root / "noreplace-conflict-source"
        conflict_destination = probe_root / "noreplace-conflict-destination"
        conflict_source.write_text("keep-source", encoding="ascii")
        conflict_destination.write_text("keep-destination", encoding="ascii")
        try:
            _rename_noreplace(conflict_source, conflict_destination)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise _unsupported("renameat2(RENAME_NOREPLACE)", exc) from exc
        else:
            raise AtomicDeliveryUnsupported("RENAME_NOREPLACE reemplazo un destino existente")
        if (
            conflict_source.read_text(encoding="ascii") != "keep-source"
            or conflict_destination.read_text(encoding="ascii") != "keep-destination"
        ):
            raise AtomicDeliveryUnsupported("RENAME_NOREPLACE altero un conflicto")
        return {
            "supported": True,
            "operation": "renameat2(RENAME_EXCHANGE)",
            "operations": [
                "renameat2(RENAME_EXCHANGE)",
                "renameat2(RENAME_NOREPLACE)",
            ],
            "st_dev": root.stat().st_dev,
        }
    finally:
        _cleanup_preflight_probes(root)


def _marker_payload(job_id: str, generation: str) -> dict[str, Any]:
    return {
        "schema_version": MARKER_SCHEMA_VERSION,
        "job_id": job_id,
        "generation": generation,
    }


def _write_marker(root: Path, job_id: str, generation: str) -> Path:
    marker = write_json_file_atomic(root / MARKER_NAME, _marker_payload(job_id, generation))
    fsync_directory(root)
    return marker


def _path_entry_exists(path: Path) -> bool:
    """Incluye enlaces colgantes; ``Path.exists`` los oculta."""

    return path.exists() or path.is_symlink()


def _read_marker(root: Path) -> dict[str, Any] | None:
    if root.is_symlink():
        _raise_ambiguous(f"la raiz esperada es un enlace simbolico: {root}")
    if not root.exists():
        return None
    if not root.is_dir():
        _raise_ambiguous(f"la raiz esperada no es un directorio normal: {root}")
    marker_path = root / MARKER_NAME
    if marker_path.is_symlink():
        _raise_ambiguous(f"el marcador esperado es un enlace simbolico: {marker_path}")
    if not marker_path.exists():
        return None
    try:
        value = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryAmbiguous(
            f"recovery_ambiguous: marcador invalido en {root}"
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != MARKER_SCHEMA_VERSION
        or not isinstance(value.get("job_id"), str)
        or not _valid_generation(value.get("generation"))
    ):
        _raise_ambiguous(f"marcador no reconocido en {root}")
    return value


def _is_generation(marker: dict[str, Any] | None, job_id: str, generation: str) -> bool:
    return bool(
        marker
        and marker.get("job_id") == job_id
        and marker.get("generation") == generation
    )


def _reserved_marker(path: Path) -> bool:
    return len(path.parts) == 1 and path.name == MARKER_NAME


def _reserved_marker_temp(path: Path) -> bool:
    prefix = f".{MARKER_NAME}."
    return bool(
        len(path.parts) == 1
        and path.name.startswith(prefix)
        and path.name.endswith(".tmp")
        and len(path.name) > len(prefix) + len(".tmp")
    )


def _cleanup_owned_marker_temps(root: Path) -> None:
    removed = False
    for path in root.iterdir():
        if not _reserved_marker_temp(Path(path.name)):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            _raise_ambiguous("temporal de marcador no es un archivo regular")
        path.unlink()
        removed = True
    if removed:
        fsync_directory(root)


def _clone_existing_with_hardlinks(source: Path, destination: Path) -> None:
    destination.mkdir(mode=stat.S_IMODE(source.stat().st_mode))
    for current, directories, filenames in os.walk(source, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        target_current = destination / relative
        for name in directories:
            source_directory = current_path / name
            if source_directory.is_symlink():
                raise DeliveryError(f"Symlink no soportado: {source_directory}")
            target_directory = target_current / name
            target_directory.mkdir(mode=stat.S_IMODE(source_directory.stat().st_mode))
        for name in filenames:
            relative_file = relative / name
            if _reserved_marker(relative_file):
                continue
            source_file = current_path / name
            if source_file.is_symlink() or not source_file.is_file():
                raise DeliveryError(f"Archivo no soportado: {source_file}")
            os.link(source_file, target_current / name)


def _remove_shadow_entry(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _logical_relative_key(path: Path) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFKC", part).casefold()
        for part in PurePosixPath(path.as_posix()).parts
    )


def _validate_logical_tree(root: Path) -> None:
    """Impide publicar dos rutas equivalentes por casefold/NFKC."""

    seen: dict[tuple[str, ...], str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        raw = relative.as_posix()
        logical = _logical_relative_key(relative)
        previous = seen.get(logical)
        if previous is not None and previous != raw:
            raise DeliveryConflict(
                f"El arbol contiene rutas logicamente duplicadas: {previous} / {raw}"
            )
        seen[logical] = raw


def _overlay_prepared_with_hardlinks(prepared: Path, shadow: Path) -> None:
    for current, directories, filenames in os.walk(prepared, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(prepared)
        target_current = shadow / relative
        if target_current.exists() and not target_current.is_dir():
            _remove_shadow_entry(target_current)
        target_current.mkdir(exist_ok=True)
        for name in directories:
            source_directory = current_path / name
            if source_directory.is_symlink():
                raise DeliveryError(f"Symlink no soportado: {source_directory}")
            target_directory = target_current / name
            if target_directory.exists() and not target_directory.is_dir():
                _remove_shadow_entry(target_directory)
            target_directory.mkdir(exist_ok=True)
        for name in filenames:
            relative_file = relative / name
            if _reserved_marker(relative_file):
                raise DeliveryError(f"{MARKER_NAME} esta reservado al series-worker")
            source_file = current_path / name
            if source_file.is_symlink() or not source_file.is_file():
                raise DeliveryError(f"Archivo no soportado: {source_file}")
            target_file = target_current / name
            if target_file.exists() or target_file.is_symlink():
                _remove_shadow_entry(target_file)
            # Se crea un enlace nuevo; nunca se abre ni modifica un hardlink heredado.
            os.link(source_file, target_file)


def _tree_signature(root: Path) -> tuple[tuple[Any, ...], ...]:
    root_info = root.lstat()
    entries: list[tuple[Any, ...]] = [
        ("root", ".", root_info.st_ino, root_info.st_mtime_ns)
    ]
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(root)
        for name in sorted(directories):
            path = current_path / name
            info = path.lstat()
            entries.append(("d", str(relative / name), info.st_ino, info.st_mtime_ns))
        for name in sorted(filenames):
            path = current_path / name
            info = path.lstat()
            entries.append(
                ("f", str(relative / name), info.st_ino, info.st_size, info.st_mtime_ns)
            )
    return tuple(entries)


def _signature_digest(signature: tuple[tuple[Any, ...], ...]) -> str:
    encoded = json.dumps(
        signature,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tree_signature_digest(root: Path) -> str:
    return _signature_digest(_tree_signature(root))


def _required_signature(details: dict[str, Any], key: str) -> str:
    value = details.get(key)
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _raise_ambiguous(f"falta firma durable {key}")
    return value


def _verify_tree_signature(path: Path, expected: str, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        _raise_ambiguous(f"{label} ya no es un directorio físico")
    if _tree_signature_digest(path) != expected:
        _raise_ambiguous(f"{label} cambió desde VERIFIED")


def _safe_job_fragment(job_id: str) -> str:
    fragment = re.sub(r"[^A-Za-z0-9_.-]+", "-", job_id).strip(".-")
    return (fragment or "job")[:48]


def _shadow_path(final: Path, job_id: str, generation: str) -> Path:
    safe_generation = _require_generation(generation, recovery=False)
    return final.parent / (
        f".{final.name}.series-worker.{_safe_job_fragment(job_id)}.{safe_generation}.shadow"
    )


def _journal_shadow_path(
    details: dict[str, Any],
    *,
    final: Path,
    job_id: str,
    generation: str,
) -> Path:
    """Valida el texto del journal, pero devuelve solo la ruta derivada."""

    expected = _shadow_path(final, job_id, generation)
    raw = details.get("shadow_root")
    if not isinstance(raw, str) or not raw:
        _raise_ambiguous("falta shadow_root en una publicacion exchange")
    recorded = Path(raw)
    if not recorded.is_absolute() or ".." in recorded.parts:
        _raise_ambiguous("shadow_root no es una ruta absoluta segura")
    if _path_key(recorded.parent) != _path_key(final.parent):
        _raise_ambiguous("shadow_root queda fuera del padre de la serie")
    if _path_key(recorded) != _path_key(expected):
        _raise_ambiguous("shadow_root no coincide con la ruta derivada")
    return expected


def _remove_owned_shadow(
    shadow: Path,
    *,
    final: Path,
    job_id: str,
    generation: str,
    allow_unmarked: bool,
) -> None:
    expected = _shadow_path(final, job_id, generation)
    if shadow != expected or shadow.parent != final.parent:
        _raise_ambiguous("el shadow registrado no coincide con el nombre derivado")
    if shadow.is_symlink():
        _raise_ambiguous("el shadow no puede ser un enlace simbolico")
    if not shadow.exists():
        return
    marker = _read_marker(shadow)
    if marker is not None and not _is_generation(marker, job_id, generation):
        _raise_ambiguous("el shadow pertenece a otra generacion")
    if marker is None and not allow_unmarked:
        _raise_ambiguous("el shadow carece del marcador de propiedad")
    if shadow.is_symlink() or not shadow.is_dir():
        _raise_ambiguous("el shadow no es un directorio normal")
    shutil.rmtree(shadow)
    fsync_directory(shadow.parent)


def _build_shadow(
    prepared: Path,
    final: Path,
    shadow: Path,
    *,
    job_id: str,
    generation: str,
) -> tuple[
    tuple[tuple[Any, ...], ...],
    tuple[tuple[Any, ...], ...],
    tuple[tuple[Any, ...], ...],
]:
    _remove_owned_shadow(
        shadow,
        final=final,
        job_id=job_id,
        generation=generation,
        allow_unmarked=True,
    )
    final_signature = _tree_signature(final)
    prepared_signature = _tree_signature(prepared)
    try:
        _clone_existing_with_hardlinks(final, shadow)
        _overlay_prepared_with_hardlinks(prepared, shadow)
        _validate_logical_tree(shadow)
        _write_marker(shadow, job_id, generation)
        _fsync_tree(shadow)
        fsync_directory(shadow.parent)
        if _tree_signature(final) != final_signature:
            raise DeliveryConflict("La serie final cambio mientras se construia la sombra")
        if _tree_signature(prepared) != prepared_signature:
            raise DeliveryConflict("El arbol preparado cambio mientras se construia la sombra")
        shadow_signature = _tree_signature(shadow)
        return final_signature, prepared_signature, shadow_signature
    except Exception:
        if shadow.exists():
            marker = _read_marker(shadow)
            if marker is None or _is_generation(marker, job_id, generation):
                shutil.rmtree(shadow)
                fsync_directory(shadow.parent)
        raise


def _journal_details(
    job_id: str,
    generation: str,
    prepared: Path,
    final: Path,
    mode: str,
    shadow: Path | None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "job_id": job_id,
        "generation": generation,
        "prepared_series_root": str(prepared),
        "final_series_root": str(final),
        "mode": mode,
        "marker_name": MARKER_NAME,
    }
    if shadow is not None:
        details["shadow_root"] = str(shadow)
    return details


def _validate_journal_identity(
    snapshot: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    details = snapshot.get("details", {})
    for key, value in expected.items():
        if key in details and details[key] != value:
            _raise_ambiguous(f"el journal contradice {key}")


def _prepared_marker_allowed_for_retry(
    job_id: str,
    prepared: Path,
    final: Path,
    journal: DurableJournal,
) -> bool:
    """Autoriza solo el marcador durable de este job al reentrar en ``publish``."""

    try:
        snapshot = journal.snapshot()
    except JournalContradiction as exc:
        raise RecoveryAmbiguous("recovery_ambiguous: journal contradictorio") from exc
    if snapshot is None or snapshot["state"] == "PREPARED":
        return False
    details = snapshot["details"]
    if details.get("mode") != "new":
        return False
    generation = _require_generation(details.get("generation"), recovery=True)
    expected = _journal_details(job_id, generation, prepared, final, "new", None)
    _validate_journal_identity(snapshot, expected)
    if any(details.get(key) != value for key, value in expected.items()):
        _raise_ambiguous("el journal no conserva la identidad completa del job")
    if snapshot["state"] == "PROCESSING":
        _cleanup_owned_marker_temps(prepared)
    return _is_generation(_read_marker(prepared), job_id, generation)


def _rollback_before_exchange(
    journal: DurableJournal,
    *,
    reason: BaseException,
    prepared: Path,
    shadow: Path | None,
    final: Path,
    job_id: str,
    generation: str,
) -> None:
    current = journal.state
    if current not in {"COMMITTED", "ROLLED_BACK"}:
        journal.transition(
            "ROLLED_BACK",
            failure_code=getattr(reason, "code", type(reason).__name__),
        )
    if shadow is not None:
        _remove_owned_shadow(
            shadow,
            final=final,
            job_id=job_id,
            generation=generation,
            allow_unmarked=True,
        )
    else:
        _remove_owned_prepared_marker(
            prepared,
            job_id=job_id,
            generation=generation,
        )


def _remove_owned_prepared_marker(
    prepared: Path,
    *,
    job_id: str,
    generation: str,
) -> None:
    if not _path_entry_exists(prepared):
        return
    marker = _read_marker(prepared)
    if marker is None:
        return
    if not _is_generation(marker, job_id, generation):
        _raise_ambiguous("prepared_series_root conserva un marcador ajeno")
    (prepared / MARKER_NAME).unlink()
    fsync_directory(prepared)


def _cleanup_root_identity(path: Path) -> dict[str, int]:
    info = path.lstat()
    if path.is_symlink() or not path.is_dir():
        _raise_ambiguous("raíz de cleanup no es un directorio físico")
    return {"st_dev": int(info.st_dev), "st_ino": int(info.st_ino)}


def _root_identity_from_value(value: Any, label: str) -> dict[str, int]:
    if (
        not isinstance(value, dict)
        or set(value) != {"st_dev", "st_ino"}
        or not isinstance(value["st_dev"], int)
        or isinstance(value["st_dev"], bool)
        or value["st_dev"] < 0
        or not isinstance(value["st_ino"], int)
        or isinstance(value["st_ino"], bool)
        or value["st_ino"] <= 0
    ):
        _raise_ambiguous(f"identidad de raíz inválida para {label}")
    return {"st_dev": value["st_dev"], "st_ino": value["st_ino"]}


def _required_root_identity(details: dict[str, Any], key: str) -> dict[str, int]:
    if key not in details:
        _raise_ambiguous(f"falta identidad durable {key}")
    return _root_identity_from_value(details[key], key)


def _root_identities_from_details(
    details: dict[str, Any],
    key: str,
) -> dict[str, dict[str, int]]:
    raw = details.get(key)
    if not isinstance(raw, dict) or not set(raw).issubset({"shadow", "prepared"}):
        _raise_ambiguous(f"{key} no contiene identidades de raíces válidas")
    result: dict[str, dict[str, int]] = {}
    for root_name, value in raw.items():
        result[root_name] = _root_identity_from_value(
            value,
            f"{key}.{root_name}",
        )
    return result


def _required_cleanup_identities(
    details: dict[str, Any],
    mode: str,
) -> dict[str, dict[str, int]]:
    identities = _root_identities_from_details(details, "cleanup_identities")
    expected = {"shadow", "prepared"} if mode == "exchange" else set()
    if set(identities) != expected:
        _raise_ambiguous("cleanup_identities contradice el modo de publicación")
    return identities


def _cleanup_roots_from_details(details: dict[str, Any]) -> dict[str, dict[str, int]]:
    return _root_identities_from_details(details, "cleanup_roots")


def _verify_root_identity(
    path: Path,
    expected: dict[str, int],
    label: str,
) -> None:
    if _cleanup_root_identity(path) != expected:
        _raise_ambiguous(f"{label} cambió de identidad desde VERIFIED")


def _verify_cleanup_identity(
    path: Path,
    cleanup_roots: dict[str, dict[str, int]],
    key: str,
) -> None:
    expected = cleanup_roots.get(key)
    if expected is None:
        _raise_ambiguous(f"apareció una raíz {key} no registrada para cleanup")
    if _cleanup_root_identity(path) != expected:
        _raise_ambiguous(f"la raíz {key} cambió durante cleanup")


def _cleanup_committed_paths(
    *,
    mode: str,
    prepared: Path,
    final: Path,
    shadow: Path | None,
    job_id: str,
    generation: str,
    cleanup_roots: dict[str, dict[str, int]],
) -> list[str]:
    pending: list[str] = []
    try:
        if mode == "exchange" and shadow is not None and _path_entry_exists(shadow):
            marker = _read_marker(shadow)
            if _is_generation(marker, job_id, generation):
                _raise_ambiguous("final y shadow contienen la misma generacion")
            if shadow != _shadow_path(final, job_id, generation):
                _raise_ambiguous("shadow de limpieza no reconocido")
            _verify_cleanup_identity(shadow, cleanup_roots, "shadow")
            shutil.rmtree(shadow)
            fsync_directory(shadow.parent)
    except RecoveryAmbiguous:
        raise
    except OSError:
        if shadow is not None:
            pending.append(str(shadow))

    if _path_entry_exists(prepared):
        try:
            if prepared.is_symlink() or not prepared.is_dir():
                _raise_ambiguous("prepared_series_root cambio de tipo")
            _verify_cleanup_identity(prepared, cleanup_roots, "prepared")
            shutil.rmtree(prepared)
            fsync_directory(prepared.parent)
        except RecoveryAmbiguous:
            raise
        except OSError:
            pending.append(str(prepared))
    return pending


def _cleanup_committed_durable(
    journal: DurableJournal,
    *,
    mode: str,
    prepared: Path,
    final: Path,
    shadow: Path | None,
    job_id: str,
    generation: str,
) -> list[str]:
    snapshot = journal.snapshot()
    if snapshot is None or snapshot["state"] != "COMMITTED":
        _raise_ambiguous("cleanup solo puede empezar desde COMMITTED")
    details = snapshot["details"]
    durable_identities = _required_cleanup_identities(details, mode)
    started = details.get("cleanup_started")
    complete = details.get("cleanup_complete")
    if started not in {None, True} or complete not in {None, True}:
        _raise_ambiguous("flags de cleanup inválidos")
    if complete is True:
        if _path_entry_exists(prepared) or (
            shadow is not None and _path_entry_exists(shadow)
        ):
            _raise_ambiguous("cleanup_complete contradice raíces existentes")
        return []

    if started is True:
        cleanup_roots = _cleanup_roots_from_details(details)
        if cleanup_roots != durable_identities:
            _raise_ambiguous("cleanup_roots contradice las identidades de VERIFIED")
    else:
        cleanup_roots = durable_identities
        if mode == "exchange":
            if (
                shadow is None
                or not _path_entry_exists(shadow)
                or not _path_entry_exists(prepared)
            ):
                _raise_ambiguous("faltan raíces de cleanup antes de iniciar su WAL")
            _verify_root_identity(shadow, cleanup_roots["shadow"], "shadow antiguo")
            _verify_root_identity(
                prepared,
                cleanup_roots["prepared"],
                "prepared_series_root",
            )
        elif _path_entry_exists(prepared) or shadow is not None:
            _raise_ambiguous("publicación nueva conserva staging inesperado")
        journal.transition(
            "COMMITTED",
            cleanup_started=True,
            cleanup_roots=cleanup_roots,
        )

    pending = _cleanup_committed_paths(
        mode=mode,
        prepared=prepared,
        final=final,
        shadow=shadow,
        job_id=job_id,
        generation=generation,
        cleanup_roots=cleanup_roots,
    )
    if not pending:
        journal.transition("COMMITTED", cleanup_complete=True)
    return pending


def _result(
    *,
    job_id: str,
    generation: str,
    mode: str,
    final: Path,
    recovered: bool,
    cleanup_pending: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "status": "committed",
        "job_id": job_id,
        "generation": generation,
        "mode": mode,
        "final_series_root": str(final),
        "marker": str(final / MARKER_NAME),
        "recovered": recovered,
        "cleanup_pending": list(cleanup_pending),
    }


def _commit_new(prepared: Path, final: Path) -> None:
    try:
        _rename_noreplace(prepared, final)
    except FileExistsError as exc:
        raise DeliveryConflict("La serie final ya existe; no se ha sobrescrito") from exc
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise DeliveryConflict("La serie final ya existe; no se ha sobrescrito") from exc
        if exc.errno in _UNSUPPORTED_ERRNOS:
            raise _unsupported("renameat2(RENAME_NOREPLACE)", exc) from exc
        raise
    fsync_directory(prepared.parent)
    fsync_directory(final.parent)


def _commit_exchange(shadow: Path, final: Path) -> None:
    try:
        _rename_exchange(shadow, final)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_ERRNOS:
            raise _unsupported("renameat2(RENAME_EXCHANGE)", exc) from exc
        raise
    fsync_directory(shadow.parent)
    fsync_directory(final.parent)


def _confirm_published_durable(
    *,
    mode: str,
    prepared: Path,
    final: Path,
    shadow: Path | None,
) -> None:
    """Reintenta la barrera durable tras detectar el marcador publicado."""

    _fsync_tree(final)
    source_parent = shadow.parent if mode == "exchange" and shadow is not None else prepared.parent
    fsync_directory(source_parent)
    fsync_directory(final.parent)


def recover_delivery(
    job_id: str,
    prepared_series_root: Path | str,
    final_series_root: Path | str,
    journal: DurableJournal,
) -> dict[str, Any]:
    """Converge una entrega interrumpida usando journal y marcador durable."""

    job = str(job_id).strip()
    if not job:
        raise ValueError("job_id no puede estar vacio")
    prepared, final = _canonical_paths_for_recovery(
        prepared_series_root, final_series_root
    )
    with _series_delivery_lock(final):
        return _recover_delivery_locked(job, prepared, final, journal)


def _recover_delivery_locked(
    job: str,
    prepared: Path,
    final: Path,
    journal: DurableJournal,
) -> dict[str, Any]:
    try:
        snapshot = journal.snapshot()
    except JournalContradiction as exc:
        raise RecoveryAmbiguous("recovery_ambiguous: journal contradictorio") from exc
    if snapshot is None:
        return {"status": "not_started", "job_id": job}
    details = snapshot["details"]
    generation = _require_generation(details.get("generation"), recovery=True)
    mode = details.get("mode")
    if mode not in {"new", "exchange"}:
        _raise_ambiguous("el journal no identifica el modo de publicacion")
    shadow = (
        _journal_shadow_path(
            details,
            final=final,
            job_id=job,
            generation=generation,
        )
        if mode == "exchange"
        else None
    )
    expected = _journal_details(job, generation, prepared, final, mode, shadow)
    _validate_journal_identity(snapshot, expected)

    final_marker = _read_marker(final)
    prepared_marker = _read_marker(prepared)
    shadow_marker = _read_marker(shadow) if shadow is not None else None
    final_is_new = _is_generation(final_marker, job, generation)
    prepared_is_new = _is_generation(prepared_marker, job, generation)
    shadow_is_new = _is_generation(shadow_marker, job, generation)
    state = snapshot["state"]
    candidate_signature: str | None = None
    prepared_signature: str | None = None
    base_signature: str | None = None
    candidate_identity: dict[str, int] | None = None
    if state in {"VERIFIED", "COMMITTING", "COMMITTED"}:
        candidate_signature = _required_signature(details, "candidate_signature")
        prepared_signature = _required_signature(details, "prepared_signature")
        candidate_identity = _required_root_identity(details, "candidate_identity")
        _required_cleanup_identities(details, mode)
        if mode == "exchange":
            base_signature = _required_signature(details, "base_signature")

    if state == "COMMITTED":
        if not final_is_new:
            _raise_ambiguous("COMMITTED sin la generacion esperada en biblioteca")
        if prepared_is_new or shadow_is_new:
            _raise_ambiguous("COMMITTED con dos copias de la generacion activa")
        assert candidate_identity is not None
        _verify_root_identity(final, candidate_identity, "biblioteca publicada")
        pending = _cleanup_committed_durable(
            journal,
            mode=mode,
            prepared=prepared,
            final=final,
            shadow=shadow,
            job_id=job,
            generation=generation,
        )
        return _result(
            job_id=job,
            generation=generation,
            mode=mode,
            final=final,
            recovered=True,
            cleanup_pending=pending,
        )

    if state == "ROLLED_BACK":
        if final_is_new:
            _raise_ambiguous("ROLLED_BACK pero la generacion esta publicada")
        if shadow is not None:
            _remove_owned_shadow(
                shadow,
                final=final,
                job_id=job,
                generation=generation,
                allow_unmarked=True,
            )
        else:
            _remove_owned_prepared_marker(
                prepared,
                job_id=job,
                generation=generation,
            )
        return {
            "status": "rolled_back",
            "job_id": job,
            "generation": generation,
            "mode": mode,
            "recovered": True,
        }

    if final_is_new and state != "COMMITTING":
        _raise_ambiguous(f"{state} pero la generacion ya esta en biblioteca")
    if state in {"PREPARED", "PROCESSING"}:
        return {
            "status": "resume_processing",
            "job_id": job,
            "generation": generation,
            "mode": mode,
        }
    if state == "VERIFIED":
        journal.transition("COMMITTING")
        state = "COMMITTING"
    if state != "COMMITTING":  # pragma: no cover - STATES lo hace defensivo
        _raise_ambiguous(f"estado de recuperacion no soportado: {state}")

    if mode == "new":
        assert candidate_signature is not None
        assert candidate_identity is not None
        if final_is_new:
            _verify_root_identity(final, candidate_identity, "biblioteca publicada")
            if prepared.exists():
                _raise_ambiguous("la generacion nueva existe en staging y biblioteca")
        elif prepared_is_new and not final.exists():
            _verify_tree_signature(prepared, candidate_signature, "candidato nuevo")
            _verify_root_identity(prepared, candidate_identity, "candidato nuevo")
            preflight_atomic_exchange(final.parent)
            _commit_new(prepared, final)
        else:
            _raise_ambiguous("NOREPLACE no puede determinar que raiz es la nueva")
    else:
        assert shadow is not None
        assert candidate_signature is not None
        assert prepared_signature is not None
        assert base_signature is not None
        assert candidate_identity is not None
        if final_is_new:
            if shadow_is_new:
                _raise_ambiguous("la generacion nueva aparece en ambos lados")
            _verify_root_identity(final, candidate_identity, "biblioteca publicada")
        elif shadow_is_new and final.is_dir():
            if not prepared.exists():
                _raise_ambiguous("falta prepared_series_root antes de EXCHANGE")
            _verify_tree_signature(
                prepared,
                prepared_signature,
                "prepared_series_root",
            )
            _verify_tree_signature(final, base_signature, "biblioteca base")
            _verify_tree_signature(shadow, candidate_signature, "candidato shadow")
            _verify_root_identity(shadow, candidate_identity, "candidato shadow")
            preflight_atomic_exchange(final.parent)
            _commit_exchange(shadow, final)
        else:
            _raise_ambiguous("EXCHANGE no puede determinar que lado es la sombra nueva")

    final_marker = _read_marker(final)
    if not _is_generation(final_marker, job, generation):
        _raise_ambiguous("el syscall termino sin el marcador nuevo en biblioteca")
    assert candidate_identity is not None
    _verify_root_identity(final, candidate_identity, "biblioteca publicada")
    _confirm_published_durable(
        mode=mode,
        prepared=prepared,
        final=final,
        shadow=shadow,
    )
    journal.transition("COMMITTED", recovered=True)
    pending = _cleanup_committed_durable(
        journal,
        mode=mode,
        prepared=prepared,
        final=final,
        shadow=shadow,
        job_id=job,
        generation=generation,
    )
    return _result(
        job_id=job,
        generation=generation,
        mode=mode,
        final=final,
        recovered=True,
        cleanup_pending=pending,
    )


def _canonical_paths_for_recovery(
    prepared_series_root: Path | str, final_series_root: Path | str
) -> tuple[Path, Path]:
    prepared_input = Path(prepared_series_root)
    if prepared_input.is_symlink():
        _raise_ambiguous("prepared_series_root no puede ser un enlace simbolico")
    prepared = prepared_input.resolve(strict=False)
    final_input = Path(final_series_root)
    if final_input.name in {"", ".", ".."}:
        _raise_ambiguous("final_series_root no tiene un nombre de serie valido")
    final_parent = final_input.parent.resolve(strict=True)
    final = final_parent / final_input.name
    if prepared == final or _is_within(prepared, final) or _is_within(final, prepared):
        raise DeliveryError("Staging y biblioteca deben ser arboles independientes")
    return prepared, final


def publish_series(
    job_id: str,
    prepared_series_root: Path | str,
    final_series_root: Path | str,
    journal: DurableJournal,
    *,
    expected_files: Iterable[Path | str] | None = None,
) -> dict[str, Any]:
    """Publica una raiz no vacia y valida exactamente ``expected_files``."""

    job = str(job_id).strip()
    if not job:
        raise ValueError("job_id no puede estar vacio")
    prepared, final = _canonical_paths(prepared_series_root, final_series_root)
    expected = _normalize_expected_files(expected_files)
    with _series_delivery_lock(final):
        _require_directory(prepared, "prepared_series_root")
        allow_marker = _prepared_marker_allowed_for_retry(
            job,
            prepared,
            final,
            journal,
        )
        _validate_prepared_contents(
            prepared,
            expected,
            allow_marker=allow_marker,
        )
        _validate_logical_tree(prepared)
        return _publish_series_locked(job, prepared, final, journal, expected)


def _publish_series_locked(
    job: str,
    prepared: Path,
    final: Path,
    journal: DurableJournal,
    expected_files: frozenset[str] | None,
) -> dict[str, Any]:
    try:
        snapshot = journal.snapshot()
    except JournalContradiction as exc:
        raise RecoveryAmbiguous("recovery_ambiguous: journal contradictorio") from exc

    if snapshot is not None and snapshot["state"] in {
        "VERIFIED",
        "COMMITTING",
        "COMMITTED",
        "ROLLED_BACK",
    }:
        return _recover_delivery_locked(job, prepared, final, journal)

    details = snapshot["details"] if snapshot is not None else {}
    if "generation" not in details:
        generation = uuid.uuid4().hex
    else:
        generation = _require_generation(details.get("generation"), recovery=True)
    mode = details.get("mode")
    if mode is None:
        mode = "exchange" if final.exists() else "new"
    if mode not in {"new", "exchange"}:
        _raise_ambiguous("modo de journal no valido")
    shadow = _shadow_path(final, job, generation) if mode == "exchange" else None
    identity = _journal_details(job, generation, prepared, final, mode, shadow)
    if snapshot is not None:
        _validate_journal_identity(snapshot, identity)

    # Completa la identidad de forma idempotente aunque lo haya abierto el caller.
    if snapshot is None or snapshot["state"] == "PREPARED":
        journal.transition("PREPARED", **identity)
    else:
        journal.transition("PROCESSING", **identity)
    if journal.state == "PREPARED":
        journal.transition("PROCESSING")

    atomic_done = False
    try:
        prepared_signature = _tree_signature(prepared)
        _validate_same_device(prepared, final)
        preflight = preflight_atomic_exchange(final.parent)
        if mode == "new":
            if final.exists():
                raise DeliveryConflict("La serie final aparecio antes de NOREPLACE")
            _write_marker(prepared, job, generation)
            _fsync_tree(prepared)
            fsync_directory(prepared.parent)
            prepared_signature = _tree_signature(prepared)
            candidate_signature = prepared_signature
            base_signature = None
            candidate_root = prepared
            cleanup_identities: dict[str, dict[str, int]] = {}
        else:
            if not final.is_dir() or final.is_symlink():
                raise DeliveryConflict("La serie existente ya no es una raiz valida")
            assert shadow is not None
            final_signature, prepared_signature, shadow_signature = _build_shadow(
                prepared,
                final,
                shadow,
                job_id=job,
                generation=generation,
            )
            base_signature = final_signature
            candidate_signature = shadow_signature
            candidate_root = shadow
            cleanup_identities = {
                "shadow": _cleanup_root_identity(final),
                "prepared": _cleanup_root_identity(prepared),
            }

        verification_details = {
            "preflight": preflight,
            "candidate_signature": _signature_digest(candidate_signature),
            "prepared_signature": _signature_digest(prepared_signature),
            "candidate_identity": _cleanup_root_identity(candidate_root),
            "cleanup_identities": cleanup_identities,
        }
        if base_signature is not None:
            verification_details["base_signature"] = _signature_digest(base_signature)
        journal.transition("VERIFIED", **verification_details)
        journal.transition("COMMITTING")
        _validate_prepared_contents(prepared, expected_files, allow_marker=mode == "new")
        if _tree_signature(prepared) != prepared_signature:
            raise DeliveryConflict("El arbol preparado cambio antes de publicar")
        if mode == "new":
            if final.exists():
                raise DeliveryConflict("La serie final aparecio antes de NOREPLACE")
            _commit_new(prepared, final)
        else:
            assert shadow is not None
            if _tree_signature(final) != final_signature:
                raise DeliveryConflict("La serie final cambio antes de EXCHANGE")
            if _tree_signature(shadow) != shadow_signature:
                raise DeliveryConflict("La sombra cambió antes de EXCHANGE")
            _commit_exchange(shadow, final)
        atomic_done = True

        if not _is_generation(_read_marker(final), job, generation):
            _raise_ambiguous("la raiz publicada no lleva el marcador esperado")
        _verify_root_identity(
            final,
            verification_details["candidate_identity"],
            "biblioteca publicada",
        )
        journal.transition("COMMITTED")
    except Exception as exc:
        # Un fsync puede fallar despues de que el syscall ya haya terminado. El
        # marcador, no una bandera en RAM, decide si hay que recuperar.
        published = _is_generation(_read_marker(final), job, generation)
        if atomic_done or published:
            # El marcador decide; nunca se hace un segundo intercambio a ciegas.
            return _recover_delivery_locked(job, prepared, final, journal)
        _rollback_before_exchange(
            journal,
            reason=exc,
            prepared=prepared,
            shadow=shadow,
            final=final,
            job_id=job,
            generation=generation,
        )
        raise

    pending = _cleanup_committed_durable(
        journal,
        mode=mode,
        prepared=prepared,
        final=final,
        shadow=shadow,
        job_id=job,
        generation=generation,
    )
    return _result(
        job_id=job,
        generation=generation,
        mode=mode,
        final=final,
        recovered=False,
        cleanup_pending=pending,
    )


__all__ = [
    "AtomicDeliveryUnsupported",
    "DeliveryConflict",
    "DeliveryError",
    "MARKER_NAME",
    "RENAME_EXCHANGE",
    "RENAME_NOREPLACE",
    "RecoveryAmbiguous",
    "preflight_atomic_exchange",
    "publish_series",
    "recover_delivery",
]
