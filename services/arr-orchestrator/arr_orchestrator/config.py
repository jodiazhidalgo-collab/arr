import os
from dataclasses import dataclass
from pathlib import Path


def _read_secret(env_name: str, file_env_name: str, default: str = "") -> str:
    file_name = os.environ.get(file_env_name, "").strip()
    if file_name:
        try:
            return Path(file_name).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return os.environ.get(env_name, default)


@dataclass(frozen=True)
class Config:
    mode: str
    config_dir: Path
    data_root: Path
    watch_inbox: Path
    processed_root: Path
    watch_error: Path
    event_dir: Path
    complete_root: Path
    workshop_root: Path
    movies_output: Path
    movies_final: Path
    tv_output: Path
    trailers_inbox: Path
    review_dir: Path
    media_worker_url: str
    callback_url: str
    media_reports_root: Path
    codex_diag_root: Path
    diagnostics_root: Path
    qbt_url: str
    qbt_user: str
    qbt_password: str
    rdt_url: str
    rdt_user: str
    rdt_password: str
    stable_seconds: int
    reconcile_seconds: int
    fallback_seconds: int
    health_port: int
    filebot_bin: str
    tmdb_api_token: str
    resolver_language: str
    resolver_region: str
    resolver_http_timeout_ms: int
    resolver_total_budget_ms: int
    resolver_retry_seconds: int

    @property
    def db_path(self) -> Path:
        return self.config_dir / "orchestrator.db"

    @property
    def log_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def active(self) -> bool:
        return self.mode == "active"

    @property
    def resolver_enabled(self) -> bool:
        return bool(self.tmdb_api_token.strip())

    @classmethod
    def from_env(cls) -> "Config":
        data_root = Path(os.environ.get("ARR_DATA_ROOT", "/data"))
        config_dir = Path(os.environ.get("ARR_CONFIG_DIR", "/config"))
        review_dir = Path(os.environ.get("ARR_REVIEW_DIR", str(data_root / "media/repetidas_vs_error")))
        return cls(
            mode=os.environ.get("ARR_MODE", "dry-run").strip().lower(),
            config_dir=config_dir,
            data_root=data_root,
            watch_inbox=data_root / "torrents/watch/inbox",
            processed_root=data_root / "torrents/watch/processed",
            watch_error=data_root / "torrents/watch/error",
            event_dir=data_root / "torrents/events/inbox/qbt",
            complete_root=data_root / "downloads/torrents/complete",
            workshop_root=Path(os.environ.get(
                "ARR_WORKSHOP_ROOT",
                str(data_root / "downloads/torrents/complete/taller"),
            )),
            movies_output=Path(os.environ.get(
                "ARR_MEDIA_AUTOMATION_INBOX",
                str(data_root / "downloads/torrents/complete/movies_automatizacion"),
            )),
            movies_final=Path(os.environ.get("ARR_MOVIES_FINAL", str(data_root / "media/movies"))),
            tv_output=Path(os.environ.get("ARR_TV_FINAL", str(data_root / "media/tv"))),
            trailers_inbox=Path(os.environ.get(
                "ARR_TRAILERS_INBOX",
                str(data_root / "downloads/torrents/complete/trailers_automatizacion"),
            )),
            review_dir=review_dir,
            media_worker_url=os.environ.get("MEDIA_WORKER_URL", "http://media-worker:8790"),
            callback_url=os.environ.get("ARR_CALLBACK_URL", "http://arr-orchestrator:8787").rstrip("/"),
            media_reports_root=config_dir / "media-worker",
            codex_diag_root=Path(os.environ.get("CODEX_DIAG_ROOT", "/diagnosticos_codex")),
            diagnostics_root=Path(os.environ.get("ARR_DIAGNOSTICS_ROOT", "/diagnostics/arr")),
            qbt_url=os.environ.get("QBT_URL", "http://gluetun:8080").rstrip("/"),
            qbt_user=os.environ.get("QBT_USER", "admin"),
            qbt_password=_read_secret("QBT_PASSWORD", "QBT_PASSWORD_FILE"),
            rdt_url=os.environ.get("RDT_URL", "http://rdtclient:6500").rstrip("/"),
            rdt_user=os.environ.get("RDT_USER", "admin"),
            rdt_password=_read_secret("RDT_PASSWORD", "RDT_PASSWORD_FILE"),
            stable_seconds=int(os.environ.get("ARR_STABLE_SECONDS", "8")),
            reconcile_seconds=int(os.environ.get("ARR_RECONCILE_SECONDS", "30")),
            fallback_seconds=int(os.environ.get("ARR_RDT_FALLBACK_SECONDS", "5400")),
            health_port=int(os.environ.get("ARR_HEALTH_PORT", "8787")),
            filebot_bin=os.environ.get("FILEBOT_BIN", "/opt/filebot/filebot"),
            tmdb_api_token=_read_secret("TMDB_API_TOKEN", "TMDB_API_TOKEN_FILE"),
            resolver_language=os.environ.get("ARR_RESOLVER_LANGUAGE", "es-ES"),
            resolver_region=os.environ.get("ARR_RESOLVER_REGION", "ES"),
            resolver_http_timeout_ms=int(
                os.environ.get("ARR_RESOLVER_HTTP_TIMEOUT_MS", "2500")
            ),
            resolver_total_budget_ms=int(
                os.environ.get("ARR_RESOLVER_TOTAL_BUDGET_MS", "5000")
            ),
            resolver_retry_seconds=int(
                os.environ.get("ARR_RESOLVER_RETRY_SECONDS", "60")
            ),
        )

    def ensure_directories(self) -> None:
        directories = [
            self.config_dir,
            self.log_dir,
            self.event_dir,
            self.workshop_root,
            self.review_dir,
            self.movies_output,
            self.movies_final,
            self.trailers_inbox,
            self.media_reports_root,
            self.codex_diag_root,
            self.diagnostics_root,
        ]
        for category in ("movies", "tv", "manual", "movies_automatizacion", "trailers_automatizacion"):
            directories.extend(
                [
                    self.watch_inbox / category,
                    self.processed_root / "rd",
                    self.processed_root / "qbit",
                    self.complete_root / category,
                ]
            )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
