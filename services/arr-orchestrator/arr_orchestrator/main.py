import logging
import signal
import sys

from .arr_blackbox import ArrBlackbox
from .arr_follow import build_follow_payload
from .config import Config
from .codex_diagnostics import create_codex_diagnostic
from .db import Database
from .engine import Engine
from .health import start_health_server


def configure_logging(config: Config) -> None:
    log_file = config.log_dir / "orchestrator.log"
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.handlers[:] = [stream, file_handler]


def main() -> int:
    config = Config.from_env()
    if config.mode not in ("dry-run", "active"):
        raise SystemExit("ARR_MODE debe ser dry-run o active")
    config.ensure_directories()
    configure_logging(config)
    blackbox = ArrBlackbox(config.diagnostics_root)
    database = Database(config.db_path, event_recorder=blackbox.record_event)
    database.initialize()
    engine = Engine(config, database)
    follow = lambda job_id: build_follow_payload(database.job_detail(job_id), config.diagnostics_root)
    diagnostic = lambda job_id, force=False: create_codex_diagnostic(
        database,
        job_id,
        config.codex_diag_root,
        engine._diagnostic_status(),
        force=force,
        diagnostics_root=config.diagnostics_root,
    )
    health = start_health_server(
        config.health_port,
        engine.status,
        lambda: database.latest_jobs(100),
        database.job_detail,
        database.add_event,
        follow,
        diagnostic,
        engine.watcher_rules,
        engine.update_watcher_rules,
    )

    def stop(_signum: int, _frame: object) -> None:
        engine.stop()
        health.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        engine.start()
    finally:
        engine.stop()
        health.shutdown()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
