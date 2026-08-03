from __future__ import annotations

import configparser
import json
import shutil
import zipfile
from pathlib import Path

import conftest as root_conftest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pytest_root_configuration_matches_arr_layout():
    parser = configparser.ConfigParser()
    parser.read(PROJECT_ROOT / "pytest.ini", encoding="utf-8")

    pytest_config = parser["pytest"]
    assert pytest_config["minversion"] == "8.4"
    assert "tests" in pytest_config["testpaths"]
    assert "services/arr-orchestrator/tests" in pytest_config["testpaths"]
    assert "services/buscador-puente-arr/tests" in pytest_config["testpaths"]
    assert "services/media-worker/tests" in pytest_config["testpaths"]
    assert "services/media-panel/tests" in pytest_config["testpaths"]
    assert pytest_config["python_files"] == "test_*.py"
    assert "-ra" in pytest_config["addopts"]
    assert "--basetemp" not in pytest_config["addopts"]
    assert "services/arr-orchestrator" in pytest_config["pythonpath"]
    assert "services/buscador-puente-arr" in pytest_config["pythonpath"]
    assert "services/media-worker" in pytest_config["pythonpath"]
    assert "services/media-panel" in pytest_config["pythonpath"]

    conftest = (PROJECT_ROOT / "conftest.py").read_text(encoding="utf-8")
    assert "PYTEST_SESSION_TOKEN" in conftest
    assert "config.option.basetemp = str(PYTEST_TEMP_DIR)" in conftest
    assert "PYTEST_FAILURE_KEEP = 5" in conftest
    assert "shutil.rmtree(PYTEST_SESSION_ROOT" in conftest


def test_pytest_runtime_is_local_and_failed_sessions_keep_only_five_zips(tmp_path):
    assert root_conftest.PYTEST_LOCAL_ROOT.name == "arr-pytest"
    assert PROJECT_ROOT not in root_conftest.PYTEST_SESSION_ROOT.parents
    assert root_conftest.PYTEST_FAILURE_DIR == (
        PROJECT_ROOT / "_codex_runtime" / "artifacts" / "pytest-failures"
    )

    source = tmp_path / "source"
    failure_dir = tmp_path / "failures"
    for index in range(7):
        source.mkdir(parents=True)
        (source / f"fallo-{index}.txt").write_text("fallo", encoding="utf-8")
        archive_path = root_conftest._archive_failed_session(
            source,
            failure_dir,
            keep=5,
        )
        assert archive_path is not None
        with zipfile.ZipFile(archive_path, "r") as archive:
            assert archive.testzip() is None
        shutil.rmtree(source)

    archives = sorted(failure_dir.glob("pytest-failure-*.zip"))
    assert len(archives) == 5


def test_requirements_dev_documents_service_scoped_dependencies():
    content = (PROJECT_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    assert "pytest>=8.4,<9" in content
    assert "watchdog==6.0.0" in content
    assert "guessit==3.8.0" in content
    assert "flask==3.0.3" in content
    assert "multi-servicio" in content
    assert "requests==2.32.5" in content


def test_orchestrator_contract_uses_isolated_blackbox_trace(isolated_arr_root):
    from arr_orchestrator.arr_blackbox import ArrBlackbox
    from arr_orchestrator.db import Database

    (isolated_arr_root / "config").mkdir(parents=True, exist_ok=True)
    blackbox = ArrBlackbox(isolated_arr_root / "diagnostics" / "arr")
    database = Database(isolated_arr_root / "config" / "orchestrator.db", event_recorder=blackbox.record_event)
    database.initialize()
    job = database.create_job(
        "pytest:movies:blackbox-root",
        "pytest",
        "movies",
        "Pelicula Pytest Root.mkv",
        state="waiting_stable",
    )
    database.transition(job["job_id"], "done", "cleanup", "Trabajo terminado")
    database.close()

    trace_dir = next((isolated_arr_root / "diagnostics" / "arr" / "jobs").glob("*/*"))
    summary = json.loads((trace_dir / "summary.json").read_text(encoding="utf-8"))

    assert summary["job_id"] == job["job_id"]
    assert summary["state"] == "done"
    assert summary["lifecycle"] == "final"
    assert summary["last_event"]["phase"] == "cleanup"
    assert str(trace_dir).startswith(str(isolated_arr_root))


def test_buscador_contract_uses_isolated_runtime(buscador_app_module, isolated_buscador_root):
    settings = buscador_app_module.copy_defaults()

    assert settings["rdt"]["fallback_enabled"] is True
    assert buscador_app_module.DATA_DIR == isolated_buscador_root / "data"
    assert buscador_app_module.LOG_DIR == isolated_buscador_root / "logs"
    assert str(buscador_app_module.DATA_DIR).startswith(str(isolated_buscador_root))
    assert str(buscador_app_module.ARR_DIAGNOSTICS_ROOT).startswith(str(isolated_buscador_root))
