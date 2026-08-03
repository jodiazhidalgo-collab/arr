import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arr_orchestrator.db import Database
from arr_orchestrator.filebot import FileBotRunner, TV_FORMAT
from arr_orchestrator.identity import factory_identity_rules
from arr_orchestrator.identity.resolver.title_candidates import (
    ordered_title_candidates,
    series_title_candidates,
)
from arr_orchestrator.identity.resolver.scoring import score_candidate
from arr_orchestrator.identity.resolver.service import RESOLVER_ALGORITHM_VERSION
from arr_orchestrator.name_resolver import (
    NameResolver,
    ResolutionError,
    ResolvedIdentity,
    ResolverAmbiguous,
    ResolverCandidate,
)


RUNTIME_TEST_ROOT = Path(os.environ["ARR_PYTEST_DATA_DIR"])


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
        return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)


def movie_payload(tmdb_id, title, original_title, year, original_language=""):
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": original_title,
        "original_language": original_language,
        "release_date": f"{year}-01-01" if year else "",
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    }


def tv_payload(tmdb_id, title, original_title, year, seasons=10, original_language=""):
    return {
        "id": tmdb_id,
        "name": title,
        "original_name": original_title,
        "original_language": original_language,
        "first_air_date": f"{year}-01-01",
        "number_of_seasons": seasons,
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
    }


class NameResolverTests(unittest.TestCase):
    def setUp(self):
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=RUNTIME_TEST_ROOT)
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

    @staticmethod
    def oldest_exact_title_rules(enabled=True):
        rules = factory_identity_rules()
        rules["resolver"]["acceptance"][
            "prefer_oldest_exact_title_without_year"
        ] = enabled
        return rules

    def test_preview_turns_tmdb_401_into_safe_json_error(self):
        resolver, _ = self.resolver(
            {"/search/movie": FakeResponse({"token": "must-not-leak"}, status_code=401)}
        )

        payload = resolver.preview("Pelicula de prueba 2024", "movies")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "TMDB_ERROR")
        self.assertEqual(payload["decision"]["status"], "TMDB_ERROR")
        self.assertFalse(payload["decision"]["has_scoring"])
        self.assertIn("HTTP 401", payload["message"])
        self.assertEqual(payload["details"], {})
        self.assertNotIn("must-not-leak", json.dumps(payload, ensure_ascii=False))

    def test_preview_sanitizes_base_resolution_error_details(self):
        def rejected(_params):
            raise ResolutionError(
                "Consulta rechazada",
                {"token": "must-not-leak", "query": "Pelicula de prueba"},
            )

        resolver, _ = self.resolver({"/search/movie": rejected})

        payload = resolver.preview("Pelicula de prueba 2024", "movies")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "TMDB_ERROR")
        self.assertEqual(payload["details"]["token"], "<REDACTED>")
        self.assertEqual(payload["details"]["query"], "Pelicula de prueba")
        self.assertNotIn("must-not-leak", json.dumps(payload, ensure_ascii=False))

    def test_trace_snapshot_is_sanitized_and_independent(self):
        resolver, _ = self.resolver({})
        resolver._trace = {
            "resolver_algorithm_version": RESOLVER_ALGORITHM_VERSION,
            "decision": {
                "status": "ACCEPTED",
                "eligibility_reason": "single_primary_title",
            },
            "token": "must-not-leak",
        }

        snapshot = resolver.trace_snapshot()

        self.assertEqual(
            snapshot["resolver_algorithm_version"],
            RESOLVER_ALGORITHM_VERSION,
        )
        self.assertEqual(snapshot["token"], "<REDACTED>")
        self.assertNotIn("must-not-leak", json.dumps(snapshot, ensure_ascii=False))
        snapshot["decision"]["status"] = "MUTATED"
        self.assertEqual(resolver._trace["decision"]["status"], "ACCEPTED")

    def test_ambiguous_preview_never_reads_filesystem_evidence_or_leaks_paths(self):
        first = movie_payload(1, "El desconocido", "Unknown", 2000)
        second = movie_payload(2, "El desconocido", "Unknown", 2000)
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [first, second]},
                "/movie/1": first,
                "/movie/2": second,
            }
        )
        private_path = str(self.root / "privado" / "no-debe-aparecer.mkv")

        with patch(
            "arr_orchestrator.identity.resolver.service.collect_evidence",
            return_value=[private_path],
        ) as filesystem_evidence:
            payload = resolver.preview("El desconocido.2000.mkv", "movies")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "REJECTED_MARGIN")
        self.assertEqual(payload["decision"]["status"], "REJECTED_MARGIN")
        self.assertTrue(payload["decision"]["score_passed"])
        self.assertFalse(payload["decision"]["margin_passed"])
        self.assertEqual(payload["decision"]["second_score"], payload["decision"]["score"])
        self.assertTrue(payload["decision"]["has_second_candidate"])
        self.assertTrue(payload["candidates"])
        self.assertTrue(payload["candidates"][0]["breakdown"])
        self.assertNotIn("reasons", payload["candidates"][0])
        filesystem_evidence.assert_not_called()
        self.assertNotIn(private_path, json.dumps(payload, ensure_ascii=False))

    def test_original_english_language_breaks_canta_like_ambiguity(self):
        hungarian = movie_payload(427416, "Canta", "Mindenki", 2016, "hu")
        animated = movie_payload(335797, "¡Canta!", "Sing", 2016, "en")
        hungarian_detail = movie_payload(427416, "Canta", "Mindenki", 2016)
        animated_detail = movie_payload(335797, "¡Canta!", "Sing", 2016)
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [hungarian, animated]},
                "/movie/427416": hungarian_detail,
                "/movie/335797": animated_detail,
            }
        )

        payload = resolver.preview("Canta 2016", "movies")

        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 335797)
        self.assertEqual(payload["identity"]["original_language"], "en")
        self.assertEqual(payload["candidates"][0]["tmdb_id"], 335797)
        self.assertFalse(payload["decision"]["margin_passed"])
        self.assertEqual(
            payload["decision"]["original_language_preference"],
            {
                "applied": True,
                "enabled": True,
                "language": "en",
                "selected_original_language": "en",
            },
        )

    def test_configured_french_preference_breaks_the_same_kind_of_ambiguity(self):
        english = movie_payload(1, "La señal", "The Signal", 2024, "en")
        french = movie_payload(2, "La señal", "Le Signal", 2024, "fr")
        rules = factory_identity_rules()
        rules["resolver"]["original_language_preference"]["language"] = "fr-FR"
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [english, french]},
                "/movie/1": english,
                "/movie/2": french,
            }
        )

        payload = resolver.preview("La señal 2024", "movies", rules)

        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 2)
        self.assertEqual(
            payload["decision"]["original_language_preference"]["language"],
            "fr-FR",
        )

    def test_original_language_preference_also_resolves_tv_ambiguity(self):
        spanish = tv_payload(1, "La serie", "La serie", 2020, original_language="es")
        english = tv_payload(2, "La serie", "The Show", 2020, original_language="en")
        resolver, _ = self.resolver(
            {
                "/search/tv": {"results": [spanish, english]},
                "/tv/1": spanish,
                "/tv/2": english,
            }
        )

        payload = resolver.preview("La serie S01E01 2020", "tv")

        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 2)
        self.assertEqual(payload["identity"]["original_language"], "en")
        self.assertTrue(
            payload["decision"]["original_language_preference"]["applied"]
        )

    def test_clear_french_movie_is_not_filtered_by_language_preference(self):
        french = movie_payload(11687, "Los visitantes", "Les Visiteurs", 1993, "fr")
        resolver, _ = self.resolver(
            {"/search/movie": {"results": [french]}, "/movie/11687": french}
        )

        payload = resolver.preview("Los visitantes 1993", "movies")

        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 11687)
        self.assertEqual(payload["identity"]["original_language"], "fr")
        self.assertFalse(
            payload["decision"]["original_language_preference"]["applied"]
        )

    def test_two_english_candidates_remain_ambiguous(self):
        first = movie_payload(1, "El desconocido", "Unknown", 2000, "en")
        second = movie_payload(2, "El desconocido", "Unknown", 2000, "en")
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [first, second]},
                "/movie/1": first,
                "/movie/2": second,
            }
        )

        payload = resolver.preview("El desconocido 2000", "movies")

        self.assertEqual(payload["status"], "REJECTED_MARGIN")
        self.assertFalse(
            payload["decision"]["original_language_preference"]["applied"]
        )

    def test_disabled_original_language_preference_keeps_ambiguity(self):
        hungarian = movie_payload(427416, "Canta", "Mindenki", 2016, "hu")
        animated = movie_payload(335797, "¡Canta!", "Sing", 2016, "en")
        rules = factory_identity_rules()
        rules["resolver"]["original_language_preference"]["enabled"] = False
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [hungarian, animated]},
                "/movie/427416": hungarian,
                "/movie/335797": animated,
            }
        )

        payload = resolver.preview("Canta 2016", "movies", rules)

        self.assertEqual(payload["status"], "REJECTED_MARGIN")
        self.assertEqual(
            [item["tmdb_id"] for item in payload["candidates"]],
            [335797, 427416],
        )
        self.assertEqual(
            payload["decision"]["original_language_preference"]["enabled"],
            False,
        )
        self.assertFalse(
            payload["decision"]["original_language_preference"]["applied"]
        )

    def test_single_english_candidate_is_not_rescued_as_an_ambiguity(self):
        unrelated = movie_payload(99, "Nadaa", "Nadaa", 1900, "en")
        rules = factory_identity_rules()
        rules["resolver"]["acceptance"]["min_score"] = -100
        rules["resolver"]["acceptance"]["min_margin"] = 20
        resolver, _ = self.resolver(
            {"/search/movie": {"results": [unrelated]}, "/movie/99": unrelated}
        )

        payload = resolver.preview("Nada 2024", "movies", rules)

        self.assertEqual(payload["status"], "REJECTED_MARGIN")
        self.assertFalse(payload["decision"]["has_second_candidate"])
        self.assertFalse(
            payload["decision"]["original_language_preference"]["applied"]
        )

    def test_oldest_exact_movie_without_year_is_selected_and_reserved_for_details(self):
        recent = movie_payload(1, "El objetivo", "El objetivo", 2024, "es")
        middle = movie_payload(2, "El objetivo", "El objetivo", 2010, "es")
        discarded_top = movie_payload(3, "El objetivo", "El objetivo", 2000, "es")
        oldest = movie_payload(4, "El objetivo", "El objetivo", 1980, "es")
        resolver, session = self.resolver(
            {
                "/search/movie": {"results": [recent, middle, discarded_top, oldest]},
                "/movie/1": recent,
                "/movie/4": oldest,
            }
        )
        rules = self.oldest_exact_title_rules()
        rules["resolver"]["search_limits"]["initial_candidates"] = 2
        rules["resolver"]["search_limits"]["detail_candidates"] = 2

        payload = resolver.preview(
            "El objetivo",
            "movies",
            rules,
        )

        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 4)
        self.assertEqual([item["tmdb_id"] for item in payload["candidates"]], [4, 1])
        self.assertFalse(payload["decision"]["margin_passed"])
        self.assertEqual(
            payload["decision"]["oldest_exact_title_preference"],
            {
                "applied": True,
                "enabled": True,
                "selected_year": 1980,
                "reason_code": "oldest_exact_title_without_year",
            },
        )
        detail_paths = [call[0].split("/3", 1)[1] for call in session.calls]
        self.assertIn("/movie/4", detail_paths)
        self.assertNotIn("/movie/2", detail_paths)
        self.assertNotIn("/movie/3", detail_paths)

    def test_oldest_preference_requires_a_real_score_tie(self):
        recent = movie_payload(1, "El objetivo", "El objetivo", 2024, "es")
        oldest = movie_payload(2, "El objetivo", "El objetivo", 1980, "es")
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [recent, oldest]},
                "/movie/1": recent,
                "/movie/2": oldest,
            }
        )
        rules = self.oldest_exact_title_rules()
        original_rank = resolver._rank_candidates

        def rank_with_one_point_advantage(candidates, guessed, evidence, direct_identity):
            ranked = original_rank(candidates, guessed, evidence, direct_identity)
            for candidate in ranked:
                candidate.score = 105 if candidate.tmdb_id == 1 else 104
            return sorted(ranked, key=lambda candidate: candidate.score, reverse=True)

        with patch.object(
            NameResolver,
            "_rank_candidates",
            side_effect=rank_with_one_point_advantage,
        ):
            payload = resolver.preview("El objetivo", "movies", rules)

        self.assertEqual(payload["status"], "REJECTED_MARGIN")
        self.assertEqual(payload["decision"]["margin"], 1)
        self.assertFalse(
            payload["decision"]["oldest_exact_title_preference"]["applied"]
        )

    def test_oldest_preference_rejects_other_candidate_inside_margin_zone(self):
        recent = movie_payload(1, "El objetivo", "El objetivo", 2024, "es")
        oldest = movie_payload(2, "El objetivo", "El objetivo", 1980, "es")
        nearby = movie_payload(3, "El objetivo final", "El objetivo final", 2000, "es")
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [recent, oldest, nearby]},
                "/movie/1": recent,
                "/movie/2": oldest,
                "/movie/3": nearby,
            }
        )
        rules = self.oldest_exact_title_rules()
        original_rank = resolver._rank_candidates

        def rank_with_nearby_ambiguity(candidates, guessed, evidence, direct_identity):
            ranked = original_rank(candidates, guessed, evidence, direct_identity)
            scores = {1: 100, 2: 100, 3: 95}
            for candidate in ranked:
                candidate.score = scores[candidate.tmdb_id]
            return sorted(ranked, key=lambda candidate: candidate.score, reverse=True)

        with patch.object(
            NameResolver,
            "_rank_candidates",
            side_effect=rank_with_nearby_ambiguity,
        ):
            payload = resolver.preview("El objetivo", "movies", rules)

        self.assertEqual(payload["status"], "REJECTED_MARGIN")
        self.assertEqual(payload["decision"]["score"], 100)
        self.assertEqual(payload["decision"]["second_score"], 100)
        self.assertFalse(
            payload["decision"]["oldest_exact_title_preference"]["applied"]
        )

    def test_original_language_preference_keeps_priority_over_oldest_movie(self):
        oldest_french = movie_payload(1, "La señal", "Le Signal", 1950, "fr")
        newer_english = movie_payload(2, "La señal", "The Signal", 2020, "en")
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [oldest_french, newer_english]},
                "/movie/1": oldest_french,
                "/movie/2": newer_english,
            }
        )

        payload = resolver.preview(
            "La señal",
            "movies",
            self.oldest_exact_title_rules(),
        )

        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 2)
        self.assertTrue(
            payload["decision"]["original_language_preference"]["applied"]
        )
        self.assertEqual(
            payload["decision"]["oldest_exact_title_preference"],
            {
                "applied": False,
                "enabled": True,
                "selected_year": None,
                "reason_code": None,
            },
        )

    def test_oldest_preference_disabled_keeps_exact_movie_ambiguity(self):
        recent = movie_payload(1, "El objetivo", "El objetivo", 2024, "es")
        oldest = movie_payload(2, "El objetivo", "El objetivo", 1980, "es")
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [recent, oldest]},
                "/movie/1": recent,
                "/movie/2": oldest,
            }
        )

        payload = resolver.preview(
            "El objetivo",
            "movies",
            self.oldest_exact_title_rules(enabled=False),
        )

        self.assertEqual(payload["status"], "REJECTED_MARGIN")
        self.assertEqual(
            payload["decision"]["oldest_exact_title_preference"]["enabled"],
            False,
        )
        self.assertFalse(
            payload["decision"]["oldest_exact_title_preference"]["applied"]
        )

    def test_oldest_preference_does_not_apply_when_input_has_year(self):
        recent = movie_payload(1, "El objetivo", "El objetivo", 2024, "es")
        oldest = movie_payload(2, "El objetivo", "El objetivo", 1980, "es")
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [recent, oldest]},
                "/movie/1": recent,
                "/movie/2": oldest,
            }
        )
        rules = self.oldest_exact_title_rules()
        rules["resolver"]["acceptance"]["min_score"] = 0
        rules["resolver"]["acceptance"]["min_margin"] = 100

        payload = resolver.preview("El objetivo 2024", "movies", rules)

        self.assertEqual(payload["status"], "REJECTED_MARGIN")
        self.assertFalse(
            payload["decision"]["oldest_exact_title_preference"]["applied"]
        )

    def test_equal_oldest_year_keeps_exact_movie_ambiguity(self):
        first_oldest = movie_payload(1, "El objetivo", "El objetivo", 1980, "es")
        second_oldest = movie_payload(2, "El objetivo", "El objetivo", 1980, "es")
        recent = movie_payload(3, "El objetivo", "El objetivo", 2024, "es")
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [first_oldest, second_oldest, recent]},
                "/movie/1": first_oldest,
                "/movie/2": second_oldest,
                "/movie/3": recent,
            }
        )

        payload = resolver.preview(
            "El objetivo",
            "movies",
            self.oldest_exact_title_rules(),
        )

        self.assertEqual(payload["status"], "REJECTED_MARGIN")
        self.assertFalse(
            payload["decision"]["oldest_exact_title_preference"]["applied"]
        )

    def test_oldest_reservation_never_evicts_a_non_exact_ambiguity(self):
        exact_recent = movie_payload(1, "Objetivo", "Objetivo", 2024, "es")
        non_exact = movie_payload(2, "Objetivo final", "Objetivo final", 2020, "es")
        exact_middle = movie_payload(3, "Objetivo", "Objetivo", 2000, "es")
        exact_oldest = movie_payload(4, "Objetivo", "Objetivo", 1980, "es")
        resolver, session = self.resolver(
            {
                "/search/movie": {
                    "results": [exact_recent, non_exact, exact_middle, exact_oldest]
                },
                "/movie/1": exact_recent,
                "/movie/2": non_exact,
                "/movie/3": exact_middle,
            }
        )
        rules = self.oldest_exact_title_rules()
        rules["resolver"]["acceptance"]["min_score"] = 0
        rules["resolver"]["acceptance"]["min_margin"] = 100
        for key in (
            "title_exact",
            "title_similarity_max",
            "token_overlap_max",
            "parser_exact",
            "parser_near",
            "origin_evidence",
        ):
            rules["resolver"]["scoring"][key] = 0

        payload = resolver.preview("Objetivo", "movies", rules)

        self.assertEqual(payload["status"], "REJECTED")
        self.assertEqual(
            payload["decision"]["status"],
            "REJECTED",
        )
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "title_selection_uncertain",
        )
        self.assertTrue(payload["decision"]["eligibility_blocked"])
        self.assertTrue(payload["selection_uncertain"])
        self.assertEqual(
            [item["tmdb_id"] for item in payload["candidates"]],
            [1, 2, 3],
        )
        self.assertFalse(
            payload["decision"]["oldest_exact_title_preference"]["applied"]
        )
        detail_paths = [call[0].split("/3", 1)[1] for call in session.calls]
        self.assertEqual(
            [path for path in detail_paths if path.startswith("/movie/")],
            ["/movie/1", "/movie/2", "/movie/3"],
        )
        self.assertNotIn("/movie/4", detail_paths)

    def test_preview_preserves_zero_min_score_when_classifying_rejection(self):
        resolver, _ = self.resolver({})
        ambiguity = ResolverAmbiguous(
            "La identidad no supera el umbral de seguridad",
            {
                "top_score": 10,
                "margin": 5,
                "min_score": 0,
                "min_margin": 12,
            },
        )

        with patch.object(NameResolver, "resolve", side_effect=ambiguity):
            payload = resolver.preview("Titulo de prueba.2024", "movies")

        self.assertEqual(payload["status"], "REJECTED_MARGIN")
        self.assertEqual(payload["details"]["min_score"], 0)

    def test_preview_direct_and_forced_id_use_structured_bypass_breakdown(self):
        direct = movie_payload(9, "Objetivo", "Target", 2024)
        resolver, _ = self.resolver({"/movie/9": direct})

        direct_payload = resolver.preview("Objetivo.tmdb-9.2024.mkv", "movies")

        self.assertEqual(direct_payload["status"], "ACCEPTED")
        self.assertTrue(direct_payload["decision"]["bypass"])
        self.assertEqual(direct_payload["decision"]["source"], "tmdb_id")
        self.assertEqual(
            direct_payload["decision"]["eligibility_reason"],
            "tmdb_id_validated",
        )
        self.assertEqual(
            direct_payload["identity"]["resolver_algorithm_version"],
            RESOLVER_ALGORITHM_VERSION,
        )
        self.assertFalse(direct_payload["decision"]["has_second_candidate"])
        self.assertEqual(
            direct_payload["candidates"][0]["breakdown"],
            [
                {
                    "key": "direct_identity",
                    "path": "resolver.scoring.direct_identity",
                    "configured": 200,
                    "applied": 200,
                }
            ],
        )

        forced_resolver, _ = self.resolver({"/movie/9": direct})
        forced_resolver.configure_rules(
            {"movies": {"forced_matches": ["Objetivo | 2024 | 9"]}}
        )

        forced_payload = forced_resolver.preview("Objetivo.2024.mkv", "movies")

        self.assertEqual(forced_payload["status"], "ACCEPTED")
        self.assertTrue(forced_payload["decision"]["bypass"])
        self.assertEqual(forced_payload["decision"]["source"], "forced_match")
        self.assertEqual(
            forced_payload["decision"]["eligibility_reason"],
            "forced_match_validated",
        )
        self.assertEqual(
            forced_payload["candidates"][0]["breakdown"][0]["key"],
            "direct_identity",
        )
        self.assertEqual(
            forced_payload["candidates"][0]["title_match_level"],
            "direct",
        )

    def test_preview_score_rejection_and_no_candidates_have_distinct_decisions(self):
        wrong = movie_payload(22, "Objetiv", "Objetiv", 2024)
        resolver, _ = self.resolver(
            {"/search/movie": {"results": [wrong]}, "/movie/22": wrong}
        )

        rejected = resolver.preview("Objetivo.2024.mkv", "movies")

        self.assertEqual(rejected["status"], "REJECTED_SCORE")
        self.assertTrue(rejected["decision"]["has_scoring"])
        self.assertFalse(rejected["decision"]["score_passed"])
        self.assertTrue(rejected["decision"]["margin_passed"])

        empty_resolver, _ = self.resolver({"/search/movie": {"results": []}})
        empty = empty_resolver.preview("Objetivo.2024.mkv", "movies")

        self.assertEqual(empty["status"], "NO_CANDIDATES")
        self.assertEqual(empty["details"]["reason_code"], "no_candidates")
        self.assertFalse(empty["decision"]["has_scoring"])
        self.assertNotIn("score", empty["decision"])

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

    def test_rules_defaults_keep_constructor_language_and_region(self):
        correct = movie_payload(9279, "Un padre en apuros", "Jingle All the Way", 1996)
        routes = {
            "/search/movie": {"results": [correct]},
            "/movie/9279": correct,
        }
        resolver, session = self.resolver(routes)
        resolver.configure_rules({})
        input_root = self.input_file("Un.padre.en.apuros.1996.mkv")

        resolver.resolve(
            {"category": "movies", "name": "Un.padre.en.apuros.1996"},
            input_root,
        )

        search_call = next(call for call in session.calls if "/search/movie" in call[0])
        self.assertEqual(search_call[1]["language"], "es-ES")
        self.assertEqual(search_call[1]["region"], "ES")

    def test_resolver_honors_100ms_http_timeout_without_500ms_clamp(self):
        correct = movie_payload(9279, "Un padre en apuros", "Jingle All the Way", 1996)
        session = FakeSession(
            {
                "/search/movie": {"results": [correct]},
                "/movie/9279": correct,
            }
        )
        resolver = NameResolver(
            "token",
            "es-ES",
            "ES",
            100,
            5000,
            self.database,
            session=session,
        )
        input_root = self.input_file("Un.padre.en.apuros.1996.mkv")

        resolver.resolve(
            {"category": "movies", "name": "Un.padre.en.apuros.1996"},
            input_root,
        )

        self.assertTrue(session.calls)
        self.assertTrue(all(abs(call[2] - 0.1) < 0.001 for call in session.calls))

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
        cache_trace = resolver.trace_snapshot()

        self.assertEqual(first.tmdb_id, second.tmdb_id)
        self.assertEqual(len(session.calls), call_count)
        self.assertEqual(second.source, "cache")
        self.assertEqual(
            first.resolver_algorithm_version,
            RESOLVER_ALGORITHM_VERSION,
        )
        self.assertEqual(
            second.resolver_algorithm_version,
            RESOLVER_ALGORITHM_VERSION,
        )
        self.assertTrue(cache_trace["cache_hit"])
        self.assertEqual(cache_trace["candidates"], [])
        self.assertEqual(
            cache_trace["resolver_algorithm_version"],
            RESOLVER_ALGORITHM_VERSION,
        )
        self.assertEqual(
            cache_trace["decision"],
            {
                "status": "ACCEPTED",
                "accepted": True,
                "has_scoring": True,
                "source": "cache",
                "cache_reused": True,
                "resolver_algorithm_version": RESOLVER_ALGORITHM_VERSION,
                "eligibility_reason": "cache_v3_reuse",
                "eligibility_blocked": False,
                "score": second.score,
                "margin": second.margin,
            },
        )
        self.assertNotIn("candidate_provenance", cache_trace)
        self.assertNotIn("search_strategy", cache_trace)

    def test_cache_signature_uses_resolution_rules_but_ignores_formatting(self):
        correct = movie_payload(9279, "Un padre en apuros", "Jingle All the Way", 1996)
        routes = {
            "/search/movie": {"results": [correct]},
            "/movie/9279": correct,
        }
        resolver, session = self.resolver(routes)
        input_root = self.input_file("Un padre en apuros.1996.mkv")
        job = {"category": "movies", "name": "Un padre en apuros.1996"}

        resolver.configure_rules(
            {"movies": {"language": "es-ES", "region": "ES", "format": "A"}}
        )
        resolver.resolve(job, input_root)
        first_call_count = len(session.calls)
        resolver.configure_rules(
            {"movies": {"language": "es-ES", "region": "ES", "format": "B"}}
        )
        cached = resolver.resolve(job, input_root)

        self.assertEqual(cached.source, "cache")
        self.assertEqual(len(session.calls), first_call_count)

        resolver.configure_rules(
            {"movies": {"language": "en-US", "region": "ES", "format": "B"}}
        )
        refreshed = resolver.resolve(job, input_root)

        self.assertNotEqual(refreshed.source, "cache")
        self.assertGreater(len(session.calls), first_call_count)

    def test_the_visitors_merges_languages_and_selects_exact_year(self):
        correct_es = movie_payload(11687, "Los visitantes", "Les Visiteurs", 1993)
        correct_en = movie_payload(11687, "The Visitors", "Les Visiteurs", 1993)
        wrong = movie_payload(1554591, "The Visitors", "The Visitors", None)
        older = movie_payload(102699, "The Visitors", "The Visitors", 1972)

        def search(params):
            if params.get("language") == "en-US" and params.get("year") == 1993:
                return {"results": [correct_en]}
            if params.get("year") == 1993:
                return {"results": [correct_es]}
            return {"results": [wrong, older]}

        routes = {
            "/search/movie": search,
            "/movie/11687": correct_es,
            "/movie/1554591": wrong,
            "/movie/102699": older,
        }
        resolver, session = self.resolver(routes)
        input_root = self.input_file(
            "The.Visitors.1993.FRENCH.REMASTERED.1080p.BluRay.H264.AAC-VXT.mp4"
        )

        identity = resolver.resolve(
            {
                "category": "movies",
                "name": "The.Visitors.1993.FRENCH.REMASTERED.1080p.BluRay.H264.AAC-VXT",
            },
            input_root,
        )

        self.assertEqual(identity.tmdb_id, 11687)
        self.assertEqual(identity.year, 1993)
        self.assertIn("The Visitors", identity.aliases)
        self.assertIn("Los visitantes", identity.aliases)
        search_calls = [call for call in session.calls if "/search/movie" in call[0]]
        self.assertEqual(
            [call[1]["language"] for call in search_calls],
            ["es-ES", "en-US"],
        )
        detail_calls = [call for call in session.calls if "/movie/" in call[0]]
        self.assertLessEqual(len(detail_calls), 3)

    def test_movie_with_year_never_early_stops_on_yearless_candidate(self):
        missing_year = movie_payload(1554591, "The Visitors", "The Visitors", None)
        correct = movie_payload(11687, "The Visitors", "Les Visiteurs", 1993)
        search_number = 0

        def search(_params):
            nonlocal search_number
            search_number += 1
            return {"results": [missing_year if search_number == 1 else correct]}

        routes = {
            "/search/movie": search,
            "/movie/11687": correct,
            "/movie/1554591": missing_year,
        }
        resolver, session = self.resolver(routes)
        resolver.configure_rules(
            {"movies": {"query_aliases": ["The Visitors | The Visitors"]}}
        )
        input_root = self.input_file("The.Visitors.1993.mkv")

        identity = resolver.resolve(
            {"category": "movies", "name": "The.Visitors.1993"}, input_root
        )

        self.assertEqual(identity.tmdb_id, 11687)
        search_calls = [call for call in session.calls if "/search/movie" in call[0]]
        self.assertGreaterEqual(len(search_calls), 2)

    def test_missing_movie_year_has_fixed_safety_penalty(self):
        resolver, _ = self.resolver({})
        candidate = ResolverCandidate(
            tmdb_id=1554591,
            media_type="movie",
            title="The Visitors",
            original_title="The Visitors",
            year=None,
            aliases=["The Visitors"],
        )

        score, breakdown = resolver._score_candidate(
            candidate,
            {"title": "The Visitors", "year": 1993},
            [],
            False,
        )

        self.assertEqual(score, 52.0)
        missing_year = next(item for item in breakdown if item["key"] == "missing_movie_year")
        self.assertEqual(missing_year["configured"], -18)
        self.assertEqual(missing_year["applied"], -18)
        self.assertAlmostEqual(
            sum(float(item["applied"]) for item in breakdown),
            score,
            places=2,
        )

    def test_query_alias_is_applied_without_changing_default_call_contract(self):
        correct = movie_payload(11687, "The Visitors", "Les Visiteurs", 1993)

        def search(params):
            if params.get("query") == "The Visitors":
                return {"results": [correct]}
            return {"results": []}

        routes = {
            "/search/movie": search,
            "/movie/11687": correct,
        }
        resolver, session = self.resolver(routes)
        resolver.configure_rules(
            {
                "movies": {
                    "language": "es-ES",
                    "region": "ES",
                    "query_aliases": ["Visitantes del tiempo | The Visitors"],
                }
            }
        )
        input_root = self.input_file("Visitantes.del.tiempo.1993.mkv")

        identity = resolver.resolve(
            {"category": "movies", "name": "Visitantes.del.tiempo.1993"},
            input_root,
        )

        self.assertEqual(identity.tmdb_id, 11687)
        self.assertTrue(
            any(call[1].get("query") == "The Visitors" for call in session.calls)
        )

    def test_preview_configured_alias_keeps_explicit_primary_authority(self):
        correct = movie_payload(11687, "The Visitors", "Les Visiteurs", 1993)
        false_primary = movie_payload(
            9001,
            "Visitantes del tiempo",
            "Visitantes del tiempo",
            1993,
        )

        def search(params):
            if params.get("query") == "The Visitors":
                return {"results": [correct]}
            return {"results": [false_primary]}

        resolver, _ = self.resolver(
            {
                "/search/movie": search,
                "/movie/11687": correct,
                "/movie/9001": false_primary,
            }
        )
        resolver.configure_rules(
            {
                "movies": {
                    "query_aliases": ["Visitantes del tiempo | The Visitors"],
                }
            }
        )

        payload = resolver.preview("Visitantes.del.tiempo.1993.mkv", "movies")

        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 11687)
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "configured_primary",
        )
        self.assertEqual(payload["candidates"][0]["title_match_level"], "configured")
        self.assertIn(
            "configured",
            payload["candidates"][0]["search_provenance"]["sources"],
        )
        self.assertEqual(
            payload["search_strategy"]["early_stop_reason"],
            "configured_primary_confirmed",
        )

    def test_configured_alias_year_arbitration_preserves_safe_cases(self):
        primary_2020 = movie_payload(2, "Target", "Target", 2020)

        def preview_for(configured, name):
            def search(params):
                if params.get("query") == "Configured Target":
                    return {"results": [configured]}
                if params.get("query") == "Target":
                    return {"results": [primary_2020]}
                return {"results": []}

            resolver, _ = self.resolver(
                {
                    "/search/movie": search,
                    f"/movie/{configured['id']}": configured,
                    "/movie/2": primary_2020,
                }
            )
            resolver.configure_rules(
                {
                    "movies": {
                        "query_aliases": ["Target | Configured Target"],
                    }
                }
            )
            return resolver.preview(name, "movies")

        mismatched = preview_for(
            movie_payload(1, "Configured Target", "Configured Target", 1990),
            "Target (2020).mkv",
        )
        without_year = preview_for(
            movie_payload(3, "Configured Target", "Configured Target", 1990),
            "Target.mkv",
        )
        exact_year = preview_for(
            movie_payload(4, "Configured Target", "Configured Target", 2020),
            "Target (2020).mkv",
        )

        self.assertEqual(mismatched["status"], "ACCEPTED")
        self.assertEqual(mismatched["identity"]["tmdb_id"], 2)
        self.assertEqual(
            mismatched["decision"]["eligibility_reason"],
            "single_primary_title",
        )
        for payload, expected_id in ((without_year, 3), (exact_year, 4)):
            with self.subTest(expected_id=expected_id):
                self.assertEqual(payload["status"], "ACCEPTED")
                self.assertEqual(payload["identity"]["tmdb_id"], expected_id)
                self.assertEqual(
                    payload["decision"]["eligibility_reason"],
                    "configured_primary",
                )

    def test_tv_wrong_year_configured_alias_cannot_stop_before_primary(self):
        configured_1995 = tv_payload(
            10,
            "The Office",
            "The Office",
            1995,
            seasons=6,
        )
        primary_2005 = tv_payload(
            20,
            "La oficina",
            "La oficina",
            2005,
            seasons=9,
        )

        def search(params):
            if params.get("query") == "The Office":
                return {"results": [configured_1995]}
            if params.get("query") == "La oficina":
                return {"results": [primary_2005]}
            return {"results": []}

        resolver, session = self.resolver(
            {
                "/search/tv": search,
                "/tv/10": configured_1995,
                "/tv/20": primary_2005,
            }
        )
        resolver.configure_rules(
            {
                "tv": {
                    "query_aliases": ["La oficina | The Office"],
                }
            }
        )

        payload = resolver.preview(
            "La oficina (2005) S02E01.mkv",
            "tv",
        )

        search_queries = [
            call[1].get("query")
            for call in session.calls
            if "/search/tv" in call[0]
        ]
        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 20)
        self.assertEqual(payload["identity"]["season"], 2)
        self.assertEqual(search_queries[:2], ["The Office", "La oficina"])
        self.assertEqual(len(search_queries), 2)
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "single_primary_title",
        )
        self.assertEqual(
            payload["search_strategy"]["early_stop_reason"],
            "single_primary_confirmed",
        )
        self.assertFalse(payload["search_strategy"]["early_detail_attempted"])

    def test_tv_configured_alias_without_season_count_waits_for_primary(self):
        configured_search = tv_payload(
            30,
            "The Office",
            "The Office",
            2005,
            seasons=1,
        )
        configured_search.pop("number_of_seasons")
        configured_detail = tv_payload(
            30,
            "The Office",
            "The Office",
            2005,
            seasons=1,
        )
        primary = tv_payload(
            40,
            "La oficina",
            "La oficina",
            2005,
            seasons=9,
        )

        def search(params):
            return {
                "results": (
                    [configured_search]
                    if params.get("query") == "The Office"
                    else [primary]
                    if params.get("query") == "La oficina"
                    else []
                )
            }

        resolver, session = self.resolver(
            {
                "/search/tv": search,
                "/tv/30": configured_detail,
                "/tv/40": primary,
            }
        )
        resolver.configure_rules(
            {"tv": {"query_aliases": ["La oficina | The Office"]}}
        )

        payload = resolver.preview("La oficina (2005) S02E01.mkv", "tv")
        queries = [
            call[1].get("query")
            for call in session.calls
            if "/search/tv" in call[0]
        ]

        self.assertEqual(queries[:2], ["The Office", "La oficina"])
        self.assertGreaterEqual(len(queries), 2)
        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 40)
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "single_primary_title",
        )
        self.assertFalse(payload["search_strategy"]["early_detail_attempted"])

    def test_configured_query_alias_runs_before_false_automatic_winner(self):
        correct = movie_payload(11687, "The Visitors", "Les Visiteurs", 1993)
        false_top = movie_payload(
            9001, "Visitantes del tiempo", "Visitantes del tiempo", 1993
        )
        decoy_one = movie_payload(9002, "Visitors Center", "Visitors Center", 1993)
        decoy_two = movie_payload(9003, "A Visitor", "A Visitor", 1993)

        def search(params):
            if params.get("query") == "The Visitors":
                return {"results": [decoy_one, correct, decoy_two]}
            return {"results": [false_top]}

        routes = {
            "/search/movie": search,
            "/movie/11687": correct,
            "/movie/9001": false_top,
            "/movie/9002": decoy_one,
            "/movie/9003": decoy_two,
        }
        resolver, session = self.resolver(routes)
        resolver.configure_rules(
            {
                "movies": {
                    "query_aliases": ["Visitantes del tiempo | The Visitors"],
                }
            }
        )
        input_root = self.input_file("Visitantes.del.tiempo.1993.mkv")

        identity = resolver.resolve(
            {"category": "movies", "name": "Visitantes.del.tiempo.1993"},
            input_root,
        )

        search_calls = [call for call in session.calls if "/search/movie" in call[0]]
        self.assertEqual(search_calls[0][1]["query"], "The Visitors")
        self.assertEqual(identity.tmdb_id, 11687)

    def test_valid_forced_match_is_validated_through_tmdb_details(self):
        correct = movie_payload(11687, "Los visitantes", "Les Visiteurs", 1993)
        correct["translations"] = {
            "translations": [{"data": {"title": "The Visitors"}}]
        }
        resolver, session = self.resolver({"/movie/11687": correct})
        resolver.configure_rules(
            {
                "movies": {
                    "forced_matches": ["The Visitors | 1993 | 11687"],
                }
            }
        )
        input_root = self.input_file("The.Visitors.1993.mkv")

        identity = resolver.resolve(
            {"category": "movies", "name": "The.Visitors.1993"}, input_root
        )

        self.assertEqual(identity.tmdb_id, 11687)
        self.assertEqual(identity.source, "forced_match")
        self.assertEqual(len(session.calls), 1)
        self.assertIn("/movie/11687", session.calls[0][0])

    def test_forced_match_rejects_tmdb_year_mismatch(self):
        wrong_year = movie_payload(11687, "Los visitantes", "Les Visiteurs", 1992)
        wrong_year["translations"] = {
            "translations": [{"data": {"title": "The Visitors"}}]
        }
        resolver, _ = self.resolver({"/movie/11687": wrong_year})
        resolver.configure_rules(
            {"movies": {"forced_matches": ["The Visitors | 1993 | 11687"]}}
        )
        input_root = self.input_file("The.Visitors.1993.mkv")

        with self.assertRaises(ResolverAmbiguous):
            resolver.resolve(
                {"category": "movies", "name": "The.Visitors.1993"}, input_root
            )

    def test_forced_match_rejects_wrong_tmdb_title_with_same_year(self):
        wrong_movie = movie_payload(999, "Parque Jurasico", "Jurassic Park", 1993)
        resolver, _ = self.resolver({"/movie/999": wrong_movie})
        resolver.configure_rules(
            {"movies": {"forced_matches": ["The Visitors | 1993 | 999"]}}
        )
        input_root = self.input_file("The.Visitors.1993.mkv")

        with self.assertRaises(ResolverAmbiguous) as context:
            resolver.resolve(
                {"category": "movies", "name": "The.Visitors.1993"}, input_root
            )

        self.assertIn("titulos reales", str(context.exception))

    def test_tv_forced_match_without_year_validates_real_alias(self):
        correct = tv_payload(77, "La Agencia", "The Agency", 2024, seasons=2)
        correct["alternative_titles"] = {
            "results": [{"title": "Agency Alias"}]
        }
        resolver, session = self.resolver({"/tv/77": correct})
        resolver.configure_rules(
            {"tv": {"forced_matches": ["Agency Alias | 77"]}}
        )
        input_root = self.input_file("Agency.Alias.S01E01.mkv")

        identity = resolver.resolve(
            {"category": "tv", "name": "Agency.Alias.S01E01"}, input_root
        )

        self.assertEqual(identity.tmdb_id, 77)
        self.assertEqual(identity.source, "forced_match")
        self.assertEqual(len(session.calls), 1)

    def test_embedded_tmdb_id_has_priority_over_forced_match(self):
        explicit = movie_payload(999, "The Visitors", "The Visitors", 1993)
        resolver, session = self.resolver({"/movie/999": explicit})
        resolver.configure_rules(
            {"movies": {"forced_matches": ["The Visitors | 1993 | 11687"]}}
        )
        input_root = self.input_file("The.Visitors.1993.tmdb-999.mkv")

        identity = resolver.resolve(
            {"category": "movies", "name": "The.Visitors.1993.tmdb-999"},
            input_root,
        )

        self.assertEqual(identity.tmdb_id, 999)
        self.assertEqual(identity.source, "tmdb_id")
        self.assertEqual(len(session.calls), 1)
        self.assertIn("/movie/999", session.calls[0][0])

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

    def test_guided_filebot_uses_identity_locale_and_fixed_format(self):
        identity = ResolvedIdentity(
            media_type="tv",
            tmdb_id=77,
            title="La Agencia",
            original_title="The Agency",
            year=2024,
            aliases=["La Agencia", "The Agency"],
            score=100,
            margin=50,
            query="La Agencia",
            guess={"title": "La Agencia", "season": 1, "episode": 1},
            source="search",
            season=1,
            episodes=[1],
        )
        runner = FileBotRunner("filebot", self.root)
        runner.configure_identity_rules(
            {"resolver": {"locales": {"tv": {"language": "fr-FR"}}}}
        )

        guided = runner.preview_command(
            "job-guided",
            "tv",
            self.root / "input",
            self.root / "output",
            identity,
        )
        legacy = runner.preview_command(
            "job-legacy",
            "tv",
            self.root / "input",
            self.root / "output",
        )

        guided_argv = guided["argv"]
        self.assertEqual(guided_argv[guided_argv.index("--lang") + 1], "fr")
        self.assertEqual(
            guided_argv[guided_argv.index("--format") + 1],
            TV_FORMAT,
        )
        self.assertNotIn("{t}", TV_FORMAT)
        self.assertNotIn("--order", guided_argv)
        self.assertEqual(guided["rules"]["language"], "fr-FR")
        self.assertEqual(guided["rules"]["format"], TV_FORMAT)
        self.assertEqual(legacy["argv"][legacy["argv"].index("--lang") + 1], "es")
        self.assertEqual(legacy["rules"]["language"], "es")
        self.assertEqual(legacy["rules"]["format"], TV_FORMAT)

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

    def test_preview_simple_primary_keeps_score_and_stops_after_one_search(self):
        correct = movie_payload(50, "Objetivo", "Objetivo", 2024)
        resolver, session = self.resolver(
            {"/search/movie": {"results": [correct]}, "/movie/50": correct}
        )

        payload = resolver.preview("Objetivo.2024.mkv", "movies")

        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 50)
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "single_primary_title",
        )
        self.assertGreaterEqual(payload["decision"]["score"], 75)
        self.assertEqual(payload["candidates"][0]["title_match_level"], "primary")
        search_calls = [
            call for call in session.calls if "/search/movie" in call[0]
        ]
        self.assertEqual(len(search_calls), 1)
        self.assertEqual(payload["search_strategy"]["early_stop_phase"], "primary")
        self.assertEqual(
            payload["search_strategy"]["early_stop_reason"],
            "single_primary_confirmed",
        )
        self.assertFalse(payload["search_strategy"]["early_detail_attempted"])
        self.assertFalse(payload["search_strategy"]["early_detail_reused"])
        self.assertEqual(payload["search_strategy"]["detail_requests"], 1)
        self.assertEqual(
            [call[0].split("/3", 1)[1] for call in session.calls if "/movie/" in call[0]],
            ["/movie/50"],
        )

    def test_incontrolable_progressive_detail_fixes_regional_search_year_and_reuses_it(self):
        regional_search = movie_payload(1317149, "I Swear", "I Swear", 2026)
        detailed = movie_payload(1317149, "Incontrolable", "I Swear", 2025)

        def search(params):
            return {
                "results": [regional_search]
                if params.get("query") == "Incontrolable"
                else []
            }

        resolver, session = self.resolver(
            {
                "/search/movie": search,
                "/movie/1317149": detailed,
            }
        )

        payload = resolver.preview(
            "Incontrolable (I Swear) (2025).mkv",
            "movies",
        )

        search_calls = [call for call in session.calls if "/search/movie" in call[0]]
        detail_calls = [call for call in session.calls if "/movie/1317149" in call[0]]
        strategy = payload["search_strategy"]
        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 1317149)
        self.assertEqual(payload["identity"]["year"], 2025)
        self.assertEqual(payload["decision"]["eligibility_reason"], "corroborated_titles")
        self.assertEqual(len(search_calls), 1)
        self.assertEqual(len(detail_calls), 1)
        self.assertTrue(strategy["early_detail_attempted"])
        self.assertTrue(strategy["early_detail_reused"])
        self.assertEqual(strategy["detail_requests"], 1)
        self.assertEqual(strategy["early_stop_reason"], "all_atomic_titles_confirmed")
        self.assertEqual(payload["candidates"][0]["title_match_level"], "corroborated")

    def test_el_oso_progressive_detail_accepts_raw_alternate_below_threshold(self):
        guessed = {
            "title": "El oso",
            "year": 2022,
            "season": 1,
            "_title_evidence": [
                {
                    "value": "El oso",
                    "role": "primary",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
                {
                    "value": "The Bear",
                    "role": "alternate",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
            ],
        }
        raw_candidate = ResolverCandidate(
            136315,
            "tv",
            "The Bear",
            "The Bear",
            2022,
            ["The Bear"],
            season_count=3,
        )
        raw_score, _ = score_candidate(raw_candidate, guessed, [], False)
        self.assertLess(raw_score, 75)

        raw = tv_payload(136315, "The Bear", "The Bear", 2022, seasons=3)
        detailed = tv_payload(136315, "El oso", "The Bear", 2022, seasons=3)

        def search(params):
            return {
                "results": [raw]
                if params.get("query") == "El oso"
                else []
            }

        resolver, session = self.resolver(
            {
                "/search/tv": search,
                "/tv/136315": detailed,
            }
        )
        payload = resolver.preview(
            "El oso (The Bear) (2022) S01E01.mkv",
            "tv",
        )

        strategy = payload["search_strategy"]
        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 136315)
        self.assertEqual(payload["decision"]["eligibility_reason"], "corroborated_titles")
        self.assertEqual(
            sum("/search/tv" in call[0] for call in session.calls),
            1,
        )
        self.assertEqual(
            sum("/tv/136315" in call[0] for call in session.calls),
            1,
        )
        self.assertTrue(strategy["early_detail_attempted"])
        self.assertTrue(strategy["early_detail_reused"])
        self.assertEqual(strategy["detail_requests"], 1)
        self.assertEqual(strategy["early_stop_reason"], "all_atomic_titles_confirmed")

    def test_progressive_detail_failure_is_not_retried_and_forces_review(self):
        raw = movie_payload(1317149, "I Swear", "I Swear", 2025)

        def search(params):
            return {
                "results": [raw]
                if params.get("query") == "Incontrolable"
                else []
            }

        resolver, session = self.resolver(
            {
                "/search/movie": search,
                "/movie/1317149": FakeResponse(
                    {"status_message": "temporary failure"},
                    status_code=500,
                ),
            }
        )

        payload = resolver.preview(
            "Incontrolable (I Swear) (2025).mkv",
            "movies",
        )

        strategy = payload["search_strategy"]
        self.assertEqual(payload["status"], "REJECTED")
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "title_selection_uncertain",
        )
        self.assertTrue(payload["selection_uncertain"])
        self.assertTrue(strategy["early_detail_attempted"])
        self.assertFalse(strategy["early_detail_reused"])
        self.assertTrue(strategy["detail_incomplete"])
        self.assertEqual(strategy["detail_requests"], 1)
        self.assertEqual(
            sum("/movie/1317149" in call[0] for call in session.calls),
            1,
        )

    def test_preview_incontrolable_real_case_requires_both_title_halves(self):
        target = movie_payload(1317149, "Incontrolable", "I Swear", 2025)
        juror = movie_payload(1044736, "El jurado", "Je le jure", 2025)
        unrelated = movie_payload(1409966, "7天", "7天", 2025)

        def search(params):
            if params.get("query") == "I Swear":
                return {"results": [juror, unrelated, target]}
            return {"results": []}

        resolver, session = self.resolver(
            {
                "/search/movie": search,
                "/movie/1317149": target,
                "/movie/1044736": juror,
                "/movie/1409966": unrelated,
            }
        )

        payload = resolver.preview(
            "Incontrolable (I Swear) (2025).mkv",
            "movies",
        )

        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 1317149)
        self.assertEqual(
            payload["identity"]["resolver_algorithm_version"],
            RESOLVER_ALGORITHM_VERSION,
        )
        self.assertEqual(
            payload["decision"]["resolver_algorithm_version"],
            RESOLVER_ALGORITHM_VERSION,
        )
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "corroborated_titles",
        )
        self.assertEqual(payload["decision"]["eligible_candidate_count"], 1)
        self.assertFalse(payload["decision"]["eligibility_blocked"])
        self.assertFalse(
            payload["decision"]["strict_alternate_fallback"]["applied"]
        )
        candidates = {item["tmdb_id"]: item for item in payload["candidates"]}
        self.assertTrue(candidates[1317149]["eligible"])
        self.assertFalse(candidates[1044736]["eligible"])
        self.assertFalse(candidates[1409966]["eligible"])
        self.assertEqual(candidates[1317149]["title_match_level"], "corroborated")
        self.assertTrue(
            all(
                "identity_exact" in match
                for candidate in candidates.values()
                for match in candidate["title_matches"]
            )
        )
        self.assertIn(
            "alternate",
            candidates[1317149]["search_provenance"]["sources"],
        )
        self.assertNotIn("query", candidates[1317149]["search_provenance"])
        self.assertEqual(payload["search_strategy"]["mode"], "phased_round_robin")
        self.assertEqual(
            payload["search_strategy"]["early_stop_reason"],
            "all_atomic_titles_confirmed",
        )
        self.assertFalse(payload["selection_uncertain"])
        self.assertTrue(
            any(
                item["tmdb_id"] == 1317149 and item["alternate_only"]
                for item in payload["candidate_provenance"]
            )
        )
        provenance_json = json.dumps(
            payload["candidate_provenance"],
            ensure_ascii=False,
        )
        self.assertNotIn("Incontrolable", provenance_json)
        self.assertNotIn("I Swear", provenance_json)
        queries = [
            call[1].get("query")
            for call in session.calls
            if "/search/movie" in call[0]
        ]
        self.assertIn("Incontrolable", queries)
        self.assertIn("I Swear", queries)
        self.assertLessEqual(len(queries), 4)

    def test_composite_tmdb_title_corroborates_and_stops_on_primary_search(self):
        target = movie_payload(
            1317149,
            "Incontrolable (I Swear)",
            "I Swear",
            2025,
        )

        def search(params):
            return {
                "results": [target]
                if params.get("query") == "Incontrolable"
                else []
            }

        resolver, session = self.resolver(
            {
                "/search/movie": search,
                "/movie/1317149": target,
            }
        )

        payload = resolver.preview(
            "Incontrolable (I Swear) (2025).mkv",
            "movies",
        )

        search_calls = [
            call for call in session.calls if "/search/movie" in call[0]
        ]
        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 1317149)
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "corroborated_titles",
        )
        self.assertEqual(payload["candidates"][0]["title_match_level"], "corroborated")
        self.assertEqual(len(search_calls), 1)
        self.assertLessEqual(len(search_calls), 4)
        self.assertEqual(
            payload["search_strategy"]["early_stop_reason"],
            "all_atomic_titles_confirmed",
        )
        self.assertFalse(payload["search_strategy"]["early_detail_attempted"])
        self.assertFalse(payload["search_strategy"]["early_detail_reused"])
        self.assertEqual(payload["search_strategy"]["detail_requests"], 1)
        self.assertEqual(
            sum("/movie/1317149" in call[0] for call in session.calls),
            1,
        )

    def test_corroborated_detail_wins_over_omitted_alternate_uncertainty(self):
        target = movie_payload(1317149, "Incontrolable", "I Swear", 2025)
        primary_one = movie_payload(
            1044736,
            "Incontrolable",
            "Primary decoy one",
            2025,
        )
        primary_two = movie_payload(
            1044737,
            "Incontrolable",
            "Primary decoy two",
            2025,
        )
        omitted_alternate = movie_payload(
            1409966,
            "I Swear",
            "I Swear",
            2025,
        )

        def search(params):
            if params.get("query") == "Incontrolable":
                return {"results": [primary_one, primary_two, target]}
            if params.get("query") == "I Swear":
                return {"results": [omitted_alternate]}
            return {"results": []}

        resolver, session = self.resolver(
            {
                "/search/movie": search,
                "/movie/1044736": primary_one,
                "/movie/1044737": primary_two,
                "/movie/1317149": target,
            }
        )
        rules = factory_identity_rules()
        rules["resolver"]["acceptance"]["early_stop_score"] = 999
        rules["resolver"]["acceptance"]["early_stop_margin"] = 999

        payload = resolver.preview(
            "Incontrolable (I Swear) (2025).mkv",
            "movies",
            rules,
        )

        detailed_ids = [item["tmdb_id"] for item in payload["candidates"]]
        provenance = {
            item["tmdb_id"]: item for item in payload["candidate_provenance"]
        }
        detail_paths = [
            call[0].split("/3", 1)[1]
            for call in session.calls
            if "/movie/" in call[0]
        ]
        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 1317149)
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "corroborated_titles",
        )
        self.assertEqual(payload["decision"]["eligible_candidate_count"], 1)
        self.assertFalse(payload["decision"]["eligibility_blocked"])
        self.assertTrue(payload["selection_uncertain"])
        self.assertTrue(payload["selection_uncertainty_alternate_only"])
        self.assertEqual(len(detailed_ids), 3)
        self.assertIn(1317149, detailed_ids)
        self.assertEqual(provenance[1409966]["exact_sources"], ["alternate"])
        self.assertTrue(provenance[1409966]["alternate_only"])
        self.assertFalse(provenance[1409966]["selected_for_detail"])
        self.assertEqual(payload["raw_exact_candidate_counts"]["total"], 4)
        self.assertNotIn("/movie/1409966", detail_paths)

    def test_preview_movie_strict_alternate_needs_unique_exact_year(self):
        target = movie_payload(200, "Remote Truth", "Remote Truth", 2024)
        rival = movie_payload(201, "Titulo local", "Titulo local", 2023)

        def search(params):
            if params.get("query") == "Titulo local":
                return {"results": [rival]}
            if params.get("query") == "Remote Truth":
                return {"results": [target]}
            return {"results": []}

        resolver, _ = self.resolver(
            {
                "/search/movie": search,
                "/movie/200": target,
                "/movie/201": rival,
            }
        )

        payload = resolver.preview(
            "Titulo local (Remote Truth) (2024).mkv",
            "movies",
        )

        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 200)
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "strict_alternate_fallback",
        )
        self.assertTrue(
            payload["decision"]["strict_alternate_fallback"]["applied"]
        )
        self.assertTrue(payload["candidates"][0]["eligible"])
        self.assertEqual(payload["candidates"][0]["title_match_level"], "alternate")

    def test_preview_omitted_part_number_cannot_drive_strict_alternate_fallback(self):
        target = movie_payload(
            300,
            "Long Saga Chapter IV final",
            "Long Saga Chapter IV final",
            2024,
        )

        def search(params):
            if params.get("query") == "Long Saga Chapter final":
                return {"results": [target]}
            return {"results": []}

        resolver, _ = self.resolver(
            {
                "/search/movie": search,
                "/movie/300": target,
            }
        )

        guessed = {
            "title": "Unrelated local title",
            "year": 2024,
            "_title_candidates": [
                "Unrelated local title",
                "Long Saga Chapter final",
            ],
            "_title_evidence": [
                {
                    "value": "Unrelated local title",
                    "role": "primary",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
                {
                    "value": "Long Saga Chapter final",
                    "role": "alternate",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
            ],
        }
        with patch(
            "arr_orchestrator.identity.resolver.service.best_guess",
            return_value=guessed,
        ):
            payload = resolver.preview(
                "Unrelated local title (Long Saga Chapter final) (2024).mkv",
                "movies",
            )

        alternate_match = next(
            item
            for item in payload["candidates"][0]["title_matches"]
            if item["role"] == "alternate"
        )
        self.assertEqual(payload["status"], "REJECTED")
        self.assertEqual(
            payload["decision"]["status"],
            "REJECTED",
        )
        self.assertTrue(payload["decision"]["eligibility_blocked"])
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "title_evidence_unconfirmed",
        )
        self.assertFalse(
            payload["decision"]["strict_alternate_fallback"]["applied"]
        )
        self.assertTrue(alternate_match["exact"])
        self.assertFalse(alternate_match["identity_exact"])
        self.assertIn(
            {
                "path": "resolver.title_matching.allow_omitted_part_number",
                "detail": "Número de saga omitido",
            },
            payload["candidates"][0]["matching_rules"],
        )

    def test_preview_legacy_editorial_descriptor_is_rejected_by_eligibility(self):
        descriptor = movie_payload(
            301,
            "Extended Edition",
            "Extended Edition",
            1982,
        )
        guessed = {
            "title": "Blade Runner",
            "_display_title": "Extended Edition",
            "year": 1982,
            "_title_candidates": ["Blade Runner", "Extended Edition"],
        }
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [descriptor]},
                "/movie/301": descriptor,
            }
        )

        with patch(
            "arr_orchestrator.identity.resolver.service.best_guess",
            return_value=guessed,
        ):
            payload = resolver.preview(
                "Blade Runner Extended Edition (1982).mkv",
                "movies",
            )

        self.assertEqual(payload["status"], "REJECTED")
        self.assertEqual(
            payload["decision"]["status"],
            "REJECTED",
        )
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "title_evidence_unconfirmed",
        )
        self.assertTrue(payload["decision"]["eligibility_blocked"])
        self.assertFalse(payload["candidates"][0]["eligible"])
        self.assertEqual(payload["candidates"][0]["title_match_level"], "none")

    def test_preview_la_oficina_needs_2005_and_rejects_impossible_season(self):
        target = tv_payload(2316, "The Office", "The Office", 2005, seasons=9)
        rival = tv_payload(999, "La oficina", "La oficina", 1995, seasons=6)

        def search(params):
            if params.get("query") == "La oficina":
                return {"results": [rival]}
            if params.get("query") == "The Office":
                return {"results": [target]}
            return {"results": []}

        routes = {
            "/search/tv": search,
            "/tv/2316": target,
            "/tv/999": rival,
        }
        resolver, session = self.resolver(routes)

        without_year = resolver.preview(
            "La oficina (The Office) S02E01.mkv",
            "tv",
        )

        self.assertTrue(without_year["decision"]["eligibility_blocked"])
        self.assertEqual(
            without_year["decision"]["eligibility_reason"],
            "title_evidence_conflict",
        )
        self.assertFalse(
            without_year["decision"]["strict_alternate_fallback"]["applied"]
        )
        searched_without_year = [
            call[1].get("query")
            for call in session.calls
            if "/search/tv" in call[0]
        ]
        self.assertIn("La oficina", searched_without_year)
        self.assertIn("The Office", searched_without_year)

        resolver_2005, _ = self.resolver(routes)
        with_year = resolver_2005.preview(
            "La oficina (The Office) (2005) S02E01.mkv",
            "tv",
        )

        self.assertEqual(with_year["status"], "ACCEPTED")
        self.assertEqual(with_year["identity"]["tmdb_id"], 2316)
        self.assertEqual(
            with_year["decision"]["eligibility_reason"],
            "strict_alternate_fallback",
        )
        self.assertTrue(
            with_year["decision"]["strict_alternate_fallback"]["applied"]
        )

        resolver_bad_season, _ = self.resolver(routes)
        impossible = resolver_bad_season.preview(
            "La oficina (The Office) (2005) S10E01.mkv",
            "tv",
        )

        self.assertTrue(impossible["decision"]["eligibility_blocked"])
        self.assertEqual(
            impossible["decision"]["eligibility_reason"],
            "season_impossible",
        )

    def test_tv_multititle_waits_for_season_details_before_strict_fallback(self):
        primary_search = tv_payload(
            1,
            "La oficina",
            "The Office",
            2005,
            seasons=1,
        )
        primary_search.pop("number_of_seasons")
        primary_detail = tv_payload(
            1,
            "La oficina",
            "The Office",
            2005,
            seasons=1,
        )
        alternate = tv_payload(
            2,
            "The Office",
            "The Office",
            2005,
            seasons=9,
        )

        def search(params):
            if params.get("query") == "La oficina":
                return {"results": [primary_search]}
            if params.get("query") == "The Office":
                return {"results": [alternate]}
            return {"results": []}

        resolver, session = self.resolver(
            {
                "/search/tv": search,
                "/tv/1": primary_detail,
                "/tv/2": alternate,
            }
        )

        payload = resolver.preview(
            "La oficina (The Office) (2005) S02E01.mkv",
            "tv",
        )

        queries = [
            call[1].get("query")
            for call in session.calls
            if "/search/tv" in call[0]
        ]
        candidates = {item["tmdb_id"]: item for item in payload["candidates"]}
        self.assertIn("La oficina", queries)
        self.assertIn("The Office", queries)
        self.assertLess(queries.index("La oficina"), queries.index("The Office"))
        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertEqual(payload["identity"]["tmdb_id"], 2)
        self.assertEqual(payload["identity"]["season"], 2)
        self.assertEqual(
            payload["decision"]["eligibility_reason"],
            "strict_alternate_fallback",
        )
        self.assertTrue(
            payload["decision"]["strict_alternate_fallback"]["applied"]
        )
        self.assertEqual(candidates[1]["season_count"], 1)
        self.assertFalse(candidates[1]["eligible"])
        self.assertEqual(candidates[2]["season_count"], 9)
        self.assertTrue(candidates[2]["eligible"])
        self.assertIsNone(payload["search_strategy"]["early_stop_reason"])
        self.assertTrue(payload["search_strategy"]["early_detail_attempted"])
        self.assertTrue(payload["search_strategy"]["early_detail_reused"])
        self.assertEqual(payload["search_strategy"]["detail_requests"], 2)
        detail_paths = [
            call[0].split("/3", 1)[1]
            for call in session.calls
            if "/tv/" in call[0]
        ]
        self.assertEqual(detail_paths.count("/tv/1"), 1)
        self.assertEqual(detail_paths.count("/tv/2"), 1)

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

    def test_tv_search_adds_generic_series_title_before_episode_marker(self):
        correct = tv_payload(
            501,
            "Universal Story",
            "Universal Story",
            2024,
            seasons=2,
        )

        def search(params):
            if params.get("query") == "Universal Story":
                return {"results": [correct]}
            return {"results": []}

        routes = {
            "/search/tv": search,
            "/tv/501": correct,
        }
        samples = (
            "Universal.Story.S01E03.The.Return.1080p",
            "Universal.Story.[WEB-DL.1080p].[Dual].1x03.The.Return",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                resolver, session = self.resolver(routes)
                input_root = self.input_file(f"{sample}.mkv")

                identity = resolver.resolve(
                    {"category": "tv", "name": sample},
                    input_root,
                )

                queries = [
                    call[1].get("query")
                    for call in session.calls
                    if "/search/tv" in call[0]
                ]
                self.assertEqual(identity.tmdb_id, 501)
                self.assertIn("Universal Story", queries)
                self.assertEqual(identity.season, 1)
                self.assertEqual(identity.episodes, [3])

    def test_series_title_candidate_rule_can_be_disabled(self):
        candidates = series_title_candidates(
            ["Universal.Story.S01E03.The.Return.1080p"],
            {
                "series_candidates": {
                    "title_before_episode_marker": False,
                    "min_title_words": 2,
                }
            },
        )

        self.assertEqual(candidates, [])

    def test_series_title_candidates_respect_minimum_words(self):
        evidence = [
            "Universal.Story.S01E03.The.Return.1080p",
            "The.Universal.Story.S01E03.The.Return.1080p",
        ]

        candidates = series_title_candidates(
            evidence,
            {
                "series_candidates": {
                    "title_before_episode_marker": True,
                    "min_title_words": 3,
                }
            },
        )

        self.assertNotIn("Universal Story", candidates)
        self.assertIn("The Universal Story", candidates)

    def test_parser_titles_stay_before_deduplicated_derived_candidates(self):
        candidates = ordered_title_candidates(
            [
                "Historia Lokalna",
                "Universal Story",
                "Historia Lokalna / Universal Story",
            ],
            "Historia Lokalna",
            ["Historia Lokalna / Universal Story", "Historia Lokalna 2024"],
        )

        self.assertEqual(
            candidates,
            [
                "Historia Lokalna",
                "Universal Story",
                "Historia Lokalna / Universal Story",
                "Historia Lokalna 2024",
            ],
        )

    def test_tv_search_keeps_bilingual_parser_titles_before_derived_prefixes(self):
        correct = tv_payload(
            502,
            "Universal Story",
            "Universal Story",
            2024,
            seasons=2,
        )

        def search(params):
            if params.get("query") == "Universal Story":
                return {"results": [correct]}
            return {"results": []}

        routes = {"/search/tv": search, "/tv/502": correct}
        resolver, session = self.resolver(routes)
        sample = "Historia Lokalna / Universal Story (2024) S01E03 WEB-DL"
        input_root = self.input_file("bilingual-series-sample.mkv")

        identity = resolver.resolve(
            {"category": "tv", "name": sample},
            input_root,
        )

        queries = [
            call[1].get("query")
            for call in session.calls
            if "/search/tv" in call[0]
        ]
        self.assertEqual(identity.tmdb_id, 502)
        self.assertIn("Universal Story", queries)
        self.assertLessEqual(queries.index("Universal Story"), 4)

    def test_tv_search_with_year_control_sends_first_air_date_year(self):
        correct = tv_payload(11, "Serie ejemplo", "Example show", 2024, seasons=2)
        routes = {"/search/tv": {"results": [correct]}, "/tv/11": correct}
        resolver, session = self.resolver(routes)
        resolver.configure_rules(
            {
                "resolver": {
                    "query_variants": {"with_year": True, "without_year": False}
                }
            }
        )
        input_root = self.input_file("Serie.ejemplo.2024.S01E01.mkv")

        identity = resolver.resolve(
            {"category": "tv", "name": "Serie.ejemplo.2024.S01E01"}, input_root
        )

        self.assertEqual(identity.tmdb_id, 11)
        search_call = next(call for call in session.calls if "/search/tv" in call[0])
        self.assertEqual(search_call[1].get("first_air_date_year"), 2024)

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
