import unittest
from unittest.mock import patch

from arr_orchestrator.identity.resolver import cache as resolver_cache
from arr_orchestrator.identity.resolver.cache import RESOLVER_CACHE_VERSION, cache_key
from arr_orchestrator.identity.resolver.candidate_search import (
    _build_query_inputs,
    _has_early_stop_evidence,
    search_candidates,
)
from arr_orchestrator.identity.resolver.models import (
    ResolverCandidate,
    ResolverUnavailable,
)
from arr_orchestrator.identity.resolver.policy import effective_policy
from arr_orchestrator.identity.resolver.rules import (
    apply_query_aliases,
    matching_forced_rule,
)
from arr_orchestrator.identity.resolver.scoring import DEFAULT_SCORING, score_candidate
from arr_orchestrator.identity.resolver.service import _select_title_eligible_candidates
from arr_orchestrator.identity.resolver.title_candidates import ensure_title_evidence
from arr_orchestrator.identity.resolver.title_matching import (
    analyze_candidate_title_evidence,
    candidate_title_aliases,
    resolved_title_evidence,
)
from arr_orchestrator.identity.schema import identity_settings_schema


class ResolverPolicyTests(unittest.TestCase):
    @staticmethod
    def _movie_payload(tmdb_id, title, year):
        return {
            "id": tmdb_id,
            "title": title,
            "original_title": title,
            "release_date": f"{year}-01-01" if year else "",
        }

    @staticmethod
    def _evidence_guess(primary, alternate=None, *, year=None, season=None):
        evidence = [
            {
                "value": primary,
                "role": "primary",
                "source": "parentheses" if alternate else "parser",
                "group_id": "parser:0",
            }
        ]
        if alternate:
            evidence.append(
                {
                    "value": alternate,
                    "role": "alternate",
                    "source": "parentheses",
                    "group_id": "parser:0",
                }
            )
        guessed = {
            "title": primary,
            "year": year,
            "_title_evidence": evidence,
        }
        if season is not None:
            guessed["season"] = season
        return guessed

    @staticmethod
    def _matched_candidate(
        tmdb_id,
        title,
        year,
        *,
        level,
        role,
        season_count=None,
        exact=True,
    ):
        return ResolverCandidate(
            tmdb_id,
            "tv" if season_count is not None else "movie",
            title,
            title,
            year,
            [title],
            score=100,
            season_count=season_count,
            title_match_level=level,
            title_matches=[
                {
                    "role": role,
                    "exact": exact,
                    "identity_exact": exact,
                }
            ],
            title_identity_exact_roles=[role] if exact else [],
        )

    def test_factory_policy_preserves_current_resolver_values(self):
        policy = effective_policy({}, "movies")

        self.assertEqual(policy["language"], "es-ES")
        self.assertEqual(policy["region"], "ES")
        self.assertEqual(policy["search_limits"]["max_searches"], 8)
        self.assertEqual(policy["acceptance"]["min_score"], 75)
        self.assertEqual(policy["acceptance"]["min_margin"], 12)
        self.assertFalse(
            policy["acceptance"]["prefer_oldest_exact_title_without_year"]
        )
        self.assertEqual(policy["cache"]["ttl_seconds"], 30 * 24 * 3600)
        self.assertEqual(
            policy["title_matching"],
            {
                "score_parser_candidates": True,
                "roman_arabic_equivalence": True,
                "allow_omitted_part_number": True,
                "omitted_part_min_words": 3,
                "supplemental_min_chars": 3,
            },
        )

    def test_cache_v3_keys_include_structured_title_evidence(self):
        guessed = {
            "title": "Incontrolable",
            "year": 2024,
            "_title_candidates": ["Incontrolable", "Unstoppable"],
            "_title_evidence": [
                {
                    "value": "Incontrolable",
                    "role": "primary",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
                {
                    "value": "Unstoppable",
                    "role": "alternate",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
            ],
        }
        altered = {
            **guessed,
            "_title_evidence": [
                *guessed["_title_evidence"][:1],
                {
                    **guessed["_title_evidence"][1],
                    "role": "composite",
                },
            ],
        }

        self.assertEqual(RESOLVER_CACHE_VERSION, 3)
        first = cache_key("movie", ["Incontrolable"], guessed, None, None)
        self.assertEqual(
            first,
            cache_key("movie", ["Incontrolable"], guessed, None, None),
        )
        self.assertNotEqual(
            first,
            cache_key("movie", ["Incontrolable"], altered, None, None),
        )
        with patch.object(resolver_cache, "RESOLVER_CACHE_VERSION", 2):
            legacy_v2 = cache_key(
                "movie",
                ["Incontrolable"],
                guessed,
                None,
                None,
            )
        self.assertNotEqual(first, legacy_v2)

    def test_legacy_flat_candidates_receive_explicit_safe_evidence(self):
        guessed = {
            "title": "Incontrolable",
            "_display_title": "Incontrolable (Unstoppable)",
            "_title_candidates": [
                "Incontrolable",
                "Unstoppable",
                "INCONTROLABLE",
                "",
            ],
        }

        evidence = ensure_title_evidence(guessed)

        self.assertEqual(
            [
                (item.value, item.role, item.source, item.group_id)
                for item in evidence
            ],
            [
                ("Incontrolable", "primary", "legacy", "legacy:0"),
                (
                    "Incontrolable (Unstoppable)",
                    "composite",
                    "legacy",
                    "legacy:0",
                ),
                ("Unstoppable", "alternate", "legacy", "legacy:0"),
            ],
        )
        self.assertEqual(
            resolved_title_evidence(guessed),
            [item.to_dict() for item in evidence],
        )
        self.assertNotIn(
            "derived_primary",
            {item.role for item in evidence},
        )
        self.assertNotIn("_title_evidence", guessed)
        self.assertEqual(
            guessed["_title_candidates"],
            ["Incontrolable", "Unstoppable", "INCONTROLABLE", ""],
        )

    def test_legacy_flat_slash_is_composite_and_cannot_drive_fallback(self):
        guessed = {
            "title": "Incontrolable",
            "year": 2025,
            "_title_candidates": [
                "Incontrolable",
                "Incontrolable / I Swear",
            ],
        }

        ensured = ensure_title_evidence(guessed)
        resolved = resolved_title_evidence(guessed)
        candidate = ResolverCandidate(
            30,
            "movie",
            "Incontrolable / I Swear",
            "Incontrolable / I Swear",
            2025,
            ["Incontrolable / I Swear"],
        )
        score_candidate(candidate, guessed, [], False)
        eligibility = _select_title_eligible_candidates(
            [candidate],
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )

        expected = [
            ("Incontrolable", "primary", "legacy", "legacy:0"),
            (
                "Incontrolable / I Swear",
                "composite",
                "legacy",
                "legacy:0",
            ),
        ]
        self.assertEqual(
            [(item.value, item.role, item.source, item.group_id) for item in ensured],
            expected,
        )
        self.assertEqual(
            [
                (
                    item["value"],
                    item["role"],
                    item["source"],
                    item["group_id"],
                )
                for item in resolved
            ],
            expected,
        )
        self.assertEqual(candidate.title_match_level, "none")
        self.assertIn("composite", candidate.title_identity_exact_roles)
        self.assertEqual(eligibility["reason_code"], "title_evidence_unconfirmed")
        self.assertFalse(eligibility["strict_fallback_applied"])
        self.assertEqual(eligibility["eligible"], [])

    def test_legacy_editorial_auxiliary_is_filtered_and_descriptor_is_ineligible(self):
        guessed = {
            "title": "Blade Runner",
            "_display_title": "Extended Edition",
            "year": 1982,
            "_title_candidates": ["Blade Runner", "Extended Edition"],
        }

        ensured = ensure_title_evidence(guessed)
        resolved = resolved_title_evidence(guessed)
        candidate = ResolverCandidate(
            31,
            "movie",
            "Extended Edition",
            "Extended Edition",
            1982,
            ["Extended Edition"],
        )
        score_candidate(candidate, guessed, [], False)
        eligibility = _select_title_eligible_candidates(
            [candidate],
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )

        expected = [("Blade Runner", "primary", "legacy", "legacy:0")]
        self.assertEqual(
            [(item.value, item.role, item.source, item.group_id) for item in ensured],
            expected,
        )
        self.assertEqual(
            [
                (
                    item["value"],
                    item["role"],
                    item["source"],
                    item["group_id"],
                )
                for item in resolved
            ],
            expected,
        )
        self.assertEqual(candidate.title_match_level, "none")
        self.assertNotIn("alternate", candidate.title_identity_exact_roles)
        self.assertEqual(eligibility["reason_code"], "title_evidence_unconfirmed")
        self.assertEqual(eligibility["eligible"], [])

    def test_title_eligibility_keeps_simple_configured_and_direct_paths(self):
        simple = self._matched_candidate(
            1,
            "Titulo",
            2024,
            level="primary",
            role="primary",
        )
        simple_result = _select_title_eligible_candidates(
            [simple],
            guessed=self._evidence_guess("Titulo", year=2024),
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )
        self.assertEqual(simple_result["reason_code"], "single_primary_title")
        self.assertEqual([item.tmdb_id for item in simple_result["eligible"]], [1])

        uncertain_simple = self._matched_candidate(
            5,
            "Titulo",
            2024,
            level="primary",
            role="primary",
        )
        uncertain_result = _select_title_eligible_candidates(
            [uncertain_simple],
            guessed=self._evidence_guess("Titulo", year=2024),
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=True,
        )
        self.assertEqual(
            uncertain_result["reason_code"],
            "title_selection_uncertain",
        )
        self.assertEqual(uncertain_result["eligible"], [])

        configured = self._matched_candidate(
            2,
            "Alias configurado",
            2024,
            level="configured",
            role="configured_primary",
        )
        rival = self._matched_candidate(
            3,
            "Titulo",
            2024,
            level="primary",
            role="primary",
        )
        configured_result = _select_title_eligible_candidates(
            [configured, rival],
            guessed=self._evidence_guess("Titulo", "Alternate", year=2024),
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )
        self.assertEqual(configured_result["reason_code"], "configured_primary")
        self.assertEqual(
            [item.tmdb_id for item in configured_result["eligible"]],
            [2],
        )

        for source in ("tmdb_id", "imdb_id", "forced_match"):
            with self.subTest(source=source):
                direct = self._matched_candidate(
                    4,
                    "Destino directo",
                    2024,
                    level="direct",
                    role="primary",
                )
                result = _select_title_eligible_candidates(
                    [direct],
                    guessed=self._evidence_guess(
                        "Incontrolable",
                        "Unstoppable",
                        year=2024,
                    ),
                    media_type="movie",
                    source=source,
                    title_matching={},
                    selection_uncertain=True,
                )
                self.assertEqual(result["reason_code"], f"{source}_validated")
                self.assertEqual(
                    [item.tmdb_id for item in result["eligible"]],
                    [4],
                )

    def test_short_alternate_is_filtered_before_eligibility_and_early_stop(self):
        guessed = self._evidence_guess("Main title", "EP", year=2024)
        candidate = ResolverCandidate(
            6,
            "movie",
            "Main title",
            "Main title",
            2024,
            ["Main title"],
        )
        score_candidate(candidate, guessed, [], False)
        eligibility = _select_title_eligible_candidates(
            [candidate],
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )

        self.assertEqual(
            [item["role"] for item in resolved_title_evidence(guessed)],
            ["primary"],
        )
        self.assertFalse(
            any(item["role"] == "alternate" for item in candidate.title_matches)
        )
        self.assertEqual(eligibility["reason_code"], "single_primary_title")
        self.assertEqual([item.tmdb_id for item in eligibility["eligible"]], [6])
        calls = []
        trace = {}

        def ranker(candidates, rank_guess, _evidence, _direct):
            for item in candidates:
                item.score, item.breakdown = score_candidate(
                    item,
                    rank_guess,
                    [],
                    False,
                )
            return sorted(candidates, key=lambda item: item.score, reverse=True)

        searched = search_candidates(
            "movie",
            "Main title",
            guessed,
            "es-ES",
            "ES",
            {
                "use_fallback_language": False,
                "query_variants": {
                    "with_year": True,
                    "without_year": False,
                    "use_parser_candidates": True,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {"max_searches": 8},
                "acceptance": {
                    "min_score": 75,
                    "min_margin": 12,
                    "early_stop_score": 75,
                    "early_stop_margin": 12,
                },
            },
            lambda _endpoint, params: calls.append(dict(params))
            or {"results": [self._movie_payload(6, "Main title", 2024)]},
            lambda *_args: ResolverCandidate(
                6,
                "movie",
                "Main title",
                "Main title",
                2024,
                ["Main title"],
            ),
            ranker,
            selection_trace=trace,
        )

        self.assertEqual([item.tmdb_id for item in searched], [6])
        self.assertEqual([item["query"] for item in calls], ["Main title"])
        self.assertEqual(
            trace["search_strategy"]["early_stop_reason"],
            "single_primary_confirmed",
        )

    def test_strict_movie_alternate_requires_unique_exact_year_without_rival(self):
        guessed = self._evidence_guess(
            "Incontrolable",
            "Unstoppable",
            year=2024,
        )

        safe = _select_title_eligible_candidates(
            [
                self._matched_candidate(
                    10,
                    "Incontrolable",
                    2023,
                    level="primary",
                    role="primary",
                ),
                self._matched_candidate(
                    11,
                    "Unstoppable",
                    2024,
                    level="alternate",
                    role="alternate",
                ),
            ],
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )
        self.assertEqual(safe["reason_code"], "strict_alternate_fallback")
        self.assertTrue(safe["strict_fallback_applied"])
        self.assertEqual([item.tmdb_id for item in safe["eligible"]], [11])

        conflict = _select_title_eligible_candidates(
            [
                self._matched_candidate(
                    12,
                    "Incontrolable",
                    2024,
                    level="primary",
                    role="primary",
                ),
                self._matched_candidate(
                    13,
                    "Unstoppable",
                    2024,
                    level="alternate",
                    role="alternate",
                ),
            ],
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )
        self.assertEqual(conflict["reason_code"], "title_evidence_conflict")
        self.assertEqual(conflict["eligible"], [])

        uncertain = _select_title_eligible_candidates(
            [
                self._matched_candidate(
                    14,
                    "Unstoppable",
                    2024,
                    level="alternate",
                    role="alternate",
                )
            ],
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=True,
        )
        self.assertEqual(uncertain["reason_code"], "title_selection_uncertain")
        self.assertEqual(uncertain["eligible"], [])

        similar_primary = _select_title_eligible_candidates(
            [
                self._matched_candidate(
                    15,
                    "Incontrolable rival",
                    2024,
                    level="primary",
                    role="primary",
                    exact=False,
                )
            ],
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )
        self.assertEqual(
            similar_primary["reason_code"],
            "title_evidence_unconfirmed",
        )
        self.assertEqual(similar_primary["eligible"], [])

    def test_corroborated_uncertainty_bypass_requires_every_safety_guard(self):
        guessed = self._evidence_guess(
            "Incontrolable",
            "I Swear",
            year=2025,
        )
        cases = (
            ("omitted_strong_or_composite", False, False, False),
            ("detail_incomplete", True, True, False),
            ("configured_for_year", True, False, True),
        )

        for label, alternate_only, detail_incomplete, include_configured in cases:
            with self.subTest(case=label):
                candidates = [
                    self._matched_candidate(
                        41,
                        "Incontrolable",
                        2025,
                        level="corroborated",
                        role="primary",
                    )
                ]
                if include_configured:
                    candidates.append(
                        self._matched_candidate(
                            42,
                            "Configured target",
                            2025,
                            level="configured",
                            role="configured_primary",
                        )
                    )

                result = _select_title_eligible_candidates(
                    candidates,
                    guessed=guessed,
                    media_type="movie",
                    source="search",
                    title_matching={},
                    selection_uncertain=True,
                    selection_uncertainty_alternate_only=alternate_only,
                    detail_incomplete=detail_incomplete,
                )

                self.assertEqual(
                    result["reason_code"],
                    "title_selection_uncertain",
                )
                self.assertFalse(result["strict_fallback_applied"])
                self.assertEqual(result["eligible"], [])

    def test_omitted_part_match_scores_but_never_enables_strict_fallback(self):
        guessed = self._evidence_guess(
            "Unrelated local title",
            "Long Saga Chapter final",
            year=2024,
        )
        guessed["_title_candidates"] = [
            "Unrelated local title",
            "Long Saga Chapter final",
        ]
        candidate = ResolverCandidate(
            16,
            "movie",
            "Long Saga Chapter IV final",
            "Long Saga Chapter IV final",
            2024,
            ["Long Saga Chapter IV final"],
        )

        _, breakdown = score_candidate(candidate, guessed, [], False)
        alternate_match = next(
            item for item in candidate.title_matches if item["role"] == "alternate"
        )
        eligibility = _select_title_eligible_candidates(
            [candidate],
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )

        self.assertIn("parser_exact", self._breakdown_keys(breakdown))
        self.assertIn(
            {
                "path": "resolver.title_matching.allow_omitted_part_number",
                "detail": "Número de saga omitido",
            },
            candidate.matching_rules,
        )
        self.assertTrue(alternate_match["exact"])
        self.assertFalse(alternate_match["identity_exact"])
        self.assertEqual(eligibility["reason_code"], "title_evidence_unconfirmed")
        self.assertFalse(eligibility["strict_fallback_applied"])
        self.assertEqual(eligibility["eligible"], [])

    def test_omitted_part_match_never_corroborates_an_exact_primary(self):
        analysis = analyze_candidate_title_evidence(
            ["Local title", "Long Saga Chapter IV final"],
            self._evidence_guess(
                "Local title",
                "Long Saga Chapter final",
                year=2024,
            ),
        )
        alternate_match = next(
            item for item in analysis["matches"] if item["role"] == "alternate"
        )

        self.assertTrue(alternate_match["exact"])
        self.assertFalse(alternate_match["identity_exact"])
        self.assertEqual(analysis["corroborated_groups"], [])
        self.assertEqual(analysis["level"], "primary")

    def test_la_oficina_alternate_requires_2005_and_valid_season(self):
        alternate = lambda season_count=9: self._matched_candidate(
            2316,
            "The Office",
            2005,
            level="alternate",
            role="alternate",
            season_count=season_count,
        )

        without_year = _select_title_eligible_candidates(
            [alternate()],
            guessed=self._evidence_guess(
                "La oficina",
                "The Office",
                season=2,
            ),
            media_type="tv",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )
        self.assertEqual(
            without_year["reason_code"],
            "alternate_fallback_requires_year",
        )
        self.assertEqual(without_year["eligible"], [])

        with_year = _select_title_eligible_candidates(
            [alternate()],
            guessed=self._evidence_guess(
                "La oficina",
                "The Office",
                year=2005,
                season=2,
            ),
            media_type="tv",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )
        self.assertEqual(with_year["reason_code"], "strict_alternate_fallback")
        self.assertEqual(
            [item.tmdb_id for item in with_year["eligible"]],
            [2316],
        )

        invalid_season = _select_title_eligible_candidates(
            [alternate(season_count=1)],
            guessed=self._evidence_guess(
                "La oficina",
                "The Office",
                year=2005,
                season=2,
            ),
            media_type="tv",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )
        self.assertEqual(
            invalid_season["reason_code"],
            "season_impossible",
        )
        self.assertEqual(invalid_season["eligible"], [])

    def test_title_match_trace_is_compact_and_single_line(self):
        noisy = "Titulo\n" + ("X" * 200)
        analysis = analyze_candidate_title_evidence(
            [noisy],
            {
                "title": noisy,
                "_title_evidence": [
                    {
                        "value": noisy,
                        "role": "primary",
                        "source": "parser",
                        "group_id": "parser:0",
                    }
                ],
            },
        )

        self.assertEqual(analysis["level"], "primary")
        for item in analysis["matches"]:
            self.assertNotIn("\n", item["value"])
            self.assertNotIn("\n", item["matched_alias"])
            self.assertLessEqual(len(item["value"]), 80)
            self.assertLessEqual(len(item["matched_alias"]), 80)

    def test_exact_role_after_trace_limit_remains_eligible(self):
        guessed = {
            "title": "Exact target",
            "year": 2024,
            "_title_evidence": [
                {
                    "value": f"Noise alternate {index}",
                    "role": "alternate",
                    "source": "legacy",
                    "group_id": f"noise:{index}",
                }
                for index in range(16)
            ]
            + [
                {
                    "value": "Exact target",
                    "role": "primary",
                    "source": "parser",
                    "group_id": "parser:0",
                }
            ],
        }
        candidate = ResolverCandidate(
            29,
            "movie",
            "Exact target",
            "Exact target",
            2024,
            ["Exact target"],
        )

        score_candidate(candidate, guessed, [], False)
        eligibility = _select_title_eligible_candidates(
            [candidate],
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )

        self.assertEqual(len(candidate.title_matches), 16)
        self.assertFalse(
            any(item["role"] == "primary" for item in candidate.title_matches)
        )
        self.assertIn("primary", candidate.title_identity_exact_roles)
        self.assertEqual(
            eligibility["reason_code"],
            "primary_without_alternate_conflict",
        )
        self.assertEqual([item.tmdb_id for item in eligibility["eligible"]], [29])

    def test_near_primary_never_corroborates_an_exact_alternate(self):
        analysis = analyze_candidate_title_evidence(
            ["Incontrolables", "Unstoppable"],
            self._evidence_guess("Incontrolable", "Unstoppable", year=2024),
        )

        self.assertTrue(analysis["primary_supported"])
        self.assertFalse(analysis["primary_exact"])
        self.assertTrue(analysis["alternate_exact"])
        self.assertEqual(analysis["corroborated_groups"], [])
        self.assertEqual(analysis["level"], "primary")

    def test_composite_literal_alone_is_not_atomic_corroboration_or_early_stop(self):
        guessed = self._evidence_guess("Incontrolable", "I Swear", year=2025)
        guessed["_title_evidence"].append(
            {
                "value": "Incontrolable (I Swear)",
                "role": "composite",
                "source": "parentheses",
                "group_id": "parser:0",
            }
        )
        analysis = analyze_candidate_title_evidence(
            ["Incontrolable (I Swear)"],
            guessed,
        )
        candidate = ResolverCandidate(
            77,
            "movie",
            "Incontrolable (I Swear)",
            "Incontrolable (I Swear)",
            2025,
            ["Incontrolable (I Swear)"],
            title_match_level=str(analysis["level"]),
        )

        self.assertTrue(analysis["composite_exact"])
        self.assertEqual(analysis["corroborated_groups"], [])
        self.assertNotEqual(analysis["level"], "corroborated")
        self.assertFalse(
            _has_early_stop_evidence(
                candidate,
                {"_search_sources": ["composite"]},
                guessed,
            )
        )

    def test_candidate_composite_aliases_filter_editorial_and_year_parentheses(self):
        self.assertEqual(
            candidate_title_aliases(
                ["Incontrolable (I Swear)", "I Swear"]
            ),
            ["Incontrolable (I Swear)", "I Swear", "Incontrolable"],
        )
        self.assertEqual(
            candidate_title_aliases(["Incontrolable (I Swear)"]),
            ["Incontrolable (I Swear)"],
        )

        chained_aliases = candidate_title_aliases(
            ["Alpha (Beta)", "Beta", "Gamma (Alpha)"]
        )
        chained = analyze_candidate_title_evidence(
            chained_aliases,
            self._evidence_guess("Gamma", "Alpha"),
        )
        self.assertEqual(candidate_title_aliases(chained_aliases), chained_aliases)
        self.assertNotIn("Gamma", chained_aliases)
        self.assertEqual(chained["corroborated_groups"], [])
        self.assertNotEqual(chained["level"], "corroborated")

        editorial_aliases = candidate_title_aliases(
            ["Blade Runner (Extended Edition)", "Blade Runner"]
        )
        editorial = analyze_candidate_title_evidence(
            editorial_aliases,
            self._evidence_guess(
                "Blade Runner",
                "Extended Edition",
                year=1982,
            ),
        )
        self.assertNotIn("Extended Edition", editorial_aliases)
        self.assertEqual(editorial["corroborated_groups"], [])
        self.assertFalse(editorial["alternate_exact"])

        year_aliases = candidate_title_aliases(
            ["Incontrolable (2025)", "Incontrolable"]
        )
        year = analyze_candidate_title_evidence(
            year_aliases,
            self._evidence_guess("Incontrolable", "2025", year=2025),
        )
        self.assertNotIn("2025", year_aliases)
        self.assertEqual(year["corroborated_groups"], [])
        self.assertFalse(year["alternate_exact"])

        for qualifier in (
            "International Version",
            "Miniseries",
            "TV Movie",
            "Television Movie",
        ):
            with self.subTest(qualifier=qualifier):
                qualifier_aliases = candidate_title_aliases(
                    [f"Target ({qualifier})", qualifier]
                )
                qualifier_analysis = analyze_candidate_title_evidence(
                    qualifier_aliases,
                    self._evidence_guess("Target", qualifier),
                )
                self.assertNotIn("Target", qualifier_aliases)
                self.assertFalse(qualifier_analysis["alternate_exact"])
                self.assertEqual(qualifier_analysis["corroborated_groups"], [])

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
                    "acceptance": {
                        "min_score": 60,
                        "min_margin": 7,
                        "prefer_oldest_exact_title_without_year": True,
                    },
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
        self.assertTrue(
            policy["acceptance"]["prefer_oldest_exact_title_without_year"]
        )
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

        factory_score, factory_breakdown = score_candidate(candidate, guessed, [], False)
        custom_score, custom_breakdown = score_candidate(
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
        self.assertIn("title_exact", self._breakdown_keys(factory_breakdown))
        self.assertIn("year_exact", self._breakdown_keys(custom_breakdown))
        self.assertEqual(
            sum(float(item["applied"]) for item in factory_breakdown),
            factory_score,
        )
        self.assertEqual(
            sum(float(item["applied"]) for item in custom_breakdown),
            custom_score,
        )
        custom_title = next(item for item in custom_breakdown if item["key"] == "title_exact")
        self.assertEqual(custom_title["path"], "resolver.scoring.title_exact")
        self.assertEqual(custom_title["configured"], 3)
        self.assertEqual(custom_title["applied"], 3)

    def test_scoring_normalizes_apostrophes_without_title_specific_rules(self):
        candidate = ResolverCandidate(
            10,
            "movie",
            "King's Journey",
            "King's Journey",
            2024,
            ["King's Journey"],
        )

        for query in ("Kings Journey", "King s Journey", "King,s Journey"):
            with self.subTest(query=query):
                score, breakdown = score_candidate(
                    candidate,
                    {
                        "title": query,
                        "year": 2024,
                        "_title_candidates": [query],
                    },
                    [],
                    False,
                )

                self.assertEqual(score, 110.0)
                self.assertIn("title_exact", self._breakdown_keys(breakdown))
                self.assertIn("parser_exact", self._breakdown_keys(breakdown))

    def test_scoring_matches_roman_arabic_and_safe_omitted_ordinals(self):
        cases = (
            ("Saga 3 Capitulo final", "Saga III Capitulo final"),
            ("Saga Capitulo final", "Saga IV Capitulo final"),
        )
        for query, title in cases:
            with self.subTest(query=query, title=title):
                candidate = ResolverCandidate(
                    11,
                    "movie",
                    title,
                    title,
                    2024,
                    [title],
                )
                score, breakdown = score_candidate(
                    candidate,
                    {
                        "title": query,
                        "year": 2024,
                        "_title_candidates": [query],
                    },
                    [],
                    False,
                )

                self.assertEqual(score, 110.0)
                self.assertIn("title_exact", self._breakdown_keys(breakdown))

    def test_ordinal_tolerance_does_not_collapse_short_or_conflicting_sagas(self):
        cases = (
            ("Rocky", "Rocky II"),
            ("Saga III Capitulo final", "Saga IV Capitulo final"),
        )
        for query, title in cases:
            with self.subTest(query=query, title=title):
                candidate = ResolverCandidate(
                    12,
                    "movie",
                    title,
                    title,
                    2024,
                    [title],
                )
                score, breakdown = score_candidate(
                    candidate,
                    {
                        "title": query,
                        "year": 2024,
                        "_title_candidates": [query],
                    },
                    [],
                    False,
                )
                keys = self._breakdown_keys(breakdown)

                self.assertLess(score, 75)
                self.assertNotIn("title_exact", keys)
                self.assertNotIn("parser_exact", keys)

    def test_tiny_supplemental_candidate_cannot_decide_an_unrelated_identity(self):
        candidate = ResolverCandidate(
            13,
            "movie",
            "EP",
            "EP",
            None,
            ["EP"],
        )

        score, breakdown = score_candidate(
            candidate,
            {
                "title": "The Long Musical Release",
                "year": 2007,
                "_title_candidates": ["The Long Musical Release", "EP"],
            },
            ["The Long Musical Release EP 2007 FLAC lossless"],
            False,
        )
        keys = self._breakdown_keys(breakdown)

        self.assertLess(score, 75)
        self.assertNotIn("title_exact", keys)
        self.assertNotIn("parser_exact", keys)

    def test_legacy_auxiliary_is_scored_but_not_promoted_to_primary(self):
        candidate = ResolverCandidate(
            13,
            "movie",
            "Clean Global Title",
            "Clean Global Title",
            2024,
            ["Clean Global Title"],
        )

        score, breakdown = score_candidate(
            candidate,
            {
                "title": "Etiqueta Titulo Sucio",
                "year": 2024,
                "_title_candidates": [
                    "Etiqueta Titulo Sucio",
                    "Clean Global Title",
                ],
            },
            [],
            False,
        )

        self.assertLess(score, 75)
        self.assertNotIn("title_exact", self._breakdown_keys(breakdown))
        self.assertIn("parser_exact", self._breakdown_keys(breakdown))
        self.assertEqual(candidate.title_match_level, "alternate")

    def test_parser_candidates_can_be_removed_from_scoring_without_affecting_primary(self):
        candidate = ResolverCandidate(
            14,
            "movie",
            "Clean Global Title",
            "Clean Global Title",
            2024,
            ["Clean Global Title"],
        )
        guessed = {
            "title": "Etiqueta Titulo Sucio",
            "year": 2024,
            "_title_candidates": ["Clean Global Title"],
        }

        enabled_score, enabled_breakdown = score_candidate(
            candidate,
            guessed,
            [],
            False,
        )
        disabled_score, disabled_breakdown = score_candidate(
            candidate,
            guessed,
            [],
            False,
            title_matching={"score_parser_candidates": False},
        )

        self.assertGreater(enabled_score, disabled_score)
        self.assertNotIn("title_exact", self._breakdown_keys(enabled_breakdown))
        self.assertIn("parser_exact", self._breakdown_keys(enabled_breakdown))
        self.assertNotIn("title_exact", self._breakdown_keys(disabled_breakdown))
        self.assertNotIn("parser_exact", self._breakdown_keys(disabled_breakdown))
        self.assertEqual(candidate.matching_rules, [])

        primary = ResolverCandidate(
            15,
            "movie",
            "Titulo primario",
            "Titulo primario",
            2024,
            ["Titulo primario"],
        )
        _, primary_breakdown = score_candidate(
            primary,
            {"title": "Titulo primario", "year": 2024},
            [],
            False,
            title_matching={"score_parser_candidates": False},
        )
        self.assertIn("title_exact", self._breakdown_keys(primary_breakdown))

    def test_roman_equivalence_toggle_and_trace_are_independent(self):
        candidate = ResolverCandidate(
            16,
            "movie",
            "Saga III Capitulo final",
            "Saga III Capitulo final",
            2024,
            ["Saga III Capitulo final"],
        )
        guessed = {"title": "Saga 3 Capitulo final", "year": 2024}

        _, enabled_breakdown = score_candidate(candidate, guessed, [], False)
        enabled_rules = list(candidate.matching_rules)
        _, disabled_breakdown = score_candidate(
            candidate,
            guessed,
            [],
            False,
            title_matching={"roman_arabic_equivalence": False},
        )

        self.assertIn("title_exact", self._breakdown_keys(enabled_breakdown))
        self.assertNotIn("title_exact", self._breakdown_keys(disabled_breakdown))
        self.assertEqual(
            enabled_rules,
            [
                {
                    "path": "resolver.title_matching.roman_arabic_equivalence",
                    "detail": "III = 3",
                }
            ],
        )
        self.assertEqual(candidate.matching_rules, [])

    def test_omitted_part_toggle_and_minimum_words_control_the_match(self):
        candidate = ResolverCandidate(
            17,
            "movie",
            "Saga IV Capitulo final",
            "Saga IV Capitulo final",
            2024,
            ["Saga IV Capitulo final"],
        )
        guessed = {"title": "Saga Capitulo final", "year": 2024}

        _, enabled_breakdown = score_candidate(candidate, guessed, [], False)
        enabled_rules = list(candidate.matching_rules)
        _, disabled_breakdown = score_candidate(
            candidate,
            guessed,
            [],
            False,
            title_matching={"allow_omitted_part_number": False},
        )
        _, strict_breakdown = score_candidate(
            candidate,
            guessed,
            [],
            False,
            title_matching={"omitted_part_min_words": 4},
        )

        self.assertIn("title_exact", self._breakdown_keys(enabled_breakdown))
        self.assertNotIn("title_exact", self._breakdown_keys(disabled_breakdown))
        self.assertNotIn("title_exact", self._breakdown_keys(strict_breakdown))
        self.assertEqual(
            enabled_rules,
            [
                {
                    "path": "resolver.title_matching.allow_omitted_part_number",
                    "detail": "Número de saga omitido",
                }
            ],
        )

    def test_supplemental_minimum_chars_controls_short_parser_candidate(self):
        candidate = ResolverCandidate(18, "movie", "EP", "EP", None, ["EP"])
        guessed = {
            "title": "The Long Musical Release",
            "year": 2007,
            "_title_candidates": ["EP"],
        }

        _, default_breakdown = score_candidate(candidate, guessed, [], False)
        _, relaxed_breakdown = score_candidate(
            candidate,
            guessed,
            [],
            False,
            title_matching={"supplemental_min_chars": 2},
        )

        self.assertNotIn("title_exact", self._breakdown_keys(default_breakdown))
        self.assertNotIn("parser_exact", self._breakdown_keys(default_breakdown))
        self.assertNotIn("title_exact", self._breakdown_keys(relaxed_breakdown))
        self.assertIn("parser_exact", self._breakdown_keys(relaxed_breakdown))
        self.assertEqual(
            candidate.matching_rules,
            [
                {
                    "path": "resolver.title_matching.score_parser_candidates",
                    "detail": "Título auxiliar del parser: EP",
                }
            ],
        )

    def test_internal_normalization_does_not_create_visible_matching_rules(self):
        candidate = ResolverCandidate(
            19,
            "movie",
            "Ángel's Journey",
            "Ángel's Journey",
            2024,
            ["Ángel's Journey"],
        )

        _, breakdown = score_candidate(
            candidate,
            {"title": "Angels Journey", "year": 2024},
            [],
            False,
        )

        self.assertIn("title_exact", self._breakdown_keys(breakdown))
        self.assertEqual(candidate.matching_rules, [])

    def test_matching_rules_are_serialized_with_each_candidate(self):
        candidate = ResolverCandidate(
            20,
            "movie",
            "Saga III Capitulo final",
            "Saga III Capitulo final",
            2024,
            ["Saga III Capitulo final"],
        )
        score_candidate(
            candidate,
            {"title": "Saga 3 Capitulo final", "year": 2024},
            [],
            False,
        )

        self.assertEqual(candidate.to_dict()["matching_rules"], candidate.matching_rules)

    def test_primary_parser_title_is_not_presented_as_an_auxiliary_rule(self):
        candidate = ResolverCandidate(
            21,
            "movie",
            "Titulo principal",
            "Titulo principal",
            2024,
            ["Titulo principal"],
        )

        score_candidate(
            candidate,
            {
                "title": "Titulo principal",
                "year": 2024,
                "_title_candidates": ["Titulo principal"],
            },
            [],
            False,
        )

        self.assertEqual(candidate.matching_rules, [])

    def test_legacy_auxiliary_without_bonus_is_not_promoted_or_traced(self):
        candidate = ResolverCandidate(
            22,
            "movie",
            "Target title",
            "Target title",
            2024,
            ["Target title"],
        )
        score_candidate(
            candidate,
            {
                "title": "Noise release title",
                "year": 2024,
                "_title_candidates": ["Target partial"],
            },
            [],
            False,
            settings={"parser_exact": 0, "parser_near": 0},
        )

        self.assertEqual(candidate.matching_rules, [])
        self.assertNotEqual(candidate.title_match_level, "primary")

    def test_exact_parser_auxiliary_is_traced_when_parser_bonus_is_zero(self):
        candidate = ResolverCandidate(
            23,
            "movie",
            "Target title",
            "Target title",
            2024,
            ["Target title"],
        )
        score, breakdown = score_candidate(
            candidate,
            {
                "title": "Noise release title",
                "year": 2024,
                "_title_candidates": ["Target title"],
            },
            [],
            False,
            settings={"parser_exact": 0, "parser_near": 0},
        )

        self.assertLess(score, 75)
        self.assertNotIn("title_exact", self._breakdown_keys(breakdown))
        self.assertNotIn("parser_exact", self._breakdown_keys(breakdown))
        self.assertEqual(candidate.matching_rules, [])
        self.assertEqual(candidate.title_match_level, "alternate")
        self.assertTrue(
            any(
                item["role"] == "alternate" and item["exact"]
                for item in candidate.title_matches
            )
        )

    def test_roman_trace_survives_shifted_and_possessive_tokens(self):
        cases = (
            ("Saga III Final", "The Saga 3 Final"),
            ("King s Saga III Journey", "Kings Saga 3 Journey"),
        )
        for query, title in cases:
            with self.subTest(query=query, title=title):
                candidate = ResolverCandidate(
                    24,
                    "movie",
                    title,
                    title,
                    2024,
                    [title],
                )
                score_candidate(
                    candidate,
                    {"title": query, "year": 2024},
                    [],
                    False,
                )

                self.assertIn(
                    {
                        "path": "resolver.title_matching.roman_arabic_equivalence",
                        "detail": "III = 3",
                    },
                    candidate.matching_rules,
                )

    def test_parser_auxiliary_trace_is_compact_and_sanitized(self):
        auxiliary = "Long auxiliary\n" + ("x" * 120)
        candidate = ResolverCandidate(
            25,
            "movie",
            auxiliary,
            auxiliary,
            2024,
            [auxiliary],
        )
        score_candidate(
            candidate,
            {
                "title": "Noise release title",
                "year": 2024,
                "_title_candidates": [auxiliary],
            },
            [],
            False,
        )

        detail = candidate.matching_rules[0]["detail"]
        self.assertNotIn("\n", detail)
        self.assertTrue(detail.endswith("…"))
        self.assertLessEqual(len(detail), len("Título auxiliar del parser: ") + 80)

    def test_english_pronoun_i_is_never_an_omitted_part_number(self):
        cases = (
            ("I Know What You Did Last Summer", "Know What You Did Last Summer"),
            ("The Day I Met Your Mother", "The Day Met Your Mother"),
            ("Me Myself And I", "Me Myself And"),
        )
        for query, title in cases:
            with self.subTest(query=query):
                candidate = ResolverCandidate(
                    26,
                    "movie",
                    title,
                    title,
                    1997,
                    [title],
                )
                score, breakdown = score_candidate(
                    candidate,
                    {"title": query, "year": 1997, "_title_candidates": [query]},
                    [],
                    False,
                )

                self.assertLess(score, 75)
                self.assertNotIn("title_exact", self._breakdown_keys(breakdown))
                self.assertNotIn("parser_exact", self._breakdown_keys(breakdown))
                self.assertEqual(candidate.matching_rules, [])

    def test_single_roman_i_still_works_with_an_explicit_part_marker(self):
        candidate = ResolverCandidate(
            27,
            "movie",
            "Long Saga Chapter 1 Final",
            "Long Saga Chapter 1 Final",
            2024,
            ["Long Saga Chapter 1 Final"],
        )
        _, breakdown = score_candidate(
            candidate,
            {"title": "Long Saga Chapter I Final", "year": 2024},
            [],
            False,
        )

        self.assertIn("title_exact", self._breakdown_keys(breakdown))
        self.assertIn(
            {
                "path": "resolver.title_matching.roman_arabic_equivalence",
                "detail": "I = 1",
            },
            candidate.matching_rules,
        )

    def test_simple_roman_iv_to_arabic_4_remains_exact_identity_evidence(self):
        guessed = self._evidence_guess(
            "Long Saga Chapter IV Final",
            year=2024,
        )
        candidate = ResolverCandidate(
            28,
            "movie",
            "Long Saga Chapter 4 Final",
            "Long Saga Chapter 4 Final",
            2024,
            ["Long Saga Chapter 4 Final"],
        )

        score_candidate(candidate, guessed, [], False)
        primary_match = next(
            item for item in candidate.title_matches if item["role"] == "primary"
        )
        eligibility = _select_title_eligible_candidates(
            [candidate],
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=False,
        )

        self.assertTrue(primary_match["exact"])
        self.assertTrue(primary_match["identity_exact"])
        self.assertIn(
            {
                "path": "resolver.title_matching.roman_arabic_equivalence",
                "detail": "IV = 4",
            },
            candidate.matching_rules,
        )
        self.assertEqual(eligibility["reason_code"], "single_primary_title")
        self.assertEqual([item.tmdb_id for item in eligibility["eligible"]], [28])

    def test_scoring_breakdown_covers_every_kind_of_points_and_penalty(self):
        movie = ResolverCandidate(
            1,
            "movie",
            "Mi pelicula",
            "Mi pelicula",
            2024,
            ["Mi pelicula"],
        )
        cases = []
        cases.append(score_candidate(movie, {}, [], True))
        cases.append(
            score_candidate(
                movie,
                {
                    "title": "Mi pelicula",
                    "year": 2024,
                    "_title_candidates": ["Mi pelicula"],
                    "_rule_query_aliases": ["Mi pelicula"],
                },
                ["Mi.pelicula.2024.mkv"],
                False,
            )
        )
        cases.append(
            score_candidate(
                ResolverCandidate(2, "movie", "Satisfaccion garantizada", "", 2024, ["Satisfaccion garantizada"]),
                {"title": "Satisfacion garantizada", "year": 2024},
                [],
                False,
            )
        )
        cases.append(
            score_candidate(
                ResolverCandidate(3, "movie", "Objetivo final", "", 2025, ["Objetivo final"]),
                {"title": "Objetivo final", "year": 2024, "_title_candidates": ["Objetivo fina"]},
                [],
                False,
            )
        )
        cases.append(
            score_candidate(
                ResolverCandidate(4, "movie", "Objetivo", "", 2020, ["Objetivo"]),
                {"title": "Objetivo", "year": 2024},
                [],
                False,
            )
        )
        cases.append(
            score_candidate(
                ResolverCandidate(5, "movie", "Objetivo", "", None, ["Objetivo"]),
                {"title": "Objetivo", "year": 2024},
                [],
                False,
            )
        )
        cases.append(
            score_candidate(
                ResolverCandidate(6, "tv", "Serie", "", 2024, ["Serie"], season_count=3),
                {"title": "Serie", "year": 2024, "season": 2},
                [],
                False,
            )
        )
        cases.append(
            score_candidate(
                ResolverCandidate(7, "tv", "Serie", "", 2024, ["Serie"], season_count=3),
                {"title": "Serie", "year": 2024, "season": 9},
                [],
                False,
            )
        )
        cases.append(
            score_candidate(
                ResolverCandidate(8, "movie", "Auxiliar", "", 2024, ["Auxiliar"]),
                {
                    "title": "Titulo principal",
                    "year": 2024,
                    "_title_candidates": ["Auxiliar"],
                },
                [],
                False,
            )
        )

        seen = set()
        for score, breakdown in cases:
            seen.update(self._breakdown_keys(breakdown))
            self.assertAlmostEqual(
                sum(float(item["applied"]) for item in breakdown),
                score,
                places=2,
            )
            self.assertTrue(all(item["path"] == f"resolver.scoring.{item['key']}" for item in breakdown))

        self.assertEqual(
            seen,
            set(DEFAULT_SCORING) - {"parser_near_min", "year_tolerance"},
        )

    def test_every_scoring_key_has_one_canonical_schema_label(self):
        schema = identity_settings_schema()
        paths = [
            control["path"]
            for group in schema["resolver"]["groups"]
            for control in group["controls"]
            if str(control["path"]).startswith("resolver.scoring.")
        ]

        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(
            {path.removeprefix("resolver.scoring.") for path in paths},
            set(DEFAULT_SCORING),
        )

    def test_original_language_preference_has_two_editable_schema_controls(self):
        schema = identity_settings_schema()
        controls = {
            control["path"]: control
            for group in schema["resolver"]["groups"]
            for control in group["controls"]
        }

        self.assertEqual(
            controls["resolver.original_language_preference.language"]["type"],
            "language",
        )
        self.assertEqual(
            controls["resolver.original_language_preference.enabled"]["type"],
            "toggle",
        )
        self.assertEqual(
            controls["resolver.original_language_preference.language"]["label"],
            "Idioma original preferido",
        )

    def test_oldest_exact_title_preference_has_one_acceptance_toggle(self):
        schema = identity_settings_schema()
        matches = [
            (group["title"], control)
            for group in schema["resolver"]["groups"]
            for control in group["controls"]
            if control["path"]
            == "resolver.acceptance.prefer_oldest_exact_title_without_year"
        ]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0], "Aceptacion y validacion")
        self.assertEqual(matches[0][1]["type"], "toggle")

    def test_zero_and_decimal_weights_keep_an_exact_additive_breakdown(self):
        candidate = ResolverCandidate(8, "movie", "Titulo parcial", "", 2024, ["Titulo parcial"])
        guessed = {"title": "Titulo", "year": 2024}
        zero_settings = {
            key: 0
            for key in DEFAULT_SCORING
            if key not in {"parser_near_min", "year_tolerance"}
        }
        zero_score, zero_breakdown = score_candidate(
            candidate,
            guessed,
            [],
            False,
            zero_settings,
        )

        self.assertEqual(zero_score, 0)
        self.assertEqual(zero_breakdown, [])

        decimal_score, decimal_breakdown = score_candidate(
            candidate,
            guessed,
            [],
            False,
            {
                **zero_settings,
                "title_similarity_max": 7.25,
                "token_overlap_max": 2.75,
            },
        )

        self.assertEqual(
            sum(float(item["applied"]) for item in decimal_breakdown),
            decimal_score,
        )
        self.assertTrue(
            all(item["configured"] in {7.25, 2.75} for item in decimal_breakdown)
        )

    @staticmethod
    def _breakdown_keys(breakdown):
        return {str(item["key"]) for item in breakdown}

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

    def test_query_aliases_are_directional_and_do_not_chain_in_one_pass(self):
        guessed = self._evidence_guess("Origen")
        guessed["_title_candidates"] = ["Origen"]

        inverse = apply_query_aliases(
            guessed,
            [
                ("Origen", "Intermedio"),
                ("Intermedio", "Origen"),
            ],
        )
        chained = apply_query_aliases(
            guessed,
            [
                ("Origen", "Intermedio"),
                ("Intermedio", "Final"),
            ],
        )

        self.assertEqual(inverse["_rule_query_aliases"], ["Intermedio"])
        self.assertEqual(chained["_rule_query_aliases"], ["Intermedio"])
        self.assertEqual(
            [
                item["value"]
                for item in chained["_title_evidence"]
                if item["role"] == "configured_primary"
            ],
            ["Intermedio"],
        )
        self.assertNotIn("Final", chained["_title_candidates"])
        self.assertNotIn("_rule_query_aliases", guessed)

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

    def test_search_round_robin_reaches_alternate_before_fallback_language(self):
        calls = []
        trace = {}

        result = search_candidates(
            "movie",
            "Incontrolable",
            {
                "title": "Incontrolable",
                "year": 2024,
                "_title_candidates": ["Incontrolable", "Unstoppable"],
            },
            "es-ES",
            "ES",
            {
                "fallback_language": "en-US",
                "use_fallback_language": True,
                "query_variants": {
                    "with_year": True,
                    "without_year": False,
                    "use_parser_candidates": True,
                    "use_guessit": False,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {"max_searches": 2},
                "acceptance": {
                    "early_stop_score": 999,
                    "early_stop_margin": 999,
                },
            },
            lambda _endpoint, params: calls.append(dict(params)) or {"results": []},
            lambda *_args: self.fail("No debe pedir detalle sin candidatos"),
            lambda candidates, *_args: list(candidates),
            selection_trace=trace,
        )

        self.assertEqual(result, [])
        self.assertEqual(
            [(item["query"], item["language"], item["year"]) for item in calls],
            [
                ("Incontrolable", "es-ES", 2024),
                ("Unstoppable", "es-ES", 2024),
            ],
        )
        self.assertEqual(
            trace["search_strategy"]["phase_calls"],
            {"primary": 1, "composite": 0, "alternate": 1},
        )
        self.assertIsNone(trace["search_strategy"]["early_stop_phase"])
        self.assertIsNone(trace["search_strategy"]["early_stop_reason"])

    def test_round_robin_reaches_alternate_with_seven_derived_before_cap(self):
        calls = []
        trace = {}
        evidence = [
            {
                "value": "Primary title",
                "role": "primary",
                "source": "parser",
                "group_id": "parser:0",
            },
            *[
                {
                    "value": f"Derived title {index}",
                    "role": "derived_primary",
                    "source": "series_prefix",
                    "group_id": f"series:{index}",
                }
                for index in range(1, 8)
            ],
            {
                "value": "Atomic alternate",
                "role": "alternate",
                "source": "parentheses",
                "group_id": "parser:0",
            },
        ]

        result = search_candidates(
            "movie",
            "Primary title",
            {
                "title": "Primary title",
                "year": 2024,
                "_title_evidence": evidence,
            },
            "es-ES",
            "ES",
            {
                "use_fallback_language": False,
                "query_variants": {
                    "with_year": True,
                    "without_year": False,
                    "use_parser_candidates": True,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {"max_searches": 8},
                "acceptance": {
                    "early_stop_score": 999,
                    "early_stop_margin": 999,
                },
            },
            lambda _endpoint, params: calls.append(dict(params)) or {"results": []},
            lambda *_args: self.fail("No debe pedir detalle sin candidatos"),
            lambda candidates, *_args: list(candidates),
            selection_trace=trace,
        )

        queried = [item["query"] for item in calls]
        self.assertEqual(result, [])
        self.assertEqual(len(queried), 8)
        self.assertIn("Atomic alternate", queried)
        self.assertLess(queried.index("Atomic alternate"), 8)
        self.assertEqual(trace["search_strategy"]["phase_calls"]["alternate"], 1)

    def test_structured_roles_override_flat_candidates_and_map_to_search_phases(self):
        grouped = _build_query_inputs(
            "Primary",
            {
                "title": "Primary",
                "_title_candidates": ["Flat trap"],
                "_title_evidence": [
                    {
                        "value": "Configured",
                        "role": "configured_primary",
                        "source": "configured_alias",
                        "group_id": "configured:0",
                    },
                    {
                        "value": "Primary",
                        "role": "primary",
                        "source": "parser",
                        "group_id": "parser:0",
                    },
                    {
                        "value": "Derived",
                        "role": "derived_primary",
                        "source": "series_prefix",
                        "group_id": "series:0",
                    },
                    {
                        "value": "Primary Alternate",
                        "role": "composite",
                        "source": "parentheses",
                        "group_id": "parser:0",
                    },
                    {
                        "value": "Alternate",
                        "role": "alternate",
                        "source": "parentheses",
                        "group_id": "parser:0",
                    },
                ],
            },
            {
                "use_parser_candidates": True,
                "use_tail_cleanup": False,
                "use_spanish_correction": False,
            },
        )

        self.assertEqual(
            [(item.value, item.source) for item in grouped["primary"]],
            [
                ("Configured", "configured"),
                ("Primary", "primary"),
                ("Derived", "primary"),
            ],
        )
        self.assertEqual(
            [(item.value, item.source) for item in grouped["composite"]],
            [("Primary Alternate", "composite")],
        )
        self.assertEqual(
            [(item.value, item.source) for item in grouped["alternate"]],
            [("Alternate", "alternate")],
        )
        self.assertNotIn(
            "Flat trap",
            [item.value for phase in grouped.values() for item in phase],
        )

    def test_multititle_early_stop_requires_atomic_corroboration(self):
        guessed = self._evidence_guess(
            "Incontrolable",
            "I Swear",
            year=2025,
        )
        candidate = ResolverCandidate(
            1317149,
            "movie",
            "Incontrolable",
            "I Swear",
            2025,
            ["Incontrolable", "I Swear"],
            title_match_level="alternate",
        )
        payload = {"_search_sources": ["alternate"]}

        self.assertFalse(_has_early_stop_evidence(candidate, payload, guessed))
        candidate.title_match_level = "primary"
        self.assertFalse(_has_early_stop_evidence(candidate, payload, guessed))
        candidate.title_match_level = "corroborated"
        self.assertTrue(_has_early_stop_evidence(candidate, payload, guessed))
        payload["_search_sources"] = ["primary", "alternate"]
        self.assertTrue(_has_early_stop_evidence(candidate, payload, guessed))

    def test_search_limits_are_hard_capped_and_reported(self):
        calls = []
        trace = {}
        guessed = {
            "title": "Principal",
            "year": 2024,
            "_title_candidates": [
                "Principal",
                "Alterno Uno",
                "Alterno Dos",
                "Alterno Tres",
            ],
        }

        search_candidates(
            "movie",
            "Principal",
            guessed,
            "es-ES",
            "ES",
            {
                "fallback_language": "en-US",
                "use_fallback_language": True,
                "query_variants": {
                    "with_year": True,
                    "without_year": True,
                    "use_parser_candidates": True,
                    "use_guessit": False,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {
                    "max_searches": 999,
                    "detail_candidates": 999,
                },
                "acceptance": {
                    "early_stop_score": 999,
                    "early_stop_margin": 999,
                },
            },
            lambda _endpoint, params: calls.append(dict(params)) or {"results": []},
            lambda *_args: self.fail("No debe pedir detalle sin candidatos"),
            lambda candidates, *_args: list(candidates),
            selection_trace=trace,
        )

        self.assertEqual(len(calls), 8)
        self.assertEqual(trace["search_strategy"]["max_searches"], 8)
        self.assertEqual(trace["search_strategy"]["executed_searches"], 8)
        self.assertEqual(trace["search_strategy"]["detail_limit"], 3)

    def test_failed_detail_marks_selection_uncertain_and_incomplete(self):
        trace = {}
        result = search_candidates(
            "movie",
            "Objetivo",
            {"title": "Objetivo", "year": 2024},
            "es-ES",
            "ES",
            {
                "use_fallback_language": False,
                "query_variants": {
                    "with_year": True,
                    "without_year": False,
                    "use_parser_candidates": True,
                    "use_guessit": False,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {"max_searches": 1},
            },
            lambda _endpoint, _params: {
                "results": [self._movie_payload(99, "Objetivo", 2024)]
            },
            lambda *_args: (_ for _ in ()).throw(
                ResolverUnavailable("Detalle no disponible")
            ),
            lambda candidates, *_args: list(candidates),
            selection_trace=trace,
        )

        self.assertEqual([item.tmdb_id for item in result], [99])
        self.assertTrue(trace["selection_uncertain"])
        self.assertTrue(trace["search_strategy"]["detail_incomplete"])

    def test_early_detail_evicted_from_selection_shares_global_budget_without_retry(self):
        trace = {}
        detail_calls = []
        guessed = {
            "title": "Primary",
            "_title_evidence": [
                {
                    "value": "Primary",
                    "role": "primary",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
                {
                    "value": "Primary Alternate",
                    "role": "composite",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
                {
                    "value": "Alternate",
                    "role": "alternate",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
            ],
        }

        def get_payload(_endpoint, params):
            if params["query"] == "Primary":
                return {"results": [self._movie_payload(1, "Alternate", None)]}
            return {
                "results": [
                    self._movie_payload(tmdb_id, "Primary Alternate", None)
                    for tmdb_id in (2, 3, 4)
                ]
            }

        def ranker(candidates, *_args):
            scores = {1: 10, 2: 130, 3: 120, 4: 110}
            if len(candidates) == 1:
                scores[1] = 100
            for candidate in candidates:
                candidate.score = scores[candidate.tmdb_id]
                candidate.title_match_level = (
                    "alternate" if candidate.tmdb_id == 1 else "none"
                )
                candidate.title_identity_exact_roles = (
                    ["alternate"] if candidate.tmdb_id == 1 else []
                )
                candidate.title_matches = (
                    [
                        {
                            "role": "alternate",
                            "identity_exact": True,
                            "group_id": "parser:0",
                        }
                    ]
                    if candidate.tmdb_id == 1
                    else []
                )
            return sorted(candidates, key=lambda item: -item.score)

        def details(media_type, tmdb_id, _language):
            detail_calls.append(tmdb_id)
            title = "Alternate" if tmdb_id == 1 else "Primary Alternate"
            return ResolverCandidate(
                tmdb_id,
                media_type,
                title,
                title,
                None,
                [title],
            )

        result = search_candidates(
            "movie",
            "Primary",
            guessed,
            "es-ES",
            "ES",
            {
                "use_fallback_language": False,
                "query_variants": {
                    "with_year": False,
                    "without_year": True,
                    "use_parser_candidates": True,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {
                    "max_searches": 2,
                    "initial_candidates": 3,
                    "detail_candidates": 3,
                },
                "acceptance": {
                    "min_score": 75,
                    "min_margin": 12,
                    "early_stop_score": 75,
                    "early_stop_margin": 12,
                },
            },
            get_payload,
            details,
            ranker,
            selection_trace=trace,
        )

        strategy = trace["search_strategy"]
        self.assertEqual(detail_calls, [1, 2, 3])
        self.assertEqual(len(detail_calls), len(set(detail_calls)))
        self.assertEqual(strategy["detail_requests"], 3)
        self.assertTrue(strategy["early_detail_attempted"])
        self.assertFalse(strategy["early_detail_reused"])
        self.assertTrue(strategy["detail_incomplete"])
        self.assertTrue(trace["selection_uncertain"])
        self.assertFalse(trace["selection_uncertainty_alternate_only"])
        self.assertEqual([item.tmdb_id for item in result], [2, 3])

        eligibility = _select_title_eligible_candidates(
            result,
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=trace["selection_uncertain"],
            selection_uncertainty_alternate_only=trace[
                "selection_uncertainty_alternate_only"
            ],
            detail_incomplete=strategy["detail_incomplete"],
        )
        self.assertEqual(eligibility["reason_code"], "title_selection_uncertain")
        self.assertEqual(eligibility["eligible"], [])

    def test_exact_alternate_from_another_group_does_not_spend_early_detail(self):
        trace = {}
        detail_calls = []
        guessed = {
            "title": "Primary",
            "_title_evidence": [
                {
                    "value": "Primary",
                    "role": "primary",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
                {
                    "value": "Expected Alternate",
                    "role": "alternate",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
                {
                    "value": "Wrong Alternate",
                    "role": "alternate",
                    "source": "legacy",
                    "group_id": "other:0",
                },
            ],
        }

        def get_payload(_endpoint, params):
            if params["query"] == "Primary":
                return {
                    "results": [self._movie_payload(1, "Wrong Alternate", None)]
                }
            return {
                "results": [self._movie_payload(2, "Expected Alternate", None)]
            }

        def ranker(candidates, *_args):
            for candidate in candidates:
                candidate.score = 100 if candidate.tmdb_id == 1 else 120
                candidate.title_match_level = "alternate" if candidate.tmdb_id == 1 else "none"
                candidate.title_identity_exact_roles = (
                    ["alternate"] if candidate.tmdb_id == 1 else []
                )
                candidate.title_matches = (
                    [
                        {
                            "role": "alternate",
                            "identity_exact": True,
                            "group_id": "other:0",
                        }
                    ]
                    if candidate.tmdb_id == 1
                    else []
                )
            return sorted(candidates, key=lambda item: -item.score)

        def details(media_type, tmdb_id, _language):
            detail_calls.append(tmdb_id)
            return ResolverCandidate(
                tmdb_id,
                media_type,
                "Expected Alternate",
                "Expected Alternate",
                None,
                ["Expected Alternate"],
            )

        result = search_candidates(
            "movie",
            "Primary",
            guessed,
            "es-ES",
            "ES",
            {
                "use_fallback_language": False,
                "query_variants": {
                    "with_year": False,
                    "without_year": True,
                    "use_parser_candidates": True,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {
                    "max_searches": 2,
                    "initial_candidates": 1,
                    "detail_candidates": 1,
                },
                "acceptance": {
                    "min_score": 75,
                    "min_margin": 12,
                    "early_stop_score": 75,
                    "early_stop_margin": 12,
                },
            },
            get_payload,
            details,
            ranker,
            selection_trace=trace,
        )

        strategy = trace["search_strategy"]
        self.assertFalse(strategy["early_detail_attempted"])
        self.assertFalse(strategy["early_detail_reused"])
        self.assertEqual(strategy["detail_requests"], 1)
        self.assertEqual(detail_calls, [1])
        self.assertEqual([item.tmdb_id for item in result], [1])

    def test_progressive_detail_probes_cross_candidate_below_primary_decoy(self):
        trace = {}
        detail_calls = []
        search_calls = []
        guessed = {
            "title": "Primary",
            "_title_evidence": [
                {
                    "value": "Primary",
                    "role": "primary",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
                {
                    "value": "Primary Alternate",
                    "role": "composite",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
                {
                    "value": "Alternate",
                    "role": "alternate",
                    "source": "parentheses",
                    "group_id": "parser:0",
                },
            ],
        }

        def get_payload(_endpoint, params):
            search_calls.append(params["query"])
            if params["query"] == "Primary":
                return {
                    "results": [
                        self._movie_payload(1, "Primary", None),
                        self._movie_payload(2, "Alternate", None),
                    ]
                }
            return {"results": [self._movie_payload(2, "Alternate", None)]}

        def ranker(candidates, *_args):
            for candidate in candidates:
                if candidate.tmdb_id == 1:
                    candidate.score = 100
                    candidate.title_match_level = "primary"
                    candidate.title_identity_exact_roles = ["primary"]
                    candidate.title_matches = [
                        {
                            "role": "primary",
                            "identity_exact": True,
                            "group_id": "parser:0",
                        }
                    ]
                    continue
                detailed = "Primary" in candidate.aliases
                hits = int(candidate.search_provenance.get("hits") or 0)
                candidate.score = 130 if detailed and hits >= 2 else 100 if detailed else 80
                candidate.title_match_level = "corroborated" if detailed else "alternate"
                candidate.title_identity_exact_roles = (
                    ["alternate", "primary"] if detailed else ["alternate"]
                )
                candidate.title_matches = [
                    {
                        "role": "alternate",
                        "identity_exact": True,
                        "group_id": "parser:0",
                    },
                    *(
                        [
                            {
                                "role": "primary",
                                "identity_exact": True,
                                "group_id": "parser:0",
                            }
                        ]
                        if detailed
                        else []
                    ),
                ]
            return sorted(candidates, key=lambda item: (-item.score, item.tmdb_id))

        def details(media_type, tmdb_id, _language):
            detail_calls.append(tmdb_id)
            return ResolverCandidate(
                tmdb_id,
                media_type,
                "Primary",
                "Alternate",
                None,
                ["Primary", "Alternate"],
            )

        result = search_candidates(
            "movie",
            "Primary",
            guessed,
            "es-ES",
            "ES",
            {
                "use_fallback_language": False,
                "query_variants": {
                    "with_year": False,
                    "without_year": True,
                    "use_parser_candidates": True,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {
                    "max_searches": 2,
                    "initial_candidates": 1,
                    "detail_candidates": 3,
                },
                "acceptance": {
                    "min_score": 75,
                    "min_margin": 12,
                    "early_stop_score": 75,
                    "early_stop_margin": 12,
                },
            },
            get_payload,
            details,
            ranker,
            selection_trace=trace,
        )

        strategy = trace["search_strategy"]
        self.assertEqual(search_calls, ["Primary", "Primary Alternate"])
        self.assertEqual(detail_calls, [2, 1])
        self.assertEqual(len(detail_calls), len(set(detail_calls)))
        self.assertTrue(strategy["early_detail_attempted"])
        self.assertTrue(strategy["early_detail_reused"])
        self.assertEqual(strategy["detail_requests"], 2)
        self.assertEqual(strategy["early_stop_reason"], "all_atomic_titles_confirmed")
        self.assertEqual([item.tmdb_id for item in result], [2, 1])
        eligibility = _select_title_eligible_candidates(
            result,
            guessed=guessed,
            media_type="movie",
            source="search",
            title_matching={},
            selection_uncertain=trace["selection_uncertain"],
            selection_uncertainty_alternate_only=trace[
                "selection_uncertainty_alternate_only"
            ],
        )
        self.assertEqual([item.tmdb_id for item in eligibility["eligible"]], [2])

    def test_tied_cross_candidates_do_not_trigger_progressive_detail(self):
        trace = {}
        detail_calls = []
        guessed = self._evidence_guess("Primary", "Alternate")

        def ranker(candidates, *_args):
            for candidate in candidates:
                candidate.score = 80
                candidate.title_match_level = "alternate"
                candidate.title_identity_exact_roles = ["alternate"]
                candidate.title_matches = [
                    {
                        "role": "alternate",
                        "identity_exact": True,
                        "group_id": "parser:0",
                    }
                ]
            return sorted(candidates, key=lambda item: item.tmdb_id)

        def details(media_type, tmdb_id, _language):
            detail_calls.append(tmdb_id)
            return ResolverCandidate(
                tmdb_id,
                media_type,
                "Alternate",
                "Alternate",
                None,
                ["Alternate"],
            )

        result = search_candidates(
            "movie",
            "Primary",
            guessed,
            "es-ES",
            "ES",
            {
                "use_fallback_language": False,
                "query_variants": {
                    "with_year": False,
                    "without_year": True,
                    "use_parser_candidates": True,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {
                    "max_searches": 1,
                    "initial_candidates": 1,
                    "detail_candidates": 1,
                },
                "acceptance": {
                    "min_score": 75,
                    "min_margin": 12,
                    "early_stop_score": 75,
                    "early_stop_margin": 12,
                },
            },
            lambda _endpoint, _params: {
                "results": [
                    self._movie_payload(1, "Alternate", None),
                    self._movie_payload(2, "Alternate", None),
                ]
            },
            details,
            ranker,
            selection_trace=trace,
        )

        strategy = trace["search_strategy"]
        self.assertFalse(strategy["early_detail_attempted"])
        self.assertFalse(strategy["early_detail_reused"])
        self.assertEqual(strategy["detail_requests"], 1)
        self.assertEqual(detail_calls, [1])
        self.assertEqual([item.tmdb_id for item in result], [1])

    def test_detail_reservation_keeps_primary_rival_and_reports_provenance(self):
        trace = {}
        detail_calls = []

        def get_payload(_endpoint, params):
            if params["query"] == "Principal":
                return {"results": [self._movie_payload(1, "Principal", 2020)]}
            return {
                "results": [
                    self._movie_payload(2, "Alternate", 2021),
                    self._movie_payload(3, "Alternate", 2022),
                ]
            }

        def ranker(candidates, *_args):
            scores = {1: 80, 2: 100, 3: 90}
            for candidate in candidates:
                candidate.score = scores[candidate.tmdb_id]
            return sorted(candidates, key=lambda item: item.score, reverse=True)

        def details(media_type, tmdb_id, _language):
            detail_calls.append(tmdb_id)
            title = "Principal" if tmdb_id == 1 else "Alternate"
            return ResolverCandidate(
                tmdb_id,
                media_type,
                title,
                title,
                {1: 2020, 2: 2021, 3: 2022}[tmdb_id],
                [title],
            )

        search_candidates(
            "movie",
            "Principal",
            {
                "title": "Principal",
                "_title_candidates": ["Principal", "Alternate"],
            },
            "es-ES",
            "ES",
            {
                "use_fallback_language": False,
                "query_variants": {
                    "with_year": True,
                    "without_year": True,
                    "use_parser_candidates": True,
                    "use_guessit": False,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {
                    "max_searches": 2,
                    "initial_candidates": 2,
                    "detail_candidates": 2,
                },
                "acceptance": {
                    "min_score": 75,
                    "min_margin": 12,
                    "early_stop_score": 999,
                    "early_stop_margin": 999,
                },
            },
            get_payload,
            details,
            ranker,
            selection_trace=trace,
        )

        self.assertEqual(detail_calls, [2, 1])
        provenance = {item["tmdb_id"]: item for item in trace["candidate_provenance"]}
        self.assertEqual(provenance[1]["exact_sources"], ["primary"])
        self.assertEqual(provenance[2]["exact_sources"], ["alternate"])
        self.assertTrue(provenance[1]["selected_for_detail"])
        self.assertTrue(provenance[2]["selected_for_detail"])
        self.assertFalse(provenance[3]["selected_for_detail"])
        self.assertEqual(
            trace["raw_exact_candidate_counts"],
            {
                "total": 3,
                "by_phase": {"primary": 1, "composite": 0, "alternate": 2},
                "by_role": {
                    "primary": 1,
                    "configured": 0,
                    "composite": 0,
                    "alternate": 2,
                    "legacy": 0,
                },
            },
        )
        self.assertTrue(trace["selection_uncertain"])

    def test_detail_reservation_never_evicts_two_strong_candidates(self):
        trace = {}
        detail_calls = []
        payloads = {
            "Configured": [self._movie_payload(1, "Configured", 2024)],
            "Primary": [self._movie_payload(2, "Primary", 2024)],
            "Alternate": [
                self._movie_payload(3, "Alternate", 2024),
                self._movie_payload(4, "Alternate", 2023),
            ],
        }

        def ranker(candidates, *_args):
            scores = {1: 80, 2: 79, 3: 100, 4: 99}
            for candidate in candidates:
                candidate.score = scores[candidate.tmdb_id]
            return sorted(candidates, key=lambda item: (-item.score, item.tmdb_id))

        def details(media_type, tmdb_id, _language):
            detail_calls.append(tmdb_id)
            title = {1: "Configured", 2: "Primary", 3: "Alternate", 4: "Alternate"}[
                tmdb_id
            ]
            return ResolverCandidate(tmdb_id, media_type, title, title, 2024, [title])

        search_candidates(
            "movie",
            "Primary",
            {
                "title": "Primary",
                "_title_candidates": ["Flat trap"],
                "_title_evidence": [
                    {
                        "value": "Configured",
                        "role": "configured_primary",
                        "source": "configured_alias",
                        "group_id": "configured:0",
                    },
                    {
                        "value": "Primary",
                        "role": "primary",
                        "source": "parser",
                        "group_id": "parser:0",
                    },
                    {
                        "value": "Alternate",
                        "role": "alternate",
                        "source": "parentheses",
                        "group_id": "parser:0",
                    },
                ],
            },
            "es-ES",
            "ES",
            {
                "use_fallback_language": False,
                "query_variants": {
                    "with_year": True,
                    "without_year": True,
                    "use_parser_candidates": True,
                    "use_guessit": False,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {"max_searches": 3, "detail_candidates": 3},
                "acceptance": {
                    "min_score": 75,
                    "min_margin": 12,
                    "early_stop_score": 999,
                    "early_stop_margin": 999,
                },
            },
            lambda _endpoint, params: {"results": payloads[params["query"]]},
            details,
            ranker,
            selection_trace=trace,
        )

        self.assertEqual(detail_calls, [3, 1, 2])
        provenance = {item["tmdb_id"]: item for item in trace["candidate_provenance"]}
        self.assertTrue(provenance[1]["selected_for_detail"])
        self.assertTrue(provenance[2]["selected_for_detail"])
        self.assertTrue(provenance[3]["selected_for_detail"])
        self.assertFalse(provenance[4]["selected_for_detail"])

    def test_more_than_three_strong_candidates_marks_selection_uncertain(self):
        trace = {}
        detail_calls = []
        query_to_id = {
            "Configured": 1,
            "Primary": 2,
            "Derived": 3,
            "Primary Alternate": 4,
        }

        def ranker(candidates, *_args):
            for candidate in candidates:
                candidate.score = 100 - candidate.tmdb_id
            return sorted(candidates, key=lambda item: (-item.score, item.tmdb_id))

        def details(media_type, tmdb_id, _language):
            detail_calls.append(tmdb_id)
            title = next(
                query for query, candidate_id in query_to_id.items() if candidate_id == tmdb_id
            )
            return ResolverCandidate(tmdb_id, media_type, title, title, 2024, [title])

        search_candidates(
            "movie",
            "Primary",
            {
                "title": "Primary",
                "_title_evidence": [
                    {
                        "value": "Configured",
                        "role": "configured_primary",
                        "source": "configured_alias",
                        "group_id": "configured:0",
                    },
                    {
                        "value": "Primary",
                        "role": "primary",
                        "source": "parser",
                        "group_id": "parser:0",
                    },
                    {
                        "value": "Derived",
                        "role": "derived_primary",
                        "source": "series_prefix",
                        "group_id": "series:0",
                    },
                    {
                        "value": "Primary Alternate",
                        "role": "composite",
                        "source": "parentheses",
                        "group_id": "parser:0",
                    },
                ],
            },
            "es-ES",
            "ES",
            {
                "use_fallback_language": False,
                "query_variants": {
                    "with_year": True,
                    "without_year": True,
                    "use_parser_candidates": True,
                    "use_guessit": False,
                    "use_tail_cleanup": False,
                    "use_spanish_correction": False,
                },
                "search_limits": {"max_searches": 4, "detail_candidates": 3},
                "acceptance": {
                    "min_score": 75,
                    "min_margin": 12,
                    "early_stop_score": 999,
                    "early_stop_margin": 999,
                },
            },
            lambda _endpoint, params: {
                "results": [
                    self._movie_payload(
                        query_to_id[params["query"]],
                        params["query"],
                        2024,
                    )
                ]
            },
            details,
            ranker,
            selection_trace=trace,
        )

        self.assertEqual(len(detail_calls), 3)
        self.assertTrue(trace["selection_uncertain"])
        self.assertFalse(trace["selection_uncertainty_alternate_only"])
        self.assertEqual(
            sum(
                item["selected_for_detail"]
                for item in trace["candidate_provenance"]
            ),
            3,
        )

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
