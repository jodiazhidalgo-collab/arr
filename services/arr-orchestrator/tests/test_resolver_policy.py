import unittest

from arr_orchestrator.identity.resolver.candidate_search import search_candidates
from arr_orchestrator.identity.resolver.models import ResolverCandidate
from arr_orchestrator.identity.resolver.policy import effective_policy
from arr_orchestrator.identity.resolver.rules import matching_forced_rule
from arr_orchestrator.identity.resolver.scoring import score_candidate


class ResolverPolicyTests(unittest.TestCase):
    @staticmethod
    def _movie_payload(tmdb_id, title, year):
        return {
            "id": tmdb_id,
            "title": title,
            "original_title": title,
            "release_date": f"{year}-01-01" if year else "",
        }

    def test_factory_policy_preserves_current_resolver_values(self):
        policy = effective_policy({}, "movies")

        self.assertEqual(policy["language"], "es-ES")
        self.assertEqual(policy["region"], "ES")
        self.assertEqual(policy["search_limits"]["max_searches"], 8)
        self.assertEqual(policy["acceptance"]["min_score"], 75)
        self.assertEqual(policy["acceptance"]["min_margin"], 12)
        self.assertEqual(policy["cache"]["ttl_seconds"], 30 * 24 * 3600)

    def test_new_identity_document_controls_every_resolver_block(self):
        policy = effective_policy(
            {
                "parser": {"site_words": ["ejemplo"]},
                "resolver": {
                    "locales": {
                        "movies": {"language": "fr-FR", "region": "FR"},
                        "fallback_language": "de-DE",
                        "use_fallback_language": False,
                    },
                    "aliases": {"movies": ["origen | destino"], "tv": []},
                    "forced_matches": {"movies": ["titulo | 2024 | 10"], "tv": []},
                    "search_limits": {"max_searches": 4},
                    "acceptance": {"min_score": 60, "min_margin": 7},
                    "cache": {"enabled": False, "ttl_seconds": 90},
                },
            },
            "movies",
        )

        self.assertEqual(policy["language"], "fr-FR")
        self.assertEqual(policy["region"], "FR")
        self.assertEqual(policy["fallback_language"], "de-DE")
        self.assertFalse(policy["use_fallback_language"])
        self.assertEqual(policy["query_aliases"], ["origen | destino"])
        self.assertEqual(policy["forced_matches"], ["titulo | 2024 | 10"])
        self.assertEqual(policy["search_limits"]["max_searches"], 4)
        self.assertEqual(policy["search_limits"]["detail_candidates"], 3)
        self.assertEqual(policy["acceptance"]["min_score"], 60)
        self.assertEqual(policy["acceptance"]["min_margin"], 7)
        self.assertFalse(policy["cache"]["enabled"])
        self.assertEqual(policy["parser"]["site_words"], ["ejemplo"])

    def test_scoring_uses_editable_weights_and_keeps_breakdown(self):
        candidate = ResolverCandidate(
            tmdb_id=1,
            media_type="movie",
            title="Mi pelicula",
            original_title="Mi pelicula",
            year=2024,
            aliases=["Mi pelicula"],
        )
        guessed = {"title": "Mi pelicula", "year": 2024, "_title_candidates": []}

        factory_score, factory_reasons = score_candidate(candidate, guessed, [], False)
        custom_score, custom_reasons = score_candidate(
            candidate,
            guessed,
            [],
            False,
            {
                "title_exact": 3,
                "title_similarity_max": 2,
                "token_overlap_max": 1,
                "year_exact": 4,
                "category": 5,
            },
        )

        self.assertEqual(factory_score, 90.0)
        self.assertEqual(custom_score, 15.0)
        self.assertIn("titulo exacto", factory_reasons)
        self.assertIn("ano exacto", custom_reasons)

    def test_forced_match_prefers_exact_year_in_any_rule_order(self):
        generic = ("Cenicienta", None, 11224)
        specific_2015 = ("Cenicienta", 2015, 150689)
        specific_2024 = ("Cenicienta", 2024, 999999)
        orders = (
            [generic, specific_2015, specific_2024],
            [specific_2024, specific_2015, generic],
        )

        for rules in orders:
            for year, expected in (
                (2015, specific_2015),
                (2024, specific_2024),
                (1999, generic),
            ):
                with self.subTest(rules=rules, year=year):
                    match = matching_forced_rule(
                        {
                            "title": "Cenicienta",
                            "year": year,
                            "_title_candidates": ["Cenicienta"],
                        },
                        rules,
                    )
                    self.assertEqual(match, expected)

    def test_early_stop_cannot_be_weaker_than_final_acceptance(self):
        calls = []

        def get_payload(_endpoint, params):
            calls.append(dict(params))
            candidate = (
                self._movie_payload(1, "Candidato parcial", 2024)
                if len(calls) == 1
                else self._movie_payload(2, "Objetivo", 2024)
            )
            return {"results": [candidate]}

        def ranker(candidates, _guessed, _evidence, _direct):
            for candidate in candidates:
                candidate.score = 70 if candidate.tmdb_id == 1 else 100
            return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)

        def details(_media_type, tmdb_id, _language):
            title = "Candidato parcial" if tmdb_id == 1 else "Objetivo"
            return ResolverCandidate(tmdb_id, "movie", title, title, 2024, [title])

        result = search_candidates(
            "movie",
            "Objetivo",
            {"title": "Objetivo", "year": 2024, "_title_candidates": []},
            "es-ES",
            "ES",
            {
                "query_variants": {"with_year": True, "without_year": True},
                "search_limits": {"max_searches": 2},
                "acceptance": {
                    "min_score": 90,
                    "min_margin": 0,
                    "early_stop_score": 60,
                    "early_stop_margin": 0,
                    "early_stop_require_exact_movie_year": True,
                },
            },
            get_payload,
            details,
            ranker,
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(result[0].tmdb_id, 2)

    def test_exact_year_candidate_replaces_last_reserved_detail_slot(self):
        payloads = [
            self._movie_payload(1, "Primera", 2020),
            self._movie_payload(2, "Segunda", 2021),
            self._movie_payload(3, "Exacta", 2024),
        ]
        detail_calls = []

        def ranker(candidates, _guessed, _evidence, _direct):
            scores = {1: 100, 2: 90, 3: 80}
            for candidate in candidates:
                candidate.score = scores[candidate.tmdb_id]
            return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)

        def details(_media_type, tmdb_id, _language):
            detail_calls.append(tmdb_id)
            payload = next(item for item in payloads if item["id"] == tmdb_id)
            return ResolverCandidate(
                tmdb_id,
                "movie",
                payload["title"],
                payload["original_title"],
                int(payload["release_date"][:4]),
                [payload["title"]],
            )

        search_candidates(
            "movie",
            "Objetivo",
            {"title": "Objetivo", "year": 2024, "_title_candidates": []},
            "es-ES",
            "ES",
            {
                "query_variants": {"with_year": True, "without_year": False},
                "search_limits": {
                    "max_searches": 1,
                    "initial_candidates": 2,
                    "detail_candidates": 2,
                    "include_exact_year_candidate": True,
                },
                "acceptance": {"early_stop_score": 999, "early_stop_margin": 999},
            },
            lambda _endpoint, _params: {"results": payloads},
            details,
            ranker,
        )

        self.assertEqual(detail_calls, [1, 3])

if __name__ == "__main__":
    unittest.main()
