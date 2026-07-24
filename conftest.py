from __future__ import annotations

import importlib
import logging
import os
import shutil
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent
ORCHESTRATOR_DIR = PROJECT_ROOT / "services" / "arr-orchestrator"
BUSCADOR_DIR = PROJECT_ROOT / "services" / "buscador-puente-arr"
PYTEST_DATA_DIR = PROJECT_ROOT / "_codex_runtime" / "test-data" / "pytest-session"
ARR_DATA_DIR = PYTEST_DATA_DIR / "arr"
BUSCADOR_DATA_DIR = PYTEST_DATA_DIR / "buscador"


def _remove_pytest_session_data() -> None:
    session_root = PYTEST_DATA_DIR.resolve()
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
    shutil.rmtree(PYTEST_DATA_DIR, ignore_errors=True)


def pytest_sessionfinish(session, exitstatus) -> None:
    if exitstatus == pytest.ExitCode.OK:
        _remove_pytest_session_data()


for path in (ORCHESTRATOR_DIR, BUSCADOR_DIR):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)


def _setdefault_path(name: str, path: Path) -> None:
    os.environ.setdefault(name, str(path))
    path.mkdir(parents=True, exist_ok=True)


os.environ.setdefault("ARR_MODE", "dry-run")
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
