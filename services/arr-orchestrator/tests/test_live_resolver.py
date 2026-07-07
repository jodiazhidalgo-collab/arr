import os
import tempfile
import time
import unittest
from pathlib import Path

from arr_orchestrator.db import Database
from arr_orchestrator.name_resolver import NameResolver


@unittest.skipUnless(os.environ.get("TMDB_API_TOKEN"), "TMDB_API_TOKEN no configurado")
class LiveResolverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "resolver.db")
        self.database.initialize()
        self.resolver = NameResolver(
            os.environ["TMDB_API_TOKEN"],
            "es-ES",
            "ES",
            2500,
            5000,
            self.database,
        )

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def input_file(self, name):
        input_root = self.root / Path(name).stem
        input_root.mkdir(parents=True)
        (input_root / name).write_bytes(b"fixture")
        return input_root

    def test_real_tmdb_movie_resolution(self):
        input_root = self.input_file("Un padre en apuros 4Kwebrip2160.atomohd.li.mkv")
        started = time.monotonic()

        identity = self.resolver.resolve(
            {"category": "movies", "name": input_root.name}, input_root
        )

        self.assertEqual(identity.tmdb_id, 9279)
        self.assertEqual(identity.year, 1996)
        self.assertLess(time.monotonic() - started, 5.5)

    def test_real_tmdb_tv_resolution(self):
        input_root = self.input_file("Juego.de.tronos.S01E01.mkv")

        identity = self.resolver.resolve(
            {"category": "tv", "name": "Juego.de.tronos.S01E01"}, input_root
        )

        self.assertEqual(identity.tmdb_id, 1399)
        self.assertEqual(identity.season, 1)
        self.assertEqual(identity.episodes, [1])

    def test_real_tmdb_cache_is_immediate(self):
        input_root = self.input_file("Cenicienta.2015.2160p.mkv")
        job = {"category": "movies", "name": "Cenicienta.2015.2160p"}
        first = self.resolver.resolve(job, input_root)
        started = time.monotonic()

        second = self.resolver.resolve(job, input_root)

        self.assertEqual(first.tmdb_id, second.tmdb_id)
        self.assertEqual(second.source, "cache")
        self.assertLess(time.monotonic() - started, 0.2)


if __name__ == "__main__":
    unittest.main()
