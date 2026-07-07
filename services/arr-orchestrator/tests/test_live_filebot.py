import os
import tempfile
import unittest
from pathlib import Path

from arr_orchestrator.filebot import FileBotRunner
from arr_orchestrator.name_resolver import ResolvedIdentity


@unittest.skipUnless(
    os.environ.get("RUN_FILEBOT_LIVE_TESTS") == "1",
    "RUN_FILEBOT_LIVE_TESTS no activado",
)
class LiveFileBotTests(unittest.TestCase):
    def identity(self, media_type, tmdb_id, title, original_title, year):
        return ResolvedIdentity(
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            original_title=original_title,
            year=year,
            aliases=[title, original_title],
            score=100,
            margin=50,
            query=title,
            guess={"title": title},
            source="live-test",
            season=1 if media_type == "tv" else None,
            episodes=[1] if media_type == "tv" else [],
        )

    def test_guided_movie_moves_video_and_subtitle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_root = root / "input"
            output_root = root / "output"
            input_root.mkdir()
            output_root.mkdir()
            (input_root / "Un padre en apuros 4Kwebrip2160.atomohd.li.mkv").write_bytes(
                b"fixture"
            )
            (input_root / "Un padre en apuros 4Kwebrip2160.atomohd.li.es.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nPrueba\n",
                encoding="utf-8",
            )
            runner = FileBotRunner("/opt/filebot/filebot", root)

            result = runner.run(
                "live-movie",
                "movies",
                input_root,
                output_root,
                self.identity("movie", 9279, "Un padre en apuros", "Jingle All the Way", 1996),
            )

            self.assertEqual(result["exit_code"], 0, result["stdout_tail"])
            destinations = [item["destination"] for item in result["moves"]]
            self.assertTrue(any(value.endswith(".mkv") for value in destinations))
            self.assertTrue(any(value.endswith(".srt") for value in destinations))

    def test_guided_tv_moves_episode_and_subtitle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_root = root / "input"
            output_root = root / "output"
            input_root.mkdir()
            output_root.mkdir()
            (input_root / "Juego.de.tronos.S01E01.mkv").write_bytes(b"fixture")
            (input_root / "Juego.de.tronos.S01E01.es.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nPrueba\n",
                encoding="utf-8",
            )
            (input_root / "Juego.de.tronos.S01E02.mkv").write_bytes(b"fixture")
            runner = FileBotRunner("/opt/filebot/filebot", root)

            result = runner.run(
                "live-tv",
                "tv",
                input_root,
                output_root,
                self.identity("tv", 1399, "Juego de tronos", "Game of Thrones", 2011),
            )

            self.assertEqual(result["exit_code"], 0, result["stdout_tail"])
            destinations = [item["destination"] for item in result["moves"]]
            self.assertEqual(sum(value.endswith(".mkv") for value in destinations), 2)
            self.assertTrue(any(value.endswith(".srt") for value in destinations))


if __name__ == "__main__":
    unittest.main()
