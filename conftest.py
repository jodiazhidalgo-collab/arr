from __future__ import annotations

import importlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent
ORCHESTRATOR_DIR = PROJECT_ROOT / "services" / "arr-orchestrator"
BUSCADOR_DIR = PROJECT_ROOT / "services" / "buscador-puente-arr"
PYTEST_SESSION_TOKEN = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
PYTEST_LOCAL_ROOT = Path(
    os.environ.get(
        "ARR_PYTEST_LOCAL_ROOT",
        str(Path(tempfile.gettempdir()) / "arr-pytest"),
    )
)
PYTEST_SESSION_ROOT = PYTEST_LOCAL_ROOT / f"pytest-{PYTEST_SESSION_TOKEN}"
PYTEST_TEMP_DIR = PYTEST_SESSION_ROOT / "tmp"
PYTEST_DATA_DIR = PYTEST_SESSION_ROOT / "test-data"
PYTEST_FAILURE_DIR = (
    PROJECT_ROOT / "_codex_runtime" / "artifacts" / "pytest-failures"
)
PYTEST_FAILURE_KEEP = 5
ARR_DATA_DIR = PYTEST_DATA_DIR / "arr"
BUSCADOR_DATA_DIR = PYTEST_DATA_DIR / "buscador"


def _close_pytest_session_handlers() -> None:
    session_root = PYTEST_SESSION_ROOT.resolve()
    for logger_object in tuple(logging.Logger.manager.loggerDict.values()):
        if not isinstance(logger_object, logging.Logger):
            continue
        for handler in tuple(logger_object.handlers):
            filename = getattr(handler, "baseFilename", None)
            if not filename:
                continue
            try:
                Path(filename).resolve().relative_to(session_root)
            except (OSError, ValueError):
                continue
            logger_object.removeHandler(handler)
            handler.close()


def _archive_failed_session(
    session_root: Path = PYTEST_SESSION_ROOT,
    failure_dir: Path = PYTEST_FAILURE_DIR,
    keep: int = PYTEST_FAILURE_KEEP,
) -> Path | None:
    if not session_root.exists():
        return None
    failure_dir.mkdir(parents=True, exist_ok=True)
    archive_path = failure_dir / (
        f"pytest-failure-{time.strftime('%Y%m%d_%H%M%S')}-{time.time_ns()}-"
        f"{PYTEST_SESSION_TOKEN}.zip"
    )
    temporary_archive = archive_path.with_suffix(".tmp")
    with zipfile.ZipFile(
        temporary_archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
    ) as archive:
        for path in sorted(session_root.rglob("*")):
            relative = path.relative_to(session_root).as_posix()
            if path.is_dir():
                archive.writestr(f"{relative}/", b"")
            elif path.is_file():
                archive.write(path, relative)
    with zipfile.ZipFile(temporary_archive, "r") as archive:
        if archive.testzip() is not None:
            raise RuntimeError("El ZIP de evidencia pytest no es valido")
    temporary_archive.replace(archive_path)
    failure_zips = sorted(
        failure_dir.glob("pytest-failure-*.zip"),
        key=lambda path: path.name,
        reverse=True,
    )
    for old_archive in failure_zips[max(1, keep) :]:
        old_archive.unlink(missing_ok=True)
    return archive_path


def _schedule_session_cleanup(session_root: Path = PYTEST_SESSION_ROOT) -> None:
    cleanup_code = (
        "import pathlib, shutil, sys, time; "
        "root = pathlib.Path(sys.argv[1]); "
        "time.sleep(1); "
        "[(shutil.rmtree(root, ignore_errors=True), time.sleep(1)) "
        "for _ in range(60) if root.exists()]"
    )
    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        )
    else:
        options["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, "-B", "-c", cleanup_code, str(session_root)],
        **options,
    )


def pytest_sessionfinish(session, exitstatus) -> None:
    _close_pytest_session_handlers()
    try:
        if exitstatus != pytest.ExitCode.OK:
            _archive_failed_session()
    finally:
        shutil.rmtree(PYTEST_SESSION_ROOT, ignore_errors=True)
        if PYTEST_SESSION_ROOT.exists():
            _schedule_session_cleanup()


def pytest_configure(config) -> None:
    """Aísla cada sesión en el disco local, fuera del recurso SMB."""

    PYTEST_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(PYTEST_TEMP_DIR)
    config.option.basetemp = str(PYTEST_TEMP_DIR)


for path in (ORCHESTRATOR_DIR, BUSCADOR_DIR):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)


def _setdefault_path(name: str, path: Path) -> None:
    os.environ.setdefault(name, str(path))
    path.mkdir(parents=True, exist_ok=True)


os.environ.setdefault("ARR_MODE", "dry-run")
os.environ["ARR_PYTEST_SESSION_ROOT"] = str(PYTEST_SESSION_ROOT)
os.environ["ARR_PYTEST_TEMP_DIR"] = str(PYTEST_TEMP_DIR)
os.environ["ARR_PYTEST_DATA_DIR"] = str(PYTEST_DATA_DIR)
_setdefault_path("ARR_CONFIG_DIR", ARR_DATA_DIR / "config")
_setdefault_path("ARR_DATA_ROOT", ARR_DATA_DIR / "data")
_setdefault_path("CODEX_DIAG_ROOT", ARR_DATA_DIR / "diagnosticos_codex")
_setdefault_path("ARR_DIAGNOSTICS_ROOT", ARR_DATA_DIR / "diagnostics" / "arr")
_setdefault_path("DATA_DIR", BUSCADOR_DATA_DIR / "data")
_setdefault_path("LOG_DIR", BUSCADOR_DATA_DIR / "logs")
_setdefault_path("ARR_DIAGNOSTICS_ROOT", BUSCADOR_DATA_DIR / "diagnostics" / "arr")

BUSCADOR_DATA_SENSITIVE_MODULES = (
    "modulos.arr_trace",
    "modulos.persistent_jobs",
    "modulos.submission_store",
    "app",
)


def reload_module_stack(*module_names: str) -> None:
    importlib.invalidate_caches()
    for module_name in module_names:
        module = sys.modules.get(module_name)
        if module is not None:
            importlib.reload(module)


@pytest.fixture
def arr_pytest_data_dir() -> Path:
    PYTEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return PYTEST_DATA_DIR


@pytest.fixture
def isolated_arr_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "arr"
    monkeypatch.setenv("ARR_MODE", "dry-run")
    monkeypatch.setenv("ARR_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("ARR_DATA_ROOT", str(root / "data"))
    monkeypatch.setenv("CODEX_DIAG_ROOT", str(root / "diagnosticos_codex"))
    monkeypatch.setenv("ARR_DIAGNOSTICS_ROOT", str(root / "diagnostics" / "arr"))
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def isolated_buscador_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "buscador"
    monkeypatch.setenv("DATA_DIR", str(root / "data"))
    monkeypatch.setenv("LOG_DIR", str(root / "logs"))
    monkeypatch.setenv("ARR_DIAGNOSTICS_ROOT", str(root / "diagnostics" / "arr"))
    root.mkdir(parents=True, exist_ok=True)
    reload_module_stack(*BUSCADOR_DATA_SENSITIVE_MODULES)
    return root


@pytest.fixture
def buscador_app_module(isolated_buscador_root):
    reload_module_stack(*BUSCADOR_DATA_SENSITIVE_MODULES)
    return importlib.import_module("app")
