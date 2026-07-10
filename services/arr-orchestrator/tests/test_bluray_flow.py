import shutil
import subprocess
from pathlib import Path

import pytest

from arr_orchestrator.config import Config
from arr_orchestrator.db import Database
from arr_orchestrator.engine import Engine
from arr_orchestrator.filesystem import (
    extract_archives,
    full_bluray_folders,
    media_files,
    prepare_filebot_input,
)
from arr_orchestrator.name_resolver import ResolvedIdentity, ResolverAmbiguous


def make_config(root: Path) -> Config:
    data = root / "data"
    complete = data / "downloads" / "torrents" / "complete"
    return Config(
        mode="active",
        config_dir=root / "config",
        data_root=data,
        watch_inbox=data / "torrents" / "watch" / "inbox",
        processed_root=data / "torrents" / "watch" / "processed",
        watch_error=data / "torrents" / "watch" / "error",
        event_dir=data / "torrents" / "events" / "inbox" / "qbt",
        complete_root=complete,
        workshop_root=complete / "taller",
        movies_output=complete / "movies_automatizacion",
        movies_final=data / "media" / "movies",
        tv_output=data / "media" / "tv",
        trailers_inbox=complete / "trailers_automatizacion",
        review_dir=data / "media" / "repetidas_vs_error",
        media_worker_url="http://media-worker:8790",
        callback_url="http://arr-orchestrator:8787",
        media_reports_root=root / "config" / "media-worker",
        codex_diag_root=root / "diagnosticos_codex",
        diagnostics_root=root / "diagnostics" / "arr",
        qbt_url="http://gluetun:8080",
        qbt_user="admin",
        qbt_password="",
        rdt_url="http://rdtclient:6500",
        rdt_user="admin",
        rdt_password="",
        stable_seconds=1,
        reconcile_seconds=30,
        fallback_seconds=5400,
        health_port=8787,
        filebot_bin="/opt/filebot/filebot",
        tmdb_api_token="",
        resolver_language="es-ES",
        resolver_region="ES",
        resolver_http_timeout_ms=2500,
        resolver_total_budget_ms=5000,
        resolver_retry_seconds=60,
    )


def make_bluray_release(root: Path, with_extra=False) -> Path:
    playlist = root / "BDMV" / "PLAYLIST"
    stream = root / "BDMV" / "STREAM"
    playlist.mkdir(parents=True)
    stream.mkdir(parents=True)
    (playlist / "00001.mpls").write_bytes(b"playlist")
    (stream / "00001.m2ts").write_bytes(b"main")
    (root / "CERTIFICATE").mkdir()
    (root / "poster.jpg").write_bytes(b"poster")
    if with_extra:
        extra = root / "EXTRA"
        extra.mkdir()
        (extra / "Dietro Mr. Bean.mkv").write_bytes(b"extra")
    return root


def movie_identity():
    return ResolvedIdentity(
        media_type="movie",
        tmdb_id=1268,
        title="Las vacaciones de Mr. Bean",
        original_title="Mr. Bean's Holiday",
        year=2007,
        aliases=["Las vacaciones de Mr. Bean", "Mr. Bean's Holiday"],
        score=125,
        margin=100,
        query="Las vacaciones de Mr. Bean",
        guess={"title": "Las vacaciones de Mr. Bean", "year": 2007},
        source="test",
    )


class Resolver:
    enabled = True

    def resolve(self, _job, _input_root):
        return movie_identity()

    def output_matches(self, _identity, names):
        return names == ["Las vacaciones de Mr. Bean (2007)"]


class FileBot:
    def __init__(self, duplicate=False):
        self.calls = []
        self.duplicate = duplicate

    def run(self, _job_id, _category, input_path, output_root, _identity):
        self.calls.append(Path(input_path))
        if self.duplicate:
            return {
                "exit_code": 0,
                "moves": [],
                "output_media": [],
                "duplicate": True,
                "stdout_tail": "already exists",
            }
        source = media_files(Path(input_path))[0]
        destination = (
            output_root
            / "Las vacaciones de Mr. Bean (2007)"
            / "Las vacaciones de Mr. Bean (2007).mkv"
        )
        destination.parent.mkdir(parents=True)
        shutil.move(str(source), str(destination))
        return {
            "exit_code": 0,
            "moves": [{"source": str(source), "destination": str(destination)}],
            "output_media": [str(destination)],
            "duplicate": False,
            "stdout_tail": "",
        }


class NormalizingWorker:
    def __init__(self, status="normalized"):
        self.status = status
        self.calls = []

    def preview_normalize_bluray(self, job_id, source_path, reports_root):
        return {
            "method": "POST",
            "endpoint": "/normalize-bluray",
            "payload": {
                "job_id": job_id,
                "source_path": str(source_path),
                "reports_root": str(reports_root),
            },
            "timeout_sec": 14400,
        }

    def normalize_bluray(self, _job_id, source_path, _reports_root):
        source = Path(source_path)
        self.calls.append(source)
        if self.status != "normalized":
            return {
                "status": self.status,
                "reason": "controlled bluray result",
                "source_removed": False,
            }
        shutil.rmtree(source / "BDMV")
        shutil.rmtree(source / "CERTIFICATE")
        result = source / f"{source.name}.mkv"
        result.write_bytes(b"normalized movie")
        return {
            "status": "normalized",
            "normalized": True,
            "source_removed": True,
            "result_file": str(result),
        }


def make_engine_job(tmp_path: Path, release: Path):
    config = make_config(tmp_path)
    config.ensure_directories()
    database = Database(tmp_path / "orchestrator.db")
    database.initialize()
    engine = Engine(config, database)
    job_root = config.workshop_root / "job-bluray"
    original = job_root / "original"
    original.mkdir(parents=True)
    staged = original / release.name
    shutil.move(str(release), str(staged))
    job = database.create_job(
        "fs:movies:bluray",
        "fs",
        "movies",
        "Las vacaciones de Mr. Bean (2007)",
        state="ready_filebot",
        source_path=str(original),
        stage_path=str(job_root),
    )
    engine.name_resolver = Resolver()
    return engine, database, job


def test_b258_bluray_simple_se_normaliza_antes_de_filebot(tmp_path):
    release = make_bluray_release(tmp_path / "input" / "Release")
    engine, database, job = make_engine_job(tmp_path, release)
    worker = NormalizingWorker()
    filebot = FileBot()
    engine.media_worker = worker
    engine.filebot = filebot
    engine._run_filebot(job)
    updated = database.get_job(job["job_id"])
    assert len(worker.calls) == 1
    assert updated["state"] == "media_postprocess_ready"
    assert len(filebot.calls) == 1
    passed_media = media_files(filebot.calls[0])
    assert passed_media == []
    assert "BDMV" not in str(filebot.calls[0])
    database.close()


def test_dfa_extras_no_llegan_a_filebot(tmp_path):
    release = make_bluray_release(tmp_path / "input" / "Release", with_extra=True)
    engine, database, job = make_engine_job(tmp_path, release)
    worker = NormalizingWorker()
    filebot = FileBot()
    engine.media_worker = worker
    engine.filebot = filebot
    engine._run_filebot(job)
    assert len(filebot.calls) == 1
    assert "EXTRA" not in str(filebot.calls[0])
    assert not any("Dietro" in str(path) for path in media_files(filebot.calls[0]))
    assert database.get_job(job["job_id"])["state"] == "media_postprocess_ready"
    database.close()


def test_entrada_mkv_normal_no_llama_al_normalizador(tmp_path):
    release = tmp_path / "input" / "Release"
    release.mkdir(parents=True)
    (release / "movie.mkv").write_bytes(b"movie")
    engine, database, job = make_engine_job(tmp_path, release)
    worker = NormalizingWorker()
    filebot = FileBot()
    engine.media_worker = worker
    engine.filebot = filebot
    engine._run_filebot(job)
    assert worker.calls == []
    assert len(filebot.calls) == 1
    assert database.get_job(job["job_id"])["state"] == "media_postprocess_ready"
    database.close()


@pytest.mark.parametrize(
    "status,expected_state",
    [("ambiguous", "manual_review"), ("verification_failed", "error_terminal")],
)
def test_fallo_bluray_impide_filebot_y_conserva_bdmv(tmp_path, status, expected_state):
    release = make_bluray_release(tmp_path / "input" / "Release")
    engine, database, job = make_engine_job(tmp_path, release)
    worker = NormalizingWorker(status)
    filebot = FileBot()
    engine.media_worker = worker
    engine.filebot = filebot
    engine._run_filebot(job)
    updated = database.get_job(job["job_id"])
    review = Path(updated["stage_path"])
    assert updated["state"] == expected_state
    assert filebot.calls == []
    assert (review / "BDMV").exists()
    assert (review / "CERTIFICATE").exists()
    database.close()


def test_362_identidad_ambigua_no_normaliza_el_bluray(tmp_path):
    release = make_bluray_release(tmp_path / "input" / "Mister.Bin.na.otdyhe.2007")
    engine, database, job = make_engine_job(tmp_path, release)
    worker = NormalizingWorker()
    filebot = FileBot()

    class AmbiguousResolver:
        enabled = True

        def resolve(self, _job, _input_root):
            raise ResolverAmbiguous("TMDb no devolvio candidatos", {})

    engine.name_resolver = AmbiguousResolver()
    engine.media_worker = worker
    engine.filebot = filebot
    engine._run_filebot(job)
    updated = database.get_job(job["job_id"])
    assert updated["state"] == "manual_review"
    assert worker.calls == []
    assert filebot.calls == []
    assert (Path(updated["stage_path"]) / "BDMV").exists()
    database.close()


def test_bluray_repetido_normaliza_y_entra_en_circuito_repetidas(tmp_path):
    release = make_bluray_release(tmp_path / "input" / "Release")
    engine, database, job = make_engine_job(tmp_path, release)
    worker = NormalizingWorker()
    filebot = FileBot(duplicate=True)
    engine.media_worker = worker
    engine.filebot = filebot
    engine._run_filebot(job)
    updated = database.get_job(job["job_id"])
    assert len(worker.calls) == 1
    assert len(filebot.calls) == 1
    assert updated["state"] == "duplicate"
    assert Path(updated["stage_path"]).exists()
    database.close()


def test_prepare_filebot_input_no_colapsa_en_bdmv_extraido(tmp_path):
    job_root = tmp_path / "job"
    extracted = job_root / "extracted"
    make_bluray_release(extracted)
    prepared = prepare_filebot_input(extracted, job_root, "Las vacaciones de Mr. Bean (2007).zip")
    assert prepared.name == "Las vacaciones de Mr. Bean (2007)"
    assert full_bluray_folders(prepared) == [prepared]
    assert prepared.name != "BDMV"


def test_zip_normal_mantiene_extraccion_existente(tmp_path, monkeypatch):
    job_root = tmp_path / "job"
    original = job_root / "original"
    original.mkdir(parents=True)
    archive = original / "Las vacaciones de Mr. Bean (2007).zip"
    archive.write_bytes(b"zip")

    def fake_run(_command, **_kwargs):
        extracted = job_root / "extracted"
        extracted.mkdir(parents=True, exist_ok=True)
        (extracted / "Las vacaciones de Mr. Bean (2007).mkv").write_bytes(b"movie")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr("arr_orchestrator.filesystem.subprocess.run", fake_run)
    extracted = extract_archives(job_root)
    assert [path.name for path in media_files(extracted)] == ["Las vacaciones de Mr. Bean (2007).mkv"]
