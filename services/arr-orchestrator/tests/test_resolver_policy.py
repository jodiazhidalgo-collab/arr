import unittest

from arr_orchestrator.identity.resolver.models import ResolverCandidate
from arr_orchestrator.identity.resolver.policy import effective_policy
from arr_orchestrator.identity.resolver.scoring import score_candidate


class ResolverPolicyTests(unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()
