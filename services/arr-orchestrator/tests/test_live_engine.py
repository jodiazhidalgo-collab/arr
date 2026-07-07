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
        self.temporary = tempfile.TemporaryDirectory()
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
        )
        self.config.ensure_directories()
        self.database = Database(self.config.db_path)
        self.database.initialize()
        self.engine = Engine(self.config, self.database)

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def create_job(self, category, name):
        job_root = self.config.workshop_root / f"job-{category}"
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
        job = self.database.create_job(
            f"live:{category}:{video.stem}",
            "fs",
            category,
            name,
            state="ready_filebot",
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
        self.assertEqual(len(list(output.glob("*.mkv"))), 1)
        self.assertEqual(len(list(output.glob("*.srt"))), 1)

    def test_complete_guided_tv_stage(self):
        job = self.create_job("tv", "Juego.de.tronos.S01E01.mkv")

        self.engine._run_filebot(job)

        updated = self.database.get_job(job["job_id"])
        self.assertEqual(updated["state"], "ready_cleanup")
        self.assertIn('"tmdb_id": 1399', updated["identity_json"])
        episodes = list(self.config.tv_output.rglob("*.mkv"))
        subtitles = list(self.config.tv_output.rglob("*.srt"))
        self.assertEqual(len(episodes), 1)
        self.assertEqual(len(subtitles), 1)


if __name__ == "__main__":
    unittest.main()
