import threading
import pytest

from media_worker import heavy_lock


def test_lock_is_noop_without_config(monkeypatch):
    monkeypatch.delenv(heavy_lock.LOCK_PATH_ENV, raising=False)
    with heavy_lock.media_heavy_lock() as state:
        assert state == {"enabled": False}


def test_same_path_serializes_and_times_out(tmp_path, monkeypatch):
    lock_path = tmp_path / "worker-locks" / "media-heavy.lock"
    monkeypatch.setenv(heavy_lock.LOCK_PATH_ENV, str(lock_path))
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with heavy_lock.media_heavy_lock(timeout_sec=1):
            entered.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=1)
    with pytest.raises(heavy_lock.HeavyLockTimeout):
        with heavy_lock.media_heavy_lock(timeout_sec=0.05, poll_sec=0.01):
            pass
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert lock_path.exists()
