import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arr_orchestrator.filebot import FileBotRunner, MOVIE_FORMAT, TV_FORMAT
from arr_orchestrator.name_resolver import ResolvedIdentity


def identity(media_type: str = "movie") -> ResolvedIdentity:
    return ResolvedIdentity(
        media_type=media_type,
        tmdb_id=11687 if media_type == "movie" else 1396,
        title="Los visitantes" if media_type == "movie" else "Breaking Bad",
        original_title="Les Visiteurs" if media_type == "movie" else "Breaking Bad",
        year=1993 if media_type == "movie" else 2008,
        aliases=["The Visitors"] if media_type == "movie" else ["Breaking Bad"],
        score=125,
        margin=20,
        query="The Visitors" if media_type == "movie" else "Breaking Bad",
        guess={"title": "The Visitors" if media_type == "movie" else "Breaking Bad"},
        source="test",
        season=1 if media_type == "tv" else None,
        episodes=[1] if media_type == "tv" else [],
        resolver_algorithm_version="phased-er-v2",
        decision_status="ACCEPTED_CONFIDENT",
    )


class FileBotCommandTests(unittest.TestCase):
    def test_guided_movie_command_is_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_root = root / "input"
            output_root = root / "output"
            runner = FileBotRunner("/opt/filebot/filebot", root)

            preview = runner.preview_command(
                "job-default", "movies", input_root, output_root, identity()
            )

            self.assertEqual(
                preview["argv"],
                [
                    "/opt/filebot/filebot",
                    "-no-xattr",
                    "-rename",
                    "-r",
                    str(input_root),
                    "--log-file",
                    str(root / "filebot-job-default.log"),
                    "--db",
                    "TheMovieDB",
                    "--q",
                    "11687",
                    "--lang",
                    "es",
                    "--output",
                    str(output_root),
                    "--action",
                    "move",
                    "--conflict",
                    "skip",
                    "--format",
                    MOVIE_FORMAT,
                ],
            )

    def test_guided_tv_command_is_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_root = root / "input"
            output_root = root / "output"
            runner = FileBotRunner("/opt/filebot/filebot", root)

            preview = runner.preview_command(
                "job-default-tv", "tv", input_root, output_root, identity("tv")
            )

            self.assertEqual(
                preview["argv"],
                [
                    "/opt/filebot/filebot",
                    "-no-xattr",
                    "-rename",
                    "-r",
                    str(input_root),
                    "--log-file",
                    str(root / "filebot-job-default-tv.log"),
                    "--db",
                    "TheMovieDB::TV",
                    "--q",
                    "1396",
                    "--lang",
                    "es",
                    "--output",
                    str(output_root),
                    "--action",
                    "move",
                    "--conflict",
                    "skip",
                    "--format",
                    TV_FORMAT,
                ],
            )

    def test_filebot_refuses_to_run_without_an_accepted_tmdb_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_root = root / "input"
            output_root = root / "output"
            runner = FileBotRunner("/opt/filebot/filebot", root)

            with self.assertRaisesRegex(ValueError, "identidad TMDb aceptada"):
                runner.preview_command("without-identity", "movies", input_root, output_root)
            with self.assertRaisesRegex(ValueError, "identidad TMDb aceptada"):
                runner.run("without-identity", "tv", input_root, output_root)

            self.assertFalse(hasattr(runner, "_legacy_amc_command"))

    def test_filebot_refuses_blank_or_blocked_v2_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = FileBotRunner("filebot", root)
            blank = identity()
            blank.decision_status = ""
            blocked = identity("tv")
            blocked.decision_status = "BLOCKED_HARD"

            with self.assertRaisesRegex(ValueError, "decision v2 aceptada"):
                runner.preview_command(
                    "blank", "movies", root / "in-m", root / "out-m", blank
                )
            with self.assertRaisesRegex(ValueError, "decision v2 aceptada"):
                runner.preview_command(
                    "blocked", "tv", root / "in-t", root / "out-t", blocked
                )

    def test_filebot_refuses_a_historical_resolver_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = FileBotRunner("filebot", root)
            historical = identity()
            historical.resolver_algorithm_version = "title-evidence-v1"

            with self.assertRaisesRegex(ValueError, "phased-er-v2"):
                runner.preview_command(
                    "historical", "movies", root / "in", root / "out", historical
                )

    def test_guided_locale_does_not_change_fixed_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = FileBotRunner("filebot", root)
            runner.configure_identity_rules(
                {
                    "resolver": {
                        "locales": {
                            "movies": {"language": "fr-FR"},
                            "tv": {"language": "en-US"},
                        }
                    }
                }
            )

            movie = runner.preview_command(
                "movie", "movies", root / "in-m", root / "out-m", identity()
            )
            tv = runner.preview_command(
                "tv", "tv", root / "in-t", root / "out-t", identity("tv")
            )
            movie_argv = movie["argv"]
            tv_argv = tv["argv"]
            self.assertEqual(movie_argv[movie_argv.index("--lang") + 1], "fr")
            self.assertEqual(movie_argv[movie_argv.index("--format") + 1], MOVIE_FORMAT)
            self.assertNotIn("[vf]", MOVIE_FORMAT)
            self.assertEqual(tv_argv[tv_argv.index("--lang") + 1], "en")
            self.assertEqual(tv_argv[tv_argv.index("--format") + 1], TV_FORMAT)
            self.assertNotIn("{t}", TV_FORMAT)
            self.assertNotIn("--order", tv_argv)
            self.assertEqual(movie["rules"]["language"], "fr-FR")
            self.assertEqual(movie["rules"]["format"], MOVIE_FORMAT)
            self.assertEqual(tv["rules"]["language"], "en-US")
            self.assertEqual(tv["rules"]["format"], TV_FORMAT)
            self.assertEqual(movie["tmdb_id"], 11687)
            self.assertEqual(tv["tmdb_id"], 1396)

    def test_timeout_is_returned_as_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_root = root / "input"
            output_root = root / "output"
            destination = output_root / "Los visitantes (1993)" / "Los visitantes (1993).mkv"
            input_root.mkdir()
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"partial")
            move_line = f"[MOVE] from [{input_root / 'movie.mkv'}] to [{destination}]"
            runner = FileBotRunner("filebot", root)

            with patch(
                "arr_orchestrator.filebot.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    ["filebot"], 14400, output=move_line, stderr=""
                ),
            ):
                result = runner.run(
                    "timeout", "movies", input_root, output_root, identity()
                )

            self.assertTrue(result["timed_out"])
            self.assertEqual(result["exit_code"], 124)
            self.assertEqual(len(result["moves"]), 1)
            self.assertIn(str(destination), result["output_media"])
            self.assertTrue((root / "filebot-timeout.json").exists())


if __name__ == "__main__":
    unittest.main()
