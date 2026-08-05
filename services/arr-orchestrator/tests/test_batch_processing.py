import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from arr_orchestrator.batch_coordinator import validate_episode_intent
from arr_orchestrator.batch_preparer import (
    clean_and_plan_batch,
    materialize_item,
)
from arr_orchestrator.config import Config
from arr_orchestrator.db import Database
from arr_orchestrator.engine import Engine
from arr_orchestrator.name_resolver import ResolvedIdentity


def _config(root: Path, *, series_mode: str = "active") -> Config:
    data = root / "data"
    complete = data / "downloads" / "torrents" / "complete"
    config = Config(
        mode="active",
        config_dir=root / "config",
        data_root=data,
        watch_inbox=data / "torrents" / "watch" / "inbox",
        processed_root=data / "torrents" / "watch" / "processed",
        watch_error=data / "torrents" / "watch" / "error",
        event_dir=data / "torrents" / "events" / "inbox" / "qbt",
        complete_root=complete,
        workshop_root=complete / "taller",
        movies_final=data / "media" / "movies",
        tv_output=data / "media" / "tv",
        trailers_inbox=complete / "trailers_automatizacion",
        review_dir=data / "media" / "repetidas_vs_error",
        media_worker_url="http://media-worker:8790",
        callback_url="http://arr-orchestrator:8787",
        media_reports_root=root / "config" / "media-worker",
        codex_diag_root=root / "diagnosticos_codex",
        diagnostics_root=root / "diagnostics" / "arr",
        qbt_url="http://qbittorrent:8080",
        qbt_user="admin",
        qbt_password="",
        rdt_url="http://rdtclient:6500",
        rdt_user="admin",
        rdt_password="",
        stable_seconds=8,
        reconcile_seconds=30,
        fallback_seconds=5400,
        health_port=8787,
        filebot_bin="filebot",
        tmdb_api_token="test",
        resolver_language="es-ES",
        resolver_region="ES",
        resolver_http_timeout_ms=2500,
        resolver_total_budget_ms=20000,
        resolver_retry_seconds=60,
        series_mode=series_mode,
    )
    config.ensure_directories()
    return config


def _identity(*, season: int = 4, episodes=range(1, 11)) -> ResolvedIdentity:
    episode_values = list(episodes)
    return ResolvedIdentity(
        media_type="tv",
        tmdb_id=1399,
        title="Juego de Tronos",
        original_title="Game of Thrones",
        year=2011,
        aliases=["Juego de Tronos", "Game of Thrones"],
        query="Juego de Tronos",
        guess={"title": "Juego de Tronos", "season": season},
        source="test",
        season=season,
        episodes=episode_values,
        season_count=8,
        season_episode_counts={season: 10},
        known_episodes={season: list(range(1, 11))},
        resolver_algorithm_version="phased-er-v2",
        decision_status="ACCEPTED_CONFIDENT",
    )


class _Resolver:
    enabled = True

    def __init__(self, identity: ResolvedIdentity) -> None:
        self.identity = identity
        self.calls = 0
        self.deferred = []

    def configure_rules(self, _rules) -> None:
        return None

    def resolve(self, _job, _root, *, defer_episode_conflicts=False):
        self.calls += 1
        self.deferred.append(bool(defer_episode_conflicts))
        return self.identity

    def trace_snapshot(self):
        return {
            "resolver_algorithm_version": "phased-er-v2",
            "decision": {
                "status": "ACCEPTED_CONFIDENT",
                "accepted": True,
                "selected_tmdb_id": self.identity.tmdb_id,
            },
        }

    def output_matches(self, _identity, _names):
        return True


def _batch_parent(tmp_path: Path, names: list[str]):
    config = _config(tmp_path)
    database = Database(tmp_path / "config" / "orchestrator.db")
    database.initialize()
    engine = Engine(config, database)
    job = database.create_job(
        "batch-parent-test",
        "fs",
        "tv",
        "Juego de Tronos - Temporada 4 COMPLETA",
        state="ready_extract",
        source_meta_json=engine._new_job_source_meta_json(
            category="tv",
            name="Juego de Tronos - Temporada 4 COMPLETA",
        ),
    )
    job_root = config.workshop_root / str(job["job_id"])
    original = job_root / "original" / "Juego de Tronos T4"
    original.mkdir(parents=True)
    for name in names:
        (original / name).write_bytes(b"episode")
    database.update_job(
        str(job["job_id"]),
        stage_path=str(job_root),
        source_path=str(original),
    )
    return engine, database, database.get_job(str(job["job_id"]))


def test_tv_cleanup_keeps_only_ten_videos_without_visible_preparation(tmp_path: Path):
    root = tmp_path / "input"
    nested = root / "season"
    nested.mkdir(parents=True)
    for episode in range(1, 11):
        (nested / f"Juego de Tronos S04E{episode:02d}.mkv").write_bytes(b"video")
    for junk in ("info.txt", "web.url", "subtitulo.srt", "metadata.nfo"):
        (nested / junk).write_text("basura", encoding="utf-8")

    plan = clean_and_plan_batch(root, "tv")

    assert len(plan.items) == 10
    assert plan.removed_non_video == 4
    assert not list(root.rglob("*.srt"))
    assert not list(root.rglob(".preparacion"))


def test_movies_discard_auxiliary_and_keep_multipart_together(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    for name in (
        "Pelicula CD1.mkv",
        "Pelicula CD2.mkv",
        "Otra pelicula.mkv",
        "Pelicula sample.mkv",
        "trailer.mp4",
    ):
        (root / name).write_bytes(b"movie")

    plan = clean_and_plan_batch(root, "movies")

    assert len(plan.items) == 2
    assert sorted(item.kind for item in plan.items) == ["multipart", "video"]
    assert len(next(item for item in plan.items if item.kind == "multipart").sources) == 2
    assert not (root / "Pelicula sample.mkv").exists()
    assert not (root / "trailer.mp4").exists()


def test_single_movie_and_double_episode_remain_single_jobs(tmp_path: Path):
    movie_root = tmp_path / "movie"
    tv_root = tmp_path / "tv"
    movie_root.mkdir()
    tv_root.mkdir()
    (movie_root / "Obsession 2025.mkv").write_bytes(b"movie")
    (tv_root / "Serie S01E01E02.mkv").write_bytes(b"episode")

    assert clean_and_plan_batch(movie_root, "movies").should_split is False
    assert clean_and_plan_batch(tv_root, "tv").should_split is False


def test_bluray_structure_is_the_only_non_video_exception(tmp_path: Path):
    root = tmp_path / "movies"
    playlist = root / "Disco" / "BDMV" / "PLAYLIST"
    stream = root / "Disco" / "BDMV" / "STREAM"
    playlist.mkdir(parents=True)
    stream.mkdir(parents=True)
    (playlist / "00001.mpls").write_bytes(b"playlist")
    (stream / "00001.m2ts").write_bytes(b"video")
    (root / "basura.txt").write_text("basura", encoding="utf-8")

    plan = clean_and_plan_batch(root, "movies")

    assert len(plan.items) == 1
    assert plan.items[0].kind == "bluray"
    assert (playlist / "00001.mpls").exists()
    assert not (root / "basura.txt").exists()


def test_materialization_is_idempotent_after_restart(tmp_path: Path):
    root = tmp_path / "input"
    root.mkdir()
    source = root / "Serie S01E01.mkv"
    source.write_bytes(b"episode")
    item = clean_and_plan_batch(root, "tv").items[0]
    child_root = tmp_path / "taller" / "child"

    first = materialize_item(item, root, child_root)
    second = materialize_item(item, root, child_root)

    assert first == second
    assert first.read_bytes() == b"episode"
    assert len(list(child_root.rglob("*.mkv"))) == 1


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ({"season": 4, "episodes": [7]}, "AGREE"),
        ({"season": 4, "episodes": [99]}, "DISAGREE"),
        ({"season": 5, "episodes": [1]}, "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_episode_validation_only_blocks_confirmed_contradictions(intent, expected):
    identity = _identity()
    identity.season_episode_counts = {4: 10}
    identity.known_episodes = {4: list(range(1, 11))}
    identity.season_count = None
    assert validate_episode_intent(identity, intent, {})[0] == expected


def test_special_absolute_double_pack_and_unknown_episode_intents():
    identity = _identity()
    identity.season_episode_counts = {0: 3, 4: 10}
    identity.known_episodes = {0: [1, 2, 3], 4: list(range(1, 11))}

    cases = (
        ({"season": 0, "episodes": [2], "is_special": True}, "AGREE"),
        ({"absolute_episode": 8}, "AGREE"),
        ({"season": 4, "episodes": [1, 2]}, "AGREE"),
        ({"season": 4, "episodes": [], "is_season_pack": True}, "AGREE"),
        (None, "UNKNOWN"),
    )
    assert [validate_episode_intent(identity, intent, {})[0] for intent, _ in cases] == [
        expected for _, expected in cases
    ]


def test_game_of_thrones_t4_creates_ten_children_with_one_tmdb_resolution(tmp_path: Path):
    names = [f"Juego de Tronos S04E{episode:02d} 1080p.mkv" for episode in range(1, 11)]
    engine, database, parent = _batch_parent(tmp_path, names)
    resolver = _Resolver(_identity())
    engine.identity.resolver = resolver
    engine.name_resolver = resolver

    engine._run_extract(parent)
    parent = database.get_job(str(parent["job_id"]))
    assert parent["state"] == "ready_filebot"
    engine._run_filebot(parent)
    parent = database.get_job(str(parent["job_id"]))
    assert parent["state"] == "batch_preparing"
    engine._run_batch_prepare(parent)

    children = database.children_for_parent(str(parent["job_id"]))
    assert resolver.calls == 1
    assert resolver.deferred == [True]
    assert len(children) == 10
    assert all(child["state"] == "ready_filebot" for child in children)
    assert all(json.loads(child["identity_json"])["tmdb_id"] == 1399 for child in children)
    assert sorted(child["batch_index"] for child in children) == list(range(1, 11))

    engine._run_batch_prepare(database.get_job(str(parent["job_id"])))
    assert len(database.children_for_parent(str(parent["job_id"]))) == 10
    database.close()


def test_only_nonexistent_episode_child_goes_to_review(tmp_path: Path):
    names = ["Juego de Tronos S04E01.mkv", "Juego de Tronos S04E99.mkv"]
    engine, database, parent = _batch_parent(tmp_path, names)
    resolver = _Resolver(_identity())
    engine.identity.resolver = resolver
    engine.name_resolver = resolver

    engine._run_extract(parent)
    parent = database.get_job(str(parent["job_id"]))
    engine._run_filebot(parent)
    parent = database.get_job(str(parent["job_id"]))
    engine._run_batch_prepare(parent)

    children = database.children_for_parent(str(parent["job_id"]))
    assert [child["state"] for child in children] == ["ready_filebot", "manual_review"]
    assert children[1]["last_error_code"] == "series_episode_not_found"
    assert children[0]["stage_path"].startswith(str(engine.config.workshop_root))
    database.close()


def test_api_groups_parent_and_child_progress(tmp_path: Path):
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    parent = database.create_job(
        "parent-api",
        "fs",
        "tv",
        "Temporada",
        state="batch_waiting_children",
        source_meta_json=json.dumps(
            {"batch": {"schema": "arr-batch-v1", "role": "parent"}}
        ),
        batch_total=2,
    )
    database.create_job(
        "child-api-1",
        "batch",
        "tv",
        "Serie S01E01.mkv",
        state="done",
        parent_job_id=parent["job_id"],
        batch_index=1,
        batch_total=2,
    )
    child = database.create_job(
        "child-api-2",
        "batch",
        "tv",
        "Serie S01E02.mkv",
        state="manual_review",
        parent_job_id=parent["job_id"],
        batch_index=2,
        batch_total=2,
    )

    parent_payload = next(
        item for item in database.jobs_for_api() if item["job_id"] == parent["job_id"]
    )
    assert parent_payload["batch"] == {
        "role": "parent",
        "parent_job_id": None,
        "index": 0,
        "total": 2,
        "completed": 2,
        "succeeded": 1,
        "issues": 1,
    }
    assert len(database.job_detail(parent["job_id"])["children"]) == 2
    assert database.job_detail(child["job_id"])["parent"]["job_id"] == parent["job_id"]
    database.close()


def test_parent_cleans_clients_once_and_finishes_with_child_warning(tmp_path: Path):
    config = _config(tmp_path)
    database = Database(tmp_path / "config" / "orchestrator.db")
    database.initialize()
    engine = Engine(config, database)
    parent = database.create_job(
        "parent-cleanup",
        "rdt",
        "tv",
        "Temporada",
        state="batch_cleanup_ready",
        infohash="abc123",
        qbt_hash="abc123",
        rdt_id="rdt-1",
        source_meta_json=json.dumps(
            {"batch": {"schema": "arr-batch-v1", "role": "parent", "total": 2}}
        ),
        batch_total=2,
    )
    parent_root = config.workshop_root / str(parent["job_id"])
    parent_root.mkdir(parents=True)
    parent = database.update_job(str(parent["job_id"]), stage_path=str(parent_root))
    for index, state in enumerate(("done", "duplicate"), start=1):
        database.create_job(
            f"parent-cleanup-child-{index}",
            "batch",
            "tv",
            f"Serie S01E0{index}.mkv",
            state=state,
            parent_job_id=parent["job_id"],
            batch_index=index,
            batch_total=2,
        )
    engine.qbt = Mock()
    engine.rdt = Mock()

    engine._run_batch_cleanup(parent)

    assert database.get_job(str(parent["job_id"]))["state"] == "done_with_warnings"
    engine.qbt.delete.assert_called_once_with("abc123", delete_files=False)
    engine.rdt.delete.assert_called_once_with("abc123", delete_files=False)
    assert not parent_root.exists()
    database.close()
