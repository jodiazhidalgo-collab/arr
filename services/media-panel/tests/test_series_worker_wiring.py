from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _compose_service(name: str, next_name: str) -> str:
    compose = (PROJECT_ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    start = compose.index(f"\n  {name}:\n") + len(f"\n  {name}:\n")
    end = compose.index(f"\n  {next_name}:\n", start)
    return compose[start:end]


def test_series_worker_is_wired_to_the_orchestrator_in_active_mode() -> None:
    orchestrator = _compose_service("arr-orchestrator", "media-worker")
    series_worker = _compose_service("series-worker", "media-panel")

    assert "series-worker:\n        condition: service_started" in orchestrator
    assert 'SERIES_WORKER_URL: "http://series-worker:8791"' in orchestrator
    assert 'SERIES_WORKER_REPORT_ROOT: "/config/series-worker"' in orchestrator
    assert 'ARR_SERIES_REVIEW_DIR: "/data/media/repetidas_vs_error"' in orchestrator
    assert "- ${ARR_ROOT}/config/series-worker:/config/series-worker:ro" in orchestrator
    assert 'ARR_SERIES_MODE: "active"' in orchestrator
    assert "ARR_ORCHESTRATOR_URL" not in series_worker
    assert 'SERIES_WORKER_CALLBACK_ORIGIN: "http://arr-orchestrator:8787"' in series_worker
    assert "ports:" not in series_worker
    assert 'expose:\n      - "8791"' in series_worker


def test_series_worker_uses_only_the_approved_mounts_and_paths() -> None:
    series_worker = _compose_service("series-worker", "media-panel")
    mounts = {
        line.strip()
        for line in series_worker.splitlines()
        if line.strip().startswith("- ${")
    }

    assert mounts == {
        "- ${ARR_DATA_ROOT}:/data",
        "- ${ARR_ROOT}/config/series-worker:/config/series-worker",
        "- ${ARR_ROOT}/config/series-rules:/config/series-rules",
        "- ${ARR_ROOT}/config/worker-locks:/config/worker-locks",
        "- ${ARR_ROOT}/config/media-rules/reglas_motor.json:/seed/reglas_motor.json:ro",
    }
    for expected in (
        'SERIES_WORKER_PORT: "8791"',
        'SERIES_WORKER_RULES_PATH: "/config/series-rules/reglas_series.json"',
        'SERIES_WORKER_SEED_RULES_PATH: "/seed/reglas_motor.json"',
        'SERIES_WORKER_REPORT_ROOT: "/config/series-worker"',
        'SERIES_WORKER_REVIEW_ROOT: "/data/media/repetidas_vs_error"',
        'SERIES_WORKER_ALLOWED_ROOTS: "/data/downloads/torrents/complete/taller"',
        'SERIES_HEAVY_LOCK_PATH: "/config/worker-locks/media-heavy.lock"',
        'SERIES_HEAVY_LOCK_TIMEOUT_SEC: "3600"',
    ):
        assert expected in series_worker
    assert "read_only: true" in series_worker
    assert "- ALL" in series_worker
    assert "- no-new-privileges:true" in series_worker
    assert "/tmp:rw,noexec,nosuid,nodev,size=512m,uid=1000,gid=10" in series_worker


def test_panel_and_pytest_are_wired_to_series_worker() -> None:
    panel = _compose_service("media-panel", "buscador-puente-arr")
    pytest_ini = (PROJECT_ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert "- series-worker" in panel
    assert 'SERIES_WORKER_URL: "http://series-worker:8791"' in panel
    assert 'SERIES_REVIEW_DIR: "/data/media/repetidas_vs_error"' in panel
    assert "services/series-worker/tests" in pytest_ini
    assert "services/series-worker" in pytest_ini


def test_series_worker_never_imports_media_worker() -> None:
    source_root = PROJECT_ROOT / "services/series-worker/series_worker"
    for source in source_root.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "media_worker" not in text, source
