import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arr_orchestrator.db import Database
from arr_orchestrator.filebot import FileBotRunner, TV_FORMAT
from arr_orchestrator.identity import factory_identity_rules
from arr_orchestrator.identity.resolver.service import RESOLVER_ALGORITHM_VERSION
from arr_orchestrator.name_resolver import (
    NameResolver,
    ResolvedIdentity,
    ResolverAmbiguous,
    ResolverUnavailable,
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


def movie_payload(
    tmdb_id,
    title,
    year,
    *,
    original_title=None,
    popularity=1.0,
    vote_count=1,
    runtime=100,
    original_language="es",
):
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": original_title or title,
        "original_language": original_language,
        "release_date": f"{year}-01-01" if year else "",
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
        "release_dates": {"results": []},
        "popularity": popularity,
        "vote_count": vote_count,
        "runtime": runtime,
    }


def tv_payload(
    tmdb_id,
    title,
    year,
    *,
    seasons=3,
    original_title=None,
    popularity=1.0,
    vote_count=1,
):
    return {
        "id": tmdb_id,
        "name": title,
        "original_name": original_title or title,
        "original_language": "es",
        "first_air_date": f"{year}-01-01" if year else "",
        "number_of_seasons": seasons,
        "seasons": [
            {"season_number": number, "episode_count": 10}
            for number in range(1, seasons + 1)
        ],
        "episode_run_time": [45],
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "popularity": popularity,
        "vote_count": vote_count,
    }


def failed_probe(*_args, **_kwargs):
    return SimpleNamespace(returncode=1, stdout="", stderr="sin duracion")


class NameResolverV2Tests(unittest.TestCase):
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
            20_000,
            self.database,
            session=session,
            probe_runner=failed_probe,
        )
        return resolver, session

    def input_root(self, *names):
        root = self.root / "filebot_input"
        root.mkdir(parents=True, exist_ok=True)
        for name in names:
            (root / name).write_bytes(b"media")
        return root

    def test_preview_503_is_retryable_and_never_leaks_provider_payload(self):
        resolver, _ = self.resolver(
            {"/search/movie": FakeResponse({"token": "must-not-leak"}, 503)}
        )

        payload = resolver.preview("Pelicula de prueba 2024", "movies")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "RETRY_PROVIDER")
        self.assertEqual(payload["decision"]["status"], "RETRY_PROVIDER")
        self.assertFalse(payload["decision"]["accepted"])
        self.assertFalse(payload["decision"]["has_scoring"])
        self.assertNotIn("must-not-leak", json.dumps(payload, ensure_ascii=False))

    def test_preview_401_is_a_hard_provider_configuration_error(self):
        resolver, _ = self.resolver(
            {"/search/movie": FakeResponse({"token": "must-not-leak"}, 401)}
        )

        payload = resolver.preview("Pelicula de prueba 2024", "movies")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "RETRY_PROVIDER")
        self.assertEqual(payload["details"]["reason_code"], "provider_unavailable")
        self.assertFalse(payload["decision"]["accepted"])
        self.assertNotIn("must-not-leak", json.dumps(payload, ensure_ascii=False))

    def test_trace_snapshot_is_sanitized_and_independent(self):
        resolver, _ = self.resolver({})
        resolver._trace = {
            "resolver_algorithm_version": RESOLVER_ALGORITHM_VERSION,
            "decision": {"status": "ACCEPTED_CONFIDENT"},
            "token": "must-not-leak",
        }

        snapshot = resolver.trace_snapshot()

        self.assertEqual(
            snapshot["resolver_algorithm_version"], RESOLVER_ALGORITHM_VERSION
        )
        self.assertEqual(snapshot["token"], "<REDACTED>")
        snapshot["decision"]["status"] = "MUTATED"
        self.assertEqual(
            resolver._trace["decision"]["status"], "ACCEPTED_CONFIDENT"
        )

    def test_ambiguous_preview_uses_deterministic_fallback_without_scoring(self):
        first = movie_payload(20, "El desconocido", 2000, popularity=9, vote_count=2)
        second = movie_payload(10, "El desconocido", 2000, popularity=8, vote_count=500)
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [second, first]},
                "/movie/10": second,
                "/movie/20": first,
            }
        )
        private_path = str(self.root / "privado" / "no-debe-aparecer.mkv")

        with patch(
            "arr_orchestrator.identity.resolver.service.collect_evidence",
            return_value=[private_path],
        ) as filesystem_evidence:
            payload = resolver.preview("El desconocido.2000.mkv", "movies")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ACCEPTED_FALLBACK")
        self.assertEqual(payload["identity"]["tmdb_id"], 20)
        self.assertEqual(payload["decision"]["fallback_reason"], "ambiguity_adjudicated")
        self.assertEqual(payload["decision"]["selected"]["tmdb_id"], 20)
        self.assertEqual(payload["candidates"], payload["decision"]["alternatives"])
        self.assertNotIn("score", payload["identity"])
        self.assertNotIn("margin", payload["identity"])
        self.assertNotIn("breakdown", payload["candidates"][0])
        filesystem_evidence.assert_not_called()
        self.assertNotIn(private_path, json.dumps(payload, ensure_ascii=False))

        for item in payload["decision"]["evidence"]:
            families = [entry["family"] for entry in item["families"]]
            self.assertEqual(len(families), len(set(families)))
            self.assertLessEqual(
                {entry["state"] for entry in item["families"]},
                {"AGREE", "DISAGREE", "UNKNOWN"},
            )

    def test_explicit_year_eliminates_conflict_and_accepts_one_candidate(self):
        correct = movie_payload(2, "Objetivo", 2024, popularity=1)
        popular_wrong = movie_payload(1, "Objetivo", 1999, popularity=1000)
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [popular_wrong, correct]},
                "/movie/1": popular_wrong,
                "/movie/2": correct,
            }
        )

        payload = resolver.preview("Objetivo 2024", "movies")

        self.assertEqual(payload["status"], "ACCEPTED_CONFIDENT")
        self.assertEqual(payload["identity"]["tmdb_id"], 2)
        alternatives = {item["tmdb_id"]: item for item in payload["candidates"]}
        self.assertTrue(alternatives[1]["eliminated"])
        self.assertIn("year_conflict", alternatives[1]["elimination_reasons"])
        self.assertEqual(
            payload["decision"]["phase_counts"],
            {"discovered": 2, "enriched": 2, "eliminated": 1, "plausible": 1},
        )

    def test_title_conflict_blocks_instead_of_using_a_low_score_threshold(self):
        unrelated = movie_payload(99, "Titulo ajeno", 2024)
        resolver, _ = self.resolver(
            {"/search/movie": {"results": [unrelated]}, "/movie/99": unrelated}
        )

        payload = resolver.preview("Objetivo 2024", "movies")

        self.assertEqual(payload["status"], "BLOCKED_HARD")
        self.assertFalse(payload["decision"]["accepted"])
        self.assertEqual(payload["decision"]["phase_counts"]["plausible"], 0)
        self.assertIn("title_conflict", payload["candidates"][0]["elimination_reasons"])

    def test_ambiguity_always_uses_fallback_without_a_disable_switch(self):
        first = movie_payload(1, "Objetivo", 2024)
        second = movie_payload(2, "Objetivo", 2024)
        rules = factory_identity_rules()
        resolver, _ = self.resolver(
            {
                "/search/movie": {"results": [first, second]},
                "/movie/1": first,
                "/movie/2": second,
            }
        )

        payload = resolver.preview("Objetivo 2024", "movies", rules)

        self.assertEqual(payload["status"], "ACCEPTED_FALLBACK")
        self.assertEqual(
            payload["decision"]["fallback_reason"], "ambiguity_adjudicated"
        )
        self.assertIsNotNone(payload["decision"]["selected"])

    def test_release_timeline_is_part_of_the_single_year_evidence_family(self):
        candidate = movie_payload(1, "Objetivo", 2023)
        candidate["release_dates"] = {
            "results": [
                {
                    "iso_3166_1": "ES",
                    "release_dates": [
                        {"release_date": "2024-01-10T00:00:00.000Z", "type": 3}
                    ],
                }
            ]
        }
        resolver, _ = self.resolver(
            {"/search/movie": {"results": [candidate]}, "/movie/1": candidate}
        )

        payload = resolver.preview("Objetivo 2024", "movies")

        self.assertEqual(payload["status"], "ACCEPTED_CONFIDENT")
        families = payload["decision"]["evidence"][0]["families"]
        year_evidence = [item for item in families if item["family"] == "year"]
        self.assertEqual(len(year_evidence), 1)
        self.assertEqual(year_evidence[0]["state"], "AGREE")
        self.assertEqual(year_evidence[0]["value"]["candidate"], [2023, 2024])

    def test_coverage_cap_accepts_best_plausible_candidate_as_fallback(self):
        candidate = movie_payload(1, "Objetivo", 2024)
        rules = factory_identity_rules()
        rules["resolver"]["coverage"]["max_searches"] = 1
        resolver, session = self.resolver(
            {"/search/movie": {"results": [candidate]}, "/movie/1": candidate}
        )

        payload = resolver.preview("Objetivo 2024", "movies", rules)

        self.assertEqual(payload["status"], "ACCEPTED_FALLBACK")
        self.assertTrue(payload["decision"]["coverage_limited"])
        self.assertEqual(payload["decision"]["fallback_reason"], "coverage_limited")
        self.assertEqual(payload["identity"]["tmdb_id"], 1)
        search_calls = [call for call in session.calls if "/search/movie" in call[0]]
        self.assertEqual(len(search_calls), 1)

    def test_partial_provider_failure_stays_pending_and_is_not_cached(self):
        candidate = movie_payload(1, "Objetivo", 2024)
        attempts = 0

        def search(_params):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return FakeResponse({}, 503)
            return {"results": [candidate]}

        resolver, _ = self.resolver(
            {"/search/movie": search, "/movie/1": candidate}
        )

        payload = resolver.preview("Objetivo 2024", "movies")

        self.assertEqual(payload["status"], "RETRY_PROVIDER")
        self.assertFalse(payload["decision"]["accepted"])
        self.assertTrue(payload["decision"]["coverage_limited"])
        self.assertGreaterEqual(payload["decision"]["provider_failures"], 1)
        self.assertNotIn("identity", payload)

        attempts = 0
        with self.assertRaises(ResolverUnavailable):
            resolver.resolve(
                {"name": "Objetivo 2024", "category": "movies"},
                self.input_root("Objetivo.2024.mkv"),
            )
        self.assertEqual(self.database.resolver_cache_stats()["total"], 0)

    def test_embedded_tmdb_id_uses_details_without_search(self):
        candidate = movie_payload(77, "Objetivo", 2024)
        resolver, session = self.resolver({"/movie/77": candidate})

        payload = resolver.preview("Objetivo 2024 tmdb-77", "movies")

        self.assertEqual(payload["status"], "ACCEPTED_CONFIDENT")
        self.assertEqual(payload["identity"]["tmdb_id"], 77)
        self.assertEqual(payload["identity"]["source"], "tmdb_id")
        paths = [call[0].split("/3", 1)[1] for call in session.calls]
        self.assertEqual(paths, ["/movie/77"])

    def test_embedded_imdb_id_uses_find_then_details(self):
        candidate = movie_payload(88, "Objetivo", 2024)
        resolver, session = self.resolver(
            {
                "/find/tt1234567": {"movie_results": [candidate]},
                "/movie/88": candidate,
            }
        )

        payload = resolver.preview("Objetivo 2024 tt1234567", "movies")

        self.assertEqual(payload["status"], "ACCEPTED_CONFIDENT")
        self.assertEqual(payload["identity"]["tmdb_id"], 88)
        self.assertEqual(payload["identity"]["source"], "imdb_id")
        paths = [call[0].split("/3", 1)[1] for call in session.calls]
        self.assertEqual(paths, ["/find/tt1234567", "/movie/88"])

    def test_real_resolution_reuses_v5_cache_without_more_tmdb_calls(self):
        candidate = movie_payload(1, "Objetivo", 2024)
        resolver, session = self.resolver(
            {"/search/movie": {"results": [candidate]}, "/movie/1": candidate}
        )
        input_root = self.input_root("Objetivo.2024.mkv")
        job = {"category": "movies", "name": "Objetivo 2024"}

        first = resolver.resolve(job, input_root)
        calls_after_first = len(session.calls)
        second = resolver.resolve(job, input_root)

        self.assertEqual(first.tmdb_id, 1)
        self.assertEqual(second.tmdb_id, 1)
        self.assertEqual(second.source, "cache")
        self.assertEqual(len(session.calls), calls_after_first)
        self.assertTrue(resolver.trace_snapshot()["cache_hit"])
        self.assertNotIn("score", second.to_dict())
        self.assertNotIn("margin", second.to_dict())

    def test_cache_key_separates_same_name_when_local_runtime_changes(self):
        short = movie_payload(1, "Objetivo", 2024, runtime=20, popularity=5)
        feature = movie_payload(2, "Objetivo", 2024, runtime=100, popularity=10)
        resolver, session = self.resolver(
            {
                "/search/movie": {"results": [feature, short]},
                "/movie/1": short,
                "/movie/2": feature,
            }
        )
        short_root = self.root / "short"
        feature_root = self.root / "feature"
        for root in (short_root, feature_root):
            root.mkdir()
            media = root / "Objetivo.2024.mkv"
            media.write_bytes(b"same-size-placeholder")
            os.utime(media, ns=(1_700_000_000_000_000_000,) * 2)

        def runtime_for(root, *_args, **_kwargs):
            return [
                {
                    "source": "Objetivo.2024.mkv",
                    "runtime_minutes": 20 if Path(root).name == "short" else 100,
                }
            ]

        job = {"category": "movies", "name": "Objetivo 2024"}
        with patch(
            "arr_orchestrator.identity.resolver.service.probe_media_runtimes",
            side_effect=runtime_for,
        ):
            first = resolver.resolve(job, short_root)
            calls_after_first = len(session.calls)
            second = resolver.resolve(job, feature_root)

        self.assertEqual(first.tmdb_id, 1)
        self.assertEqual(second.tmdb_id, 2)
        self.assertGreater(len(session.calls), calls_after_first)
        self.assertFalse(resolver.trace_snapshot()["cache_hit"])
        self.assertEqual(self.database.resolver_cache_stats()["total"], 2)

    def test_cache_key_separates_same_name_when_local_manifest_changes(self):
        candidate = movie_payload(1, "Objetivo", 2024, runtime=100)
        resolver, session = self.resolver(
            {"/search/movie": {"results": [candidate]}, "/movie/1": candidate}
        )
        first_root = self.root / "first-content"
        second_root = self.root / "second-content"
        first_root.mkdir()
        second_root.mkdir()
        first_media = first_root / "Objetivo.2024.mkv"
        second_media = second_root / "Objetivo.2024.mkv"
        first_media.write_bytes(b"small")
        second_media.write_bytes(b"different-and-larger")
        for media in (first_media, second_media):
            os.utime(media, ns=(1_700_000_000_000_000_000,) * 2)
        runtime = [
            {"source": "Objetivo.2024.mkv", "runtime_minutes": 100}
        ]
        job = {"category": "movies", "name": "Objetivo 2024"}

        with patch(
            "arr_orchestrator.identity.resolver.service.probe_media_runtimes",
            return_value=runtime,
        ):
            resolver.resolve(job, first_root)
            calls_after_first = len(session.calls)
            resolver.resolve(job, second_root)

        self.assertGreater(len(session.calls), calls_after_first)
        self.assertFalse(resolver.trace_snapshot()["cache_hit"])
        self.assertEqual(self.database.resolver_cache_stats()["total"], 2)

    def test_cached_fallback_preserves_reason_alternatives_and_evidence(self):
        preferred = movie_payload(
            20, "El desconocido", 2000, popularity=9, vote_count=2
        )
        alternative = movie_payload(
            10, "El desconocido", 2000, popularity=8, vote_count=500
        )
        resolver, session = self.resolver(
            {
                "/search/movie": {"results": [alternative, preferred]},
                "/movie/10": alternative,
                "/movie/20": preferred,
            }
        )
        input_root = self.input_root("El.desconocido.2000.mkv")
        job = {"category": "movies", "name": "El desconocido 2000"}

        first = resolver.resolve(job, input_root)
        original = resolver.trace_snapshot()["decision"]
        calls_after_first = len(session.calls)
        second = resolver.resolve(job, input_root)
        cached = resolver.trace_snapshot()["decision"]

        self.assertEqual(first.decision_status, "ACCEPTED_FALLBACK")
        self.assertEqual(second.source, "cache")
        self.assertEqual(len(session.calls), calls_after_first)
        self.assertEqual(cached["fallback_reason"], "ambiguity_adjudicated")
        self.assertEqual(cached["alternatives"], original["alternatives"])
        self.assertEqual(cached["evidence"], original["evidence"])
        self.assertEqual(cached["phase_counts"], original["phase_counts"])
        self.assertEqual(
            [item["tmdb_id"] for item in cached["alternatives"]], [20, 10]
        )
        self.assertTrue(cached["cache_reused"])
        self.assertNotIn("score", json.dumps(cached, ensure_ascii=False))
        self.assertNotIn("margin", json.dumps(cached, ensure_ascii=False))

    def test_cache_fingerprint_changes_when_resolution_policy_changes(self):
        candidate = movie_payload(1, "Objetivo", 2024)
        resolver, session = self.resolver(
            {"/search/movie": {"results": [candidate]}, "/movie/1": candidate}
        )
        input_root = self.input_root("Objetivo.2024.mkv")
        job = {"category": "movies", "name": "Objetivo 2024"}
        first_rules = factory_identity_rules()
        second_rules = factory_identity_rules()
        second_rules["resolver"]["movies"]["runtime_tolerance_minutes"] = 12

        resolver.resolve(job, input_root, first_rules)
        calls_after_first = len(session.calls)
        resolver.resolve(job, input_root, second_rules)

        self.assertGreater(len(session.calls), calls_after_first)
        self.assertFalse(resolver.trace_snapshot()["cache_hit"])

    def test_tv_season_and_episode_are_validated_from_season_details(self):
        candidate = tv_payload(5, "La serie", 2024, seasons=2)
        season = {
            "episodes": [
                {"episode_number": number, "runtime": 45}
                for number in range(1, 11)
            ]
        }
        resolver, _ = self.resolver(
            {
                "/search/tv": {"results": [candidate]},
                "/tv/5": candidate,
                "/tv/5/season/2": season,
            }
        )

        payload = resolver.preview("La serie 2024 S02E03", "tv")

        self.assertEqual(payload["status"], "ACCEPTED_CONFIDENT")
        self.assertEqual(payload["identity"]["tmdb_id"], 5)
        self.assertEqual(payload["identity"]["season"], 2)
        self.assertEqual(payload["identity"]["episodes"], [3])
        families = payload["decision"]["evidence"][0]["families"]
        evidence = {item["family"]: item for item in families}
        episode_checks = {
            item["name"]: item["state"]
            for item in evidence["episode"]["value"]["subchecks"]
        }
        self.assertEqual(episode_checks["season"], "AGREE")
        self.assertEqual(episode_checks["numbered_episode"], "AGREE")

    def test_tv_impossible_episode_is_blocked_hard(self):
        candidate = tv_payload(5, "La serie", 2024, seasons=2)
        season = {
            "episodes": [
                {"episode_number": number, "runtime": 45}
                for number in range(1, 11)
            ]
        }
        resolver, _ = self.resolver(
            {
                "/search/tv": {"results": [candidate]},
                "/tv/5": candidate,
                "/tv/5/season/2": season,
            }
        )

        payload = resolver.preview("La serie 2024 S02E99", "tv")

        self.assertEqual(payload["status"], "BLOCKED_HARD")
        self.assertIn("episode_conflict", payload["candidates"][0]["elimination_reasons"])

    def test_real_tv_resolution_preserves_one_full_episode_intent_per_file(self):
        candidate = tv_payload(5, "La serie", 2024, seasons=1)
        season = {
            "episodes": [
                {"episode_number": number, "runtime": 45}
                for number in range(1, 11)
            ]
        }
        long_name = f"La.serie.{'x' * 170}.S01E01.mkv"
        second_name = "La.serie.copia.S01E01.mkv"
        input_root = self.input_root(long_name, second_name)
        resolver, _ = self.resolver(
            {
                "/search/tv": {"results": [candidate]},
                "/tv/5": candidate,
                "/tv/5/season/1": season,
            }
        )

        identity = resolver.resolve(
            {"category": "tv", "name": "La serie 2024 S01E01"}, input_root
        )

        self.assertEqual(identity.tmdb_id, 5)
        self.assertEqual(len(identity.episode_intents), 2)
        self.assertEqual(
            {item["source"] for item in identity.episode_intents},
            {long_name, second_name},
        )
        self.assertTrue(any(len(item["source"]) > 160 for item in identity.episode_intents))

    def test_manual_category_never_calls_tmdb(self):
        resolver, session = self.resolver({})
        input_root = self.input_root("Curso.manual.mkv")

        with self.assertRaises(ResolverAmbiguous) as captured:
            resolver.resolve(
                {"category": "manual", "name": "Curso manual"}, input_root
            )

        self.assertEqual(captured.exception.details["status"], "BLOCKED_HARD")
        self.assertEqual(session.calls, [])

    def test_no_candidates_stays_blocked_and_searches_are_capped_at_twelve(self):
        resolver, session = self.resolver({"/search/movie": {"results": []}})

        payload = resolver.preview(
            "Red One Codigo Traje Rojo 2024 version extendida", "movies"
        )

        self.assertEqual(payload["status"], "BLOCKED_HARD")
        search_calls = [call for call in session.calls if "/search/movie" in call[0]]
        self.assertLessEqual(len(search_calls), 12)
        self.assertLessEqual(payload["search_strategy"]["executed_searches"], 12)

    def test_output_validation_uses_title_alias_and_year_tolerance(self):
        identity = ResolvedIdentity(
            media_type="movie",
            tmdb_id=9279,
            title="Un padre en apuros",
            original_title="Jingle All the Way",
            year=1996,
            aliases=["Un padre en apuros", "Jingle All the Way"],
            score=0,
            margin=0,
            query="Un padre en apuros",
            guess={},
            source="search",
            resolver_algorithm_version=RESOLVER_ALGORITHM_VERSION,
        )
        resolver, _ = self.resolver({})

        self.assertTrue(resolver.output_matches(identity, ["Un padre en apuros (1996)"]))
        self.assertTrue(resolver.output_matches(identity, ["Jingle All the Way (1997)"]))
        self.assertFalse(resolver.output_matches(identity, ["Titulo distinto (1996)"]))
        self.assertFalse(resolver.output_matches(identity, ["Un padre en apuros (2000)"]))

    def test_legacy_identity_can_be_read_but_v2_serialization_omits_scores(self):
        legacy = ResolvedIdentity.from_legacy_dict(
            {
                "media_type": "movie",
                "tmdb_id": 1,
                "title": "Objetivo",
                "original_title": "Objetivo",
                "year": 2024,
                "aliases": ["Objetivo"],
                "score": 91,
                "margin": 7,
                "query": "Objetivo",
                "guess": {},
                "source": "cache",
            }
        )
        v2 = ResolvedIdentity.from_dict(
            {
                **legacy.to_dict(),
                "resolver_algorithm_version": RESOLVER_ALGORITHM_VERSION,
            }
        )

        self.assertEqual(legacy.score, 91)
        self.assertEqual(legacy.margin, 7)
        self.assertNotIn("score", v2.to_dict())
        self.assertNotIn("margin", v2.to_dict())

    def test_filebot_guided_command_uses_selected_tmdb_id_and_v2_locale(self):
        identity = ResolvedIdentity(
            media_type="tv",
            tmdb_id=77,
            title="La Agencia",
            original_title="The Agency",
            year=2024,
            aliases=["La Agencia", "The Agency"],
            score=0,
            margin=0,
            query="La Agencia",
            guess={"title": "La Agencia", "season": 1, "episode": 1},
            source="search",
            season=1,
            episodes=[1],
            resolver_algorithm_version=RESOLVER_ALGORITHM_VERSION,
            decision_status="ACCEPTED_CONFIDENT",
        )
        runner = FileBotRunner("filebot", self.root)
        runner.configure_identity_rules(
            {"resolver": {"locales": {"tv": {"language": "fr-FR"}}}}
        )

        preview = runner.preview_command(
            "job-guided",
            "tv",
            self.root / "input",
            self.root / "output",
            identity,
        )

        argv = preview["argv"]
        self.assertEqual(preview["mode"], "guided")
        self.assertEqual(argv[argv.index("--q") + 1], "77")
        self.assertEqual(argv[argv.index("--lang") + 1], "fr")
        self.assertEqual(argv[argv.index("--format") + 1], TV_FORMAT)
        self.assertEqual(preview["timeout_sec"], 14_400)


if __name__ == "__main__":
    unittest.main()
