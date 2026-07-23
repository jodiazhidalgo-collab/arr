import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arr_orchestrator.filebot import FileBotRunner
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
    )


class FileBotRuleCommandTests(unittest.TestCase):
    def test_default_guided_movie_command_is_unchanged(self) -> None:
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
                    "-non-strict",
                    "--format",
                    "{n} ({y})/{n} ({y})",
                ],
            )

    def test_default_guided_tv_command_is_unchanged(self) -> None:
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
                    "-non-strict",
                    "--format",
                    "{n}/Season {s.pad(2)}/{n} - {s00e00}",
                ],
            )

    def test_default_legacy_movie_and_tv_commands_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_root = root / "input"
            output_root = root / "output"
            runner = FileBotRunner("/opt/filebot/filebot", root)

            common = [
                "/opt/filebot/filebot",
                "-no-xattr",
                "-script",
                "fn:amc",
                str(input_root),
                "--log-file",
            ]
            common_tail = [
                "--output",
                str(output_root),
                "--action",
                "move",
                "--conflict",
                "skip",
                "-non-strict",
                "--lang",
                "es",
                "--def",
                "clean=y",
                "music=n",
                "artwork=n",
                "excludeList=/dev/null",
            ]
            movie = runner.preview_command(
                "legacy-movie", "movies", input_root, output_root
            )["argv"]
            tv = runner.preview_command(
                "legacy-tv", "tv", input_root, output_root
            )["argv"]

            self.assertEqual(
                movie,
                common
                + [str(root / "filebot-legacy-movie.log")]
                + common_tail
                + ["ut_label=movie", "movieFormat={n} ({y})/{n} ({y})"],
            )
            self.assertEqual(
                tv,
                common
                + [str(root / "filebot-legacy-tv.log")]
                + common_tail
                + [
                    "ut_label=TV",
                    "minLengthMS=300000",
                    "seriesFormat={n}/Season {s.pad(2)}/{n} - {s00e00}",
                ],
            )

    def test_safe_styles_keep_canonical_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = FileBotRunner("filebot", root)
            runner.configure_rules(
                {
                    "movies": {
                        "language": "fr-FR",
                        "filename_style": "title_year_quality",
                    },
                    "tv": {
                        "language": "en-US",
                        "filename_style": "series_sxxexx_title",
                        "episode_order": "DVD",
                    },
                }
            )

            movie = runner.preview_command(
                "movie", "movies", root / "in-m", root / "out-m", identity()
            )["argv"]
            tv = runner.preview_command(
                "tv", "tv", root / "in-t", root / "out-t", identity("tv")
            )["argv"]

            self.assertEqual(movie[movie.index("--lang") + 1], "fr")
            self.assertEqual(
                movie[movie.index("--format") + 1],
                "{n} ({y})/{n} ({y}) [{vf}]",
            )
            self.assertEqual(tv[tv.index("--lang") + 1], "en")
            self.assertEqual(
                tv[tv.index("--format") + 1],
                "{n}/Season {s.pad(2)}/{n} - {s00e00} - {t}",
            )
            self.assertEqual(tv[tv.index("--order") + 1], "DVD")

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
