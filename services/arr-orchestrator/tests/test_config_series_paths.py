from pathlib import Path

from arr_orchestrator.config import Config


def test_series_default_paths_follow_the_configured_isolated_roots() -> None:
    config = Config(
        mode="dry-run",
        config_dir=Path("/isolated/config"),
        data_root=Path("/isolated/data"),
        watch_inbox=Path("/isolated/watch/inbox"),
        processed_root=Path("/isolated/watch/processed"),
        watch_error=Path("/isolated/watch/error"),
        event_dir=Path("/isolated/events"),
        complete_root=Path("/isolated/complete"),
        workshop_root=Path("/isolated/complete/taller"),
        movies_final=Path("/isolated/media/movies"),
        tv_output=Path("/isolated/media/tv"),
        trailers_inbox=Path("/isolated/complete/trailers_automatizacion"),
        review_dir=Path("/isolated/media/repetidas_vs_error"),
        media_worker_url="http://media-worker:8790",
        callback_url="http://arr-orchestrator:8787",
        media_reports_root=Path("/isolated/config/media-worker"),
        codex_diag_root=Path("/isolated/diagnosticos_codex"),
        diagnostics_root=Path("/isolated/diagnostics/arr"),
        qbt_url="http://qbittorrent:8080",
        qbt_user="admin",
        qbt_password="",
        rdt_url="http://rdtclient:6500",
        rdt_user="admin",
        rdt_password="",
        stable_seconds=1,
        reconcile_seconds=30,
        fallback_seconds=5400,
        health_port=8787,
        filebot_bin="filebot",
        tmdb_api_token="",
        resolver_language="es-ES",
        resolver_region="ES",
        resolver_http_timeout_ms=2500,
        resolver_total_budget_ms=5000,
        resolver_retry_seconds=60,
    )

    assert config.series_reports_root == Path("/isolated/config/series-worker")
    assert config.series_review_dir == Path(
        "/isolated/data/media/repetidas_vs_error"
    )
