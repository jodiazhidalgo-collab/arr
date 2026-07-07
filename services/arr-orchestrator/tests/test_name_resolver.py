import tempfile
import unittest
from pathlib import Path

from arr_orchestrator.db import Database
from arr_orchestrator.filebot import FileBotRunner
from arr_orchestrator.name_resolver import (
    NameResolver,
    ResolvedIdentity,
    ResolverAmbiguous,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params or {}, timeout))
        path = url.split("/3", 1)[1]
        route = self.routes[path]
        payload = route(params or {}) if callable(route) else route
        return FakeResponse(payload)


def movie_payload(tmdb_id, title, original_title, year):
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": original_title,
        "release_date": f"{year}-01-01",
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    }


def tv_payload(tmdb_id, title, original_title, year, seasons=10):
    return {
        "id": tmdb_id,
        "name": title,
        "original_name": original_title,
        "first_air_date": f"{year}-01-01",
        "number_of_seasons": seasons,
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
    }


class NameResolverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "test.db")
        self.database.initialize()

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def resolver(self, routes):
        session = FakeSession(routes)
        resolver = NameResolver(
            "token",
            "es-ES",
            "ES",
            2500,
            5000,
            self.database,
            session=session,
        )
        return resolver, session

    def input_file(self, name):
        input_root = self.root / "filebot_input" / Path(name).stem
        input_root.mkdir(parents=True)
        (input_root / name).write_bytes(b"movie")
        return input_root

    def test_spanish_movie_without_year_prefers_exact_title(self):
        correct = movie_payload(9279, "Un padre en apuros", "Jingle All the Way", 1996)
        wrong = movie_payload(
            505026,
            "El padre: La venganza tiene un precio",
            "The Father",
            2018,
        )
        routes = {
            "/search/movie": {"results": [wrong, correct]},
            "/movie/9279": correct,
            "/movie/505026": wrong,
        }
        resolver, _ = self.resolver(routes)
        input_root = self.input_file("Un padre en apuros 4Kwebrip2160.atomohd.li.mkv")

        identity = resolver.resolve(
            {"category": "movies", "name": input_root.name}, input_root
        )

        self.assertEqual(identity.tmdb_id, 9279)
        self.assertEqual(identity.year, 1996)
        self.assertGreaterEqual(identity.score, 75)

    def test_cache_avoids_second_tmdb_query(self):
        correct = movie_payload(9279, "Un padre en apuros", "Jingle All the Way", 1996)
        routes = {
            "/search/movie": {"results": [correct]},
            "/movie/9279": correct,
        }
        resolver, session = self.resolver(routes)
        input_root = self.input_file("Un padre en apuros.mkv")
        job = {"category": "movies", "name": "Un padre en apuros"}

        first = resolver.resolve(job, input_root)
        call_count = len(session.calls)
        second = resolver.resolve(job, input_root)

        self.assertEqual(first.tmdb_id, second.tmdb_id)
        self.assertEqual(len(session.calls), call_count)
        self.assertEqual(second.source, "cache")

    def test_same_title_prefers_matching_year(self):
        old = movie_payload(11224, "Cenicienta", "Cinderella", 1950)
        current = movie_payload(150689, "Cenicienta", "Cinderella", 2015)
        routes = {
            "/search/movie": {"results": [old, current]},
            "/movie/11224": old,
            "/movie/150689": current,
        }
        resolver, _ = self.resolver(routes)
        input_root = self.input_file("Cenicienta.2015.2160p.mkv")

        identity = resolver.resolve(
            {"category": "movies", "name": "Cenicienta.2015.2160p"}, input_root
        )

        self.assertEqual(identity.tmdb_id, 150689)

    def test_tv_episode_validates_season(self):
        search = {
            "id": 1399,
            "name": "Juego de tronos",
            "original_name": "Game of Thrones",
            "first_air_date": "2011-04-17",
        }
        details = {
            **search,
            "number_of_seasons": 8,
            "alternative_titles": {"results": []},
            "translations": {"translations": []},
        }
        routes = {
            "/search/tv": {"results": [search]},
            "/tv/1399": details,
        }
        resolver, _ = self.resolver(routes)
        input_root = self.input_file("Juego.de.tronos.S01E01.mkv")

        identity = resolver.resolve(
            {"category": "tv", "name": "Juego.de.tronos.S01E01"}, input_root
        )

        self.assertEqual(identity.tmdb_id, 1399)
        self.assertEqual(identity.season, 1)
        self.assertEqual(identity.episodes, [1])

    def test_ambiguous_candidates_are_not_accepted(self):
        first = movie_payload(1, "El desconocido", "Unknown", 2000)
        second = movie_payload(2, "El desconocido", "Unknown", 2000)
        routes = {
            "/search/movie": {"results": [first, second]},
            "/movie/1": first,
            "/movie/2": second,
        }
        resolver, _ = self.resolver(routes)
        input_root = self.input_file("El desconocido.2000.mkv")

        with self.assertRaises(ResolverAmbiguous):
            resolver.resolve(
                {"category": "movies", "name": "El desconocido.2000"}, input_root
            )

    def test_guided_filebot_command_uses_tmdb_id(self):
        identity = ResolvedIdentity(
            media_type="movie",
            tmdb_id=9279,
            title="Un padre en apuros",
            original_title="Jingle All the Way",
            year=1996,
            aliases=["Un padre en apuros", "Jingle All the Way"],
            score=100,
            margin=50,
            query="Un padre en apuros",
            guess={"title": "Un padre en apuros"},
            source="search",
        )
        runner = FileBotRunner("filebot", self.root)

        command = runner._guided_command(
            "movies", self.root / "input", self.root / "output", self.root / "log", identity
        )

        self.assertIn("-rename", command)
        self.assertNotIn("fn:amc", command)
        self.assertEqual(command[command.index("--q") + 1], "9279")
        self.assertEqual(command[command.index("--db") + 1], "TheMovieDB")

    def test_filebot_preview_command_exposes_argv_mode_and_timeout(self):
        identity = ResolvedIdentity(
            media_type="movie",
            tmdb_id=9279,
            title="Un padre en apuros",
            original_title="Jingle All the Way",
            year=1996,
            aliases=["Un padre en apuros", "Jingle All the Way"],
            score=100,
            margin=50,
            query="Un padre en apuros",
            guess={"title": "Un padre en apuros"},
            source="search",
        )
        runner = FileBotRunner("filebot", self.root)

        preview = runner.preview_command(
            "job-1",
            "movies",
            self.root / "input",
            self.root / "output",
            identity,
        )

        self.assertEqual(preview["mode"], "guided")
        self.assertEqual(preview["timeout_sec"], 14400)
        self.assertIn("-rename", preview["argv"])
        self.assertEqual(preview["argv"][preview["argv"].index("--q") + 1], "9279")
        self.assertTrue(str(preview["log_file"]).endswith("filebot-job-1.log"))

    def test_output_validation_accepts_alias_and_rejects_wrong_title(self):
        identity = ResolvedIdentity(
            media_type="movie",
            tmdb_id=9279,
            title="Un padre en apuros",
            original_title="Jingle All the Way",
            year=1996,
            aliases=["Un padre en apuros", "Jingle All the Way"],
            score=100,
            margin=50,
            query="Un padre en apuros",
            guess={},
            source="search",
        )
        resolver, _ = self.resolver({})

        self.assertTrue(resolver.output_matches(identity, ["Un padre en apuros (1996)"]))
        self.assertFalse(
            resolver.output_matches(
                identity, ["El padre La venganza tiene un precio (2018)"]
            )
        )

    def test_resolver_tries_bilingual_title_candidates_for_movie(self):
        correct = movie_payload(845781, "Codigo Traje Rojo", "Red One", 2024)

        def search(params):
            if params.get("query") == "Codigo Traje Rojo":
                return {"results": [correct]}
            return {"results": []}

        routes = {
            "/search/movie": search,
            "/movie/845781": correct,
        }
        resolver, session = self.resolver(routes)
        input_root = self.input_file("Red One (Codigo Traje Rojo) (2024) cast.mp4")

        identity = resolver.resolve(
            {"category": "movies", "name": "Red One (Codigo Traje Rojo) (2024)"},
            input_root,
        )

        self.assertEqual(identity.tmdb_id, 845781)
        self.assertTrue(
            any(call[1].get("query") == "Codigo Traje Rojo" for call in session.calls)
        )

    def test_resolver_uses_cleaned_tv_title_for_s03e53(self):
        correct = tv_payload(1, "La reina del flow", "La reina del flow", 2018, seasons=3)

        def search(params):
            self.assertNotIn("S03", params.get("query", ""))
            return {"results": [correct]}

        routes = {
            "/search/tv": search,
            "/tv/1": correct,
        }
        resolver, _ = self.resolver(routes)
        input_root = self.input_file("La reina del flow S03 E53 (2026) NETFLIX.mkv")

        identity = resolver.resolve(
            {"category": "tv", "name": "La reina del flow S03 E53 (2026) NETFLIX"},
            input_root,
        )

        self.assertEqual(identity.tmdb_id, 1)
        self.assertEqual(identity.season, 3)
        self.assertEqual(identity.episodes, [53])

    def test_resolver_drops_torrente_release_tail_before_tmdb(self):
        correct = movie_payload(1217584, "Torrente Presidente", "Torrente Presidente", 2026)

        def search(params):
            if params.get("query") == "Torrente presidente":
                return {"results": [correct]}
            return {"results": []}

        routes = {
            "/search/movie": search,
            "/movie/1217584": correct,
        }
        resolver, session = self.resolver(routes)
        input_root = self.input_file("Torrente.presidente.2026.Pm.TS.1O8Op.mkv")

        identity = resolver.resolve(
            {"category": "movies", "name": "Torrente.presidente.2026.Pm.TS.1O8Op"},
            input_root,
        )

        self.assertEqual(identity.tmdb_id, 1217584)
        self.assertFalse(
            any("Pm" in call[1].get("query", "") for call in session.calls)
        )

    def test_resolver_prefers_parser_title_when_guessit_truncates(self):
        correct = movie_payload(58233, "Johnny English Returns", "Johnny English Reborn", 2011)
        correct["alternative_titles"] = {"titles": [{"title": "Johnny English"}]}

        def search(params):
            if params.get("query") == "Johnny English":
                return {"results": [correct]}
            return {"results": []}

        routes = {
            "/search/movie": search,
            "/movie/58233": correct,
        }
        resolver, session = self.resolver(routes)
        input_root = self.input_file("Johnny.English.2011.mkv")

        identity = resolver.resolve(
            {"category": "movies", "name": "Johnny.English.2011"},
            input_root,
        )

        self.assertEqual(identity.tmdb_id, 58233)
        self.assertTrue(
            any(call[1].get("query") == "Johnny English" for call in session.calls)
        )

    def test_resolver_uses_parser_title_for_o_retorno(self):
        correct = movie_payload(58233, "Johnny English Returns", "Johnny English Reborn", 2011)
        correct["alternative_titles"] = {"titles": [{"title": "O Retorno de Johnny English"}]}

        def search(params):
            if params.get("query") == "O Retorno de Johnny English":
                return {"results": [correct]}
            return {"results": []}

        routes = {
            "/search/movie": search,
            "/movie/58233": correct,
        }
        resolver, _ = self.resolver(routes)
        input_root = self.input_file("O Retorno de Johnny English 2011 (1080p).mkv")

        identity = resolver.resolve(
            {"category": "movies", "name": "O Retorno de Johnny English 2011 (1080p)"},
            input_root,
        )

        self.assertEqual(identity.tmdb_id, 58233)

    def test_resolver_recovers_missing_c_spanish_title(self):
        correct = tv_payload(
            285404,
            "Satisfaccion garantizada",
            "Maximum Pleasure Guaranteed",
            2026,
            seasons=1,
        )

        def search(params):
            if params.get("query") == "Satisfaccion garantizada":
                return {"results": [correct]}
            return {"results": []}

        routes = {
            "/search/tv": search,
            "/tv/285404": correct,
        }
        resolver, session = self.resolver(routes)
        input_root = self.input_file("Satisfacion garantizada [HDTV 1080p][Cap.101].mkv")

        identity = resolver.resolve(
            {"category": "tv", "name": "Satisfacion garantizada [HDTV 1080p][Cap.101]"},
            input_root,
        )

        self.assertEqual(identity.tmdb_id, 285404)
        self.assertTrue(
            any(call[1].get("query") == "Satisfaccion garantizada" for call in session.calls)
        )

    def test_resolver_keeps_ambiguous_la_agencia_manual(self):
        current = tv_payload(219971, "La Agencia", "The Agency", 2024, seasons=2)
        older = tv_payload(1537, "La Agencia", "La Agencia", 2001, seasons=2)
        routes = {
            "/search/tv": {"results": [current, older]},
            "/tv/219971": current,
            "/tv/1537": older,
        }
        resolver, _ = self.resolver(routes)
        input_root = self.input_file("La Agencia [Cap.201].mkv")

        with self.assertRaises(ResolverAmbiguous):
            resolver.resolve(
                {"category": "tv", "name": "La Agencia [Cap.201]"},
                input_root,
            )

    def test_resolver_uses_3x41_as_tv_context(self):
        correct = tv_payload(2, "La reina del flow", "La reina del flow", 2018, seasons=3)
        routes = {
            "/search/tv": {"results": [correct]},
            "/tv/2": correct,
        }
        resolver, _ = self.resolver(routes)
        input_root = self.input_file("la reina del flow.3x41.1080.mkv")

        identity = resolver.resolve(
            {"category": "tv", "name": "la reina del flow.3x41.1080"}, input_root
        )

        self.assertEqual(identity.season, 3)
        self.assertEqual(identity.episodes, [41])

    def test_resolver_accepts_cap_3401_as_tv_episode_context(self):
        correct = tv_payload(3, "Los Simpsons", "The Simpsons", 1989, seasons=36)
        routes = {
            "/search/tv": {"results": [correct]},
            "/tv/3": correct,
        }
        resolver, _ = self.resolver(routes)
        input_root = self.input_file("Los Simpsons - Temporada 34 [Cap.3401].mkv")

        identity = resolver.resolve(
            {"category": "tv", "name": "Los Simpsons - Temporada 34 [Cap.3401]"},
            input_root,
        )

        self.assertEqual(identity.season, 34)
        self.assertEqual(identity.episodes, [1])

    def test_resolver_keeps_absolute_episode_without_penalizing_missing_season(self):
        correct = tv_payload(4, "Lejos de Ti", "Lejos de Ti", 2019, seasons=1)
        routes = {
            "/search/tv": {"results": [correct]},
            "/tv/4": correct,
        }
        resolver, _ = self.resolver(routes)
        input_root = self.input_file("Lejos de Ti 1080p Capitulo 14.mp4")

        identity = resolver.resolve(
            {"category": "tv", "name": "Lejos de Ti 1080p Capitulo 14"}, input_root
        )

        self.assertIsNone(identity.season)
        self.assertEqual(identity.episodes, [])
        self.assertEqual(identity.tmdb_id, 4)

    def test_resolver_does_not_search_tmdb_for_manual_non_media_package(self):
        resolver, session = self.resolver({"/search/tv": {"results": []}})
        input_root = self.input_file(
            "Lynda - Scott Simpson - Compleat Course Collection ( Linux, Ubuntu, Shell, CLI..) [AhLaN].mkv"
        )

        with self.assertRaises(ResolverAmbiguous):
            resolver.resolve(
                {
                    "category": "manual",
                    "name": "Lynda - Scott Simpson - Compleat Course Collection ( Linux, Ubuntu, Shell, CLI..) [AhLaN]",
                },
                input_root,
            )

        self.assertEqual(session.calls, [])

    def test_resolver_deduplicates_and_limits_tmdb_searches(self):
        routes = {"/search/movie": {"results": []}}
        resolver, session = self.resolver(routes)
        input_root = self.input_file("Red One (Codigo Traje Rojo) (2024) cast.mp4")

        with self.assertRaises(ResolverAmbiguous):
            resolver.resolve(
                {"category": "movies", "name": "Red One (Codigo Traje Rojo) (2024)"},
                input_root,
            )

        search_calls = [call for call in session.calls if "/search/movie" in call[0]]
        self.assertLessEqual(len(search_calls), 8)


if __name__ == "__main__":
    unittest.main()
