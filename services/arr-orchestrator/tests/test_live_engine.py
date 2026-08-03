import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from arr_orchestrator.config import Config
from arr_orchestrator.db import Database
from arr_orchestrator.engine import Engine


@unittest.skipUnless(
    os.environ.get("RUN_ENGINE_LIVE_TESTS") == "1",
    "RUN_ENGINE_LIVE_TESTS no activado",
)
class LiveEngineTests(unittest.TestCase):
    def setUp(self):
        candidates = [
            Path(__file__).resolve().parents[3] / "_codex_runtime" / "tmp",
            Path(tempfile.gettempdir()) / "_codex_runtime" / "tmp",
        ]
        self.temporary = None
        for candidate in candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                temporary = tempfile.TemporaryDirectory(
                    prefix="live-engine-",
                    dir=candidate,
                )
                self.temporary = temporary
                break
            except OSError:
                continue
        if self.temporary is None:
            self.fail("No existe una ubicación escribible para _codex_runtime/tmp")
        self.root = Path(self.temporary.name)
        base = Config.from_env()
        data = self.root / "data"
        complete = data / "downloads" / "torrents" / "complete"
        self.config = replace(
            base,
            config_dir=self.root / "config",
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
            media_reports_root=self.root / "config" / "media-worker",
            codex_diag_root=self.root / "diagnosticos_codex",
            series_reports_root=self.root / "config" / "series-worker",
            series_review_dir=data / "media" / "series-review-test",
            series_mode="active",
        )
        self.config.ensure_directories()
        self.database = Database(self.config.db_path)
        self.database.initialize()
        self.engine = Engine(self.config, self.database)

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def create_job(self, category, name):
        job = self.database.create_job(
            f"live:{category}:{Path(name).stem}",
            "fs",
            category,
            name,
            state="ready_filebot",
            source_meta_json=self.engine._new_job_source_meta_json(
                category=category,
                name=name,
            ),
        )
        job_root = self.config.workshop_root / str(job["job_id"])
        original = job_root / "original"
        original.mkdir(parents=True)
        video = original / name
        fixture = Path(os.environ.get("LIVE_MEDIA_FIXTURE", ""))
        if fixture.is_file():
            shutil.copy2(fixture, video)
        else:
            video.write_bytes(b"fixture")
        subtitle = original / f"{video.stem}.es.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nPrueba\n",
            encoding="utf-8",
        )
        job = self.database.update_job(
            str(job["job_id"]),
            source_path=str(original),
            stage_path=str(job_root),
        )
        return job

    def test_complete_guided_movie_stage(self):
        job = self.create_job(
            "movies", "Un padre en apuros 4Kwebrip2160.atomohd.li.mkv"
        )

        self.engine._run_filebot(job)

        updated = self.database.get_job(job["job_id"])
        self.assertEqual(updated["state"], "media_postprocess_ready")
        self.assertIn('"tmdb_id": 9279', updated["identity_json"])
        output = Path(updated["source_path"])
        self.assertEqual(output.name, "Un padre en apuros (1996)")
        self.assertTrue((output / "Un padre en apuros (1996).mkv").is_file())
        self.assertTrue((output / "Un padre en apuros (1996).srt").is_file())

    def test_complete_guided_tv_stage(self):
        job = self.create_job("tv", "Juego.de.tronos.S01E01.mkv")

        self.engine._run_filebot(job)

        updated = self.database.get_job(job["job_id"])
        self.assertEqual(updated["state"], "series_postprocess_ready")
        self.assertIn('"tmdb_id": 1399', updated["identity_json"])
        output = (
            Path(updated["stage_path"])
            / "series_filebot_output"
            / "Juego de tronos"
            / "Season 01"
        )
        self.assertTrue((output / "Juego de tronos - S01E01.mkv").is_file())
        self.assertTrue((output / "Juego de tronos - S01E01.srt").is_file())
        self.assertFalse(any(self.config.tv_output.rglob("*.mkv")))


if __name__ == "__main__":
    unittest.main()
