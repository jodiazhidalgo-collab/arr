"""Semaforo pesado entre workers mediante un lockfile neutral.

El fichero vive fuera de los arboles de trabajo/publicacion y nunca se borra.
En Linux se usa ``flock``; el fallback solo mantiene la suite portable en
Windows y no sustituye la garantia entre contenedores.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

try:  # pragma: no cover - la rama real se ejerce dentro del contenedor Linux
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None


LOCK_PATH_ENV = "MEDIA_HEAVY_LOCK_PATH"
LOCK_TIMEOUT_ENV = "MEDIA_HEAVY_LOCK_TIMEOUT_SEC"
DEFAULT_TIMEOUT_SEC = 14_400.0
_FALLBACK_GUARD = threading.Lock()
_FALLBACK_LOCKS: dict[str, threading.Lock] = {}


class HeavyLockTimeout(TimeoutError):
    """El turno pesado no quedo disponible dentro del plazo configurado."""


def _timeout(value: Optional[float]) -> float:
    if value is not None:
        return max(0.0, float(value))
    raw = str(os.environ.get(LOCK_TIMEOUT_ENV, DEFAULT_TIMEOUT_SEC)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


def _fallback_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _FALLBACK_GUARD:
        return _FALLBACK_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def media_heavy_lock(
    path: Optional[Path | str] = None,
    *,
    timeout_sec: Optional[float] = None,
    poll_sec: float = 0.1,
) -> Iterator[dict[str, object]]:
    """Adquiere el turno audiovisual compartido.

    Sin variable configurada conserva compatibilidad en desarrollo. Compose
    siempre la configura para que peliculas y series compartan el mismo inode.
    """

    configured = str(path or os.environ.get(LOCK_PATH_ENV, "")).strip()
    if not configured:
        yield {"enabled": False}
        return

    lock_path = Path(configured)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    deadline = time.monotonic() + _timeout(timeout_sec)
    poll = max(0.01, float(poll_sec))

    if _fcntl is None:
        lock = _fallback_lock(lock_path)
        remaining = max(0.0, deadline - time.monotonic())
        if not lock.acquire(timeout=remaining):
            raise HeavyLockTimeout("El motor audiovisual compartido sigue ocupado.")
        try:
            yield {"enabled": True, "path": str(lock_path), "backend": "thread"}
        finally:
            lock.release()
        return

    handle = lock_path.open("a+b")
    acquired = False
    try:
        while True:
            try:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HeavyLockTimeout(
                        "El motor audiovisual compartido sigue ocupado."
                    )
                time.sleep(poll)
        yield {"enabled": True, "path": str(lock_path), "backend": "flock"}
    finally:
        if acquired:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        handle.close()


__all__ = ["HeavyLockTimeout", "media_heavy_lock"]
