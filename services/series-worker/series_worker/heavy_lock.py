"""Semaforo audiovisual compartido para el worker de series.

En Linux se bloquea el inode estable con ``flock``. El fallback por hilo existe
solo para que desarrollo y tests funcionen en Windows; el contenedor real usa
siempre ``flock`` y nunca borra ni recrea el lockfile.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # pragma: no cover - se ejerce en el contenedor Linux
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None


LOCK_PATH_ENV = "SERIES_HEAVY_LOCK_PATH"
LOCK_TIMEOUT_ENV = "SERIES_HEAVY_LOCK_TIMEOUT_SEC"
DEFAULT_TIMEOUT_SEC = 14_400.0

_WINDOWS_GUARD = threading.Lock()
_WINDOWS_LOCKS: dict[str, threading.Lock] = {}


class HeavyLockTimeout(TimeoutError):
    """El motor pesado no quedo libre dentro del plazo solicitado."""


def _timeout(value: float | None) -> float:
    if value is not None:
        return max(0.0, float(value))
    raw = str(os.environ.get(LOCK_TIMEOUT_ENV, DEFAULT_TIMEOUT_SEC)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


def _windows_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _WINDOWS_GUARD:
        return _WINDOWS_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def series_heavy_lock(
    path: Path | str | None = None,
    *,
    timeout_sec: float | None = None,
    poll_sec: float = 0.1,
) -> Iterator[dict[str, object]]:
    """Adquiere el lock pesado configurado por ``SERIES_HEAVY_LOCK_PATH``."""

    configured = str(path or os.environ.get(LOCK_PATH_ENV, "")).strip()
    if not configured:
        yield {"enabled": False}
        return

    lock_path = Path(configured)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Abrir en append crea una vez y conserva el mismo inode entre ejecuciones.
    handle = lock_path.open("a+b")
    deadline = time.monotonic() + _timeout(timeout_sec)
    poll = max(0.01, float(poll_sec))

    if _fcntl is None:
        handle.close()
        lock = _windows_lock(lock_path)
        remaining = max(0.0, deadline - time.monotonic())
        if not lock.acquire(timeout=remaining):
            raise HeavyLockTimeout("El motor audiovisual compartido sigue ocupado.")
        try:
            yield {"enabled": True, "path": str(lock_path), "backend": "thread"}
        finally:
            lock.release()
        return

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


__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "HeavyLockTimeout",
    "LOCK_PATH_ENV",
    "LOCK_TIMEOUT_ENV",
    "series_heavy_lock",
]
