import json
import unittest

from arr_orchestrator.identity.defaults import factory_identity_rules
from arr_orchestrator.identity.parser_models import ParsedName, TitleEvidence
from arr_orchestrator.name_parser import (
    decide_media,
    factory_parser_rules,
    parse_release_name,
    parse_with_trace,
)


class NameParserTests(unittest.TestCase):
    def test_real_paths_use_only_the_final_component(self):
        samples = (
            "/downloads/releases/Movie.Name.2024.mkv",
            r"C:\Downloads\releases\Movie.Name.2024.mkv",
            r"\\server\share\Movie.Name.2024.mkv",
            "downloads/releases/Movie.Name.2024.mkv",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                parsed = parse_release_name(sample)
                self.assertEqual(parsed.display_title, "Movie Name")

    def test_internal_release_slashes_are_not_treated_as_paths(self):
        bilingual = parse_release_name("Titulo / Original.Title.2024.1080p.mkv")
        languages = parse_release_name("Titulo.CZ/SK/EN.2024.1080p.mkv")
        prefixed_languages = parse_release_name("Titulo/CZ/SK/EN.Movie.2024.mkv")
        mixed_path = parse_release_name(
            "downloads/releases/Titulo / Original.Title.2024.mkv"
        )

        self.assertEqual(bilingual.display_title, "Titulo")
        self.assertEqual(
            bilingual.title_candidates,
            ["Titulo", "Original Title", "Titulo / Original Title"],
        )
        self.assertEqual(languages.display_title, "Titulo CZ/SK/EN")
        self.assertEqual(prefixed_languages.display_title, "Titulo/CZ/SK/EN Movie")
        self.assertEqual(mixed_path.display_title, "Original Title")
        self.assertEqual(mixed_path.title_candidates, ["Original Title"])

    def test_escaped_apostrophe_is_not_treated_as_a_path_separator(self):
        samples = (
            r"Mr. Bean\'s Holiday (2007) BDRip 1080p multisub [mkvonly]",
            r"Mr. Bean\'s Holiday  (2007) DVDRip (dutch subs NL).avi",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                parsed = parse_release_name(sample)
                self.assertEqual(parsed.display_title, "Mr Bean's Holiday")
                self.assertEqual(parsed.year, 2007)
                self.assertEqual(parsed.media_hint, "movies")

    def test_s03_e53_is_tv(self):
        parsed = parse_release_name("La reina del flow S03 E53 (2026) NETFLIX.mkv")
        self.assertEqual(parsed.media_hint, "tv")
        self.assertEqual(parsed.season, 3)
        self.assertEqual(parsed.episodes, [53])

    def test_3x41_is_tv(self):
        parsed = parse_release_name("la reina del flow.3x41.1080.mkv")
        self.assertEqual(parsed.media_hint, "tv")
        self.assertEqual(parsed.season, 3)
        self.assertEqual(parsed.episodes, [41])

    def test_cap_3401(self):
        parsed = parse_release_name("Los Simpsons - Temporada 34 [Cap.3401]")
        self.assertEqual(parsed.media_hint, "tv")
        self.assertEqual(parsed.season, 34)
        self.assertEqual(parsed.episodes, [1])

    def test_cap_range_201_203(self):
        parsed = parse_release_name("Bluey - Temporada 2 [Cap.201_203]")
        self.assertEqual(parsed.media_hint, "tv")
        self.assertEqual(parsed.season, 2)
        self.assertEqual(parsed.episodes, [1, 2, 3])
        self.assertEqual(parsed.episode_range, (1, 3))

    def test_absolute_episode_without_season(self):
        parsed = parse_release_name("Lejos de Ti 1080p Capitulo 14.mp4")
        self.assertEqual(parsed.media_hint, "tv")
        self.assertIsNone(parsed.season)
        self.assertEqual(parsed.absolute_episode, 14)

    def test_t06_season_pack(self):
        parsed = parse_release_name("Los Simpson T06")
        self.assertEqual(parsed.media_hint, "tv")
        self.assertEqual(parsed.season_pack, 6)

    def test_supported_compact_tv_patterns(self):
        samples = (
            ("Serie.S01.1080p.mkv", 1, []),
            ("Serie.S01E02.1080p.mkv", 1, [2]),
            ("Serie.1x02.1080p.mkv", 1, [2]),
            ("Serie.01E09.1080p.mkv", 1, [9]),
            ("Serie.Temporada.2.1080p.mkv", 2, []),
            ("Serie.Season.2.1080p.mkv", 2, []),
            ("Serie.Temp.2.1080p.mkv", 2, []),
            ("Serie.Sezon.2.1080p.mkv", 2, []),
        )
        for name, season, episodes in samples:
            with self.subTest(name=name):
                parsed = parse_release_name(name)
                self.assertEqual(parsed.media_hint, "tv")
                self.assertEqual(parsed.season, season)
                self.assertEqual(parsed.episodes, episodes)

    def test_configurable_written_season_numbers(self):
        samples = (
            ("Serie Temporada uno 1080p.mkv", 1),
            ("Serie Season two 1080p.mkv", 2),
            ("Serie Temp tercera 1080p.mkv", 3),
            ("Serie Sezon fourth 1080p.mkv", 4),
        )
        for name, season in samples:
            with self.subTest(name=name):
                parsed = parse_release_name(name)
                self.assertEqual(parsed.media_hint, "tv")
                self.assertEqual(parsed.season, season)
                self.assertNotRegex(parsed.display_title, r"(?i)temporada|season|temp|sezon")

        custom = parse_release_name(
            "Serie Temp once.mkv",
            rules={"season_number_words": ["once | 11"]},
        )
        self.assertEqual(custom.season, 11)

        underscored = parse_release_name("Serie_Temporada_Uno.mkv")
        self.assertEqual(underscored.media_hint, "tv")
        self.assertEqual(underscored.season, 1)
        self.assertEqual(underscored.display_title, "Serie")

    def test_numeric_serie_and_miniseries_with_year_range_are_tv(self):
        numbered = parse_release_name("Spartacus House of Ashur 1 série 2025-2026.mkv")
        miniseries = parse_release_name(
            "Spartacus House of Ashur 2025-2026 Miniserial WEB-DL"
        )

        self.assertEqual(numbered.media_hint, "tv")
        self.assertEqual(numbered.season, 1)
        self.assertEqual(miniseries.media_hint, "tv")
        self.assertIsNone(miniseries.season)

    def test_movie_with_year(self):
        parsed = parse_release_name("Erase Una Vez En... Hollywood (2019).mkv")
        self.assertEqual(parsed.media_hint, "movies")
        self.assertEqual(parsed.year, 2019)
        self.assertEqual(parsed.display_title, "Erase Una Vez En Hollywood")

    def test_movie_without_year_accepts_video_extension_or_strong_marker(self):
        by_extension = parse_release_name("Una pelicula sin fecha.mkv")
        by_marker = parse_release_name("Una pelicula sin fecha WEB-DL x265")

        self.assertEqual(by_extension.media_hint, "movies")
        self.assertEqual(by_marker.media_hint, "movies")

    def test_movie_without_year_rejects_weak_or_non_video_evidence(self):
        weak_samples = (
            "Titulo sin fecha 1080p",
            "Titulo sin fecha 4K Spanish AAC",
            "Titulo sin fecha Castellano AC3",
        )
        for name in weak_samples:
            with self.subTest(name=name):
                self.assertEqual(parse_release_name(name).media_hint, "manual")

        blocked_samples = (
            "Mi Videojuego WEB-DL x265.mkv",
            "Grandes PC Games WEB-DL x265.mkv",
            "Coleccion de clasicos WEB-DL x265.mkv",
            "Saga completa WEB-DL x265.mkv",
        )
        for name in blocked_samples:
            with self.subTest(name=name):
                self.assertEqual(parse_release_name(name).media_hint, "manual")

        self.assertEqual(
            parse_release_name("4KUHDrip-Recuperado.jpg [4KUHDrip]").media_hint,
            "manual",
        )

    def test_common_video_sources_without_year_are_strong_and_clean(self):
        samples = (
            "Pelicula UHDmicro Spanish",
            "Pelicula 4KUHDremux Spanish",
            "Pelicula FullUHD 2160p Spanish",
            "Pelicula HDRip Ac3 Spanish",
            "Pelicula DVD-Screener Spanish",
            "Pelicula 3D SBS Spanish",
            "Pelicula (1080p mkv) Spanish",
        )
        for name in samples:
            with self.subTest(name=name):
                parsed = parse_release_name(name)
                self.assertEqual(parsed.media_hint, "movies")
                self.assertEqual(parsed.display_title, "Pelicula")

    def test_platform_markers_override_video_looking_game_releases(self):
        samples = (
            "Resident Evil 5 PCDVD DVD-Screener",
            "Resident Evil 4 PS2 DVD-Screener",
            "Resident Evil Xbox360 HDRip",
            "Resident Evil Wii BluRayRip",
        )
        for name in samples:
            with self.subTest(name=name):
                self.assertEqual(parse_release_name(name).media_hint, "manual")

    def test_game_words_inside_real_titles_are_not_global_vetoes(self):
        parsed = parse_release_name("Juego.de.tronos.S01E01.mkv")
        movie = parse_release_name("The.Game.1997.mkv")
        nintendo_tv = parse_release_name("Nintendo.S01E01.mkv")
        nintendo_movie = parse_release_name("Nintendo.2024.mkv")
        nintendo_game = parse_release_name("Nintendo.WEB-DL.x265")

        self.assertEqual(parsed.media_hint, "tv")
        self.assertEqual(movie.media_hint, "movies")
        self.assertEqual(nintendo_tv.media_hint, "tv")
        self.assertEqual(nintendo_movie.media_hint, "movies")
        self.assertEqual(nintendo_game.media_hint, "manual")

    def test_video_heuristic_does_not_conflict_with_explicit_tv(self):
        decision = decide_media("Serie sin patron WEB-DL x265.mkv", "tv")

        self.assertEqual(decision.media_type, "tv")
        self.assertIsNone(decision.block_reason)
        self.assertTrue(decision.allow_external_lookup)

    def test_year_alone_does_not_conflict_with_explicit_tv(self):
        decision = decide_media("The Office (2005)", "tv")

        self.assertEqual(decision.media_type, "tv")
        self.assertIsNone(decision.block_reason)
        self.assertTrue(decision.allow_external_lookup)

    def test_year_still_classifies_movies_without_explicit_category(self):
        parsed = parse_release_name("The Office (2005)")

        self.assertEqual(parsed.media_hint, "movies")

    def test_tv_year_range_skips_only_year_barriers(self):
        allowed = parse_release_name(
            "Serie.S01.2010-2020.1080p.mkv",
            rules={"year": {"multiple": "manual"}},
        )
        collection = parse_release_name("Serie.S01.Saga.2010-2020.1080p.mkv")
        unrelated_years = parse_release_name(
            "Serie.S01.1999.2024.mkv",
            rules={"year": {"multiple": "manual"}},
        )
        extra_year = parse_release_name(
            "Serie.S01.2010-2020.2024.mkv",
            rules={"year": {"multiple": "manual"}},
        )
        second_range = parse_release_name(
            "Serie.S01.2010-2020.2021-2022.mkv",
            rules={"year": {"multiple": "manual"}},
        )

        self.assertEqual(allowed.media_hint, "tv")
        self.assertEqual(allowed.season, 1)
        self.assertEqual(collection.media_hint, "manual")
        self.assertEqual(unrelated_years.media_hint, "manual")
        self.assertEqual(extra_year.media_hint, "manual")
        self.assertEqual(second_range.media_hint, "manual")

    def test_bilingual_title_candidates(self):
        parsed = parse_release_name("Red One (Código Traje Rojo) (2024) cast.mp4")
        self.assertEqual(parsed.media_hint, "movies")
        self.assertEqual(parsed.year, 2024)
        self.assertIn("Red One", parsed.title_candidates)
        self.assertIn("Código Traje Rojo", parsed.title_candidates)

    def test_title_evidence_model_serializes_without_shared_mutable_state(self):
        evidence = TitleEvidence(
            value="Incontrolable",
            role="primary",
            source="parentheses",
            group_id="parser:0",
        )
        first = ParsedName(
            raw="Incontrolable.mkv",
            cleaned="Incontrolable",
            display_title="Incontrolable",
            title_evidence=[evidence],
        )
        second = ParsedName(
            raw="Otra.mkv",
            cleaned="Otra",
            display_title="Otra",
        )

        self.assertEqual(
            evidence.to_dict(),
            {
                "value": "Incontrolable",
                "role": "primary",
                "source": "parentheses",
                "group_id": "parser:0",
            },
        )
        self.assertEqual(first.to_dict()["title_evidence"], [evidence.to_dict()])
        first.title_evidence.append(
            TitleEvidence("Unstoppable", "alternate", "parentheses", "parser:0")
        )
        self.assertEqual(second.title_evidence, [])

    def test_structured_title_evidence_preserves_flat_candidate_contract(self):
        samples = (
            (
                "Incontrolable (I Swear) (2025).mkv",
                ["Incontrolable", "I Swear", "Incontrolable (I Swear)"],
                [
                    ("Incontrolable", "primary", "parentheses", "parser:0"),
                    ("I Swear", "alternate", "parentheses", "parser:0"),
                    (
                        "Incontrolable (I Swear)",
                        "composite",
                        "parentheses",
                        "parser:0",
                    ),
                ],
            ),
            (
                "Historia / Global (2024).mkv",
                ["Historia", "Global", "Historia / Global"],
                [
                    ("Historia", "primary", "bilingual", "parser:0"),
                    ("Global", "alternate", "bilingual", "parser:0"),
                    ("Historia / Global", "composite", "bilingual", "parser:0"),
                ],
            ),
            (
                "Titulo.2024.mkv",
                ["Titulo"],
                [("Titulo", "primary", "parser", "parser:0")],
            ),
        )

        for raw_name, flat_candidates, structured in samples:
            with self.subTest(raw_name=raw_name):
                parsed = parse_release_name(raw_name)
                self.assertEqual(parsed.title_candidates, flat_candidates)
                self.assertEqual(
                    [
                        (item.value, item.role, item.source, item.group_id)
                        for item in parsed.title_evidence
                    ],
                    structured,
                )

    def test_editorial_parentheses_never_become_alternate_title_evidence(self):
        for raw_name, category, expected_title in (
            ("Titulo (Extended Edition) (2024).mkv", "movies", "Titulo"),
            ("Serie (Extended Edition) S01E01.mkv", "tv", "Serie"),
        ):
            with self.subTest(raw_name=raw_name):
                parsed = parse_release_name(raw_name, category)
                self.assertEqual(parsed.display_title, expected_title)
                self.assertEqual(parsed.title_candidates, [parsed.display_title])
                self.assertEqual(
                    [
                        (item.value, item.role, item.source, item.group_id)
                        for item in parsed.title_evidence
                    ],
                    [(parsed.display_title, "primary", "parser", "parser:0")],
                )

    def test_snatch_year_and_title(self):
        parsed = parse_release_name("Snatch.2000.2160p.AMZN.WEB-DL.x265")
        self.assertEqual(parsed.media_hint, "movies")
        self.assertEqual(parsed.year, 2000)
        self.assertEqual(parsed.display_title, "Snatch")

    def test_timestamp_suffix_removed(self):
        parsed = parse_release_name("Return to Silent Hill (2026) [4k 2160p][Esp]__1779242564")
        self.assertEqual(parsed.media_hint, "movies")
        self.assertEqual(parsed.year, 2026)
        self.assertNotIn("1779242564", parsed.cleaned)

    def test_duplicate_suffix_rule_finishes_and_removes_suffix(self):
        parsed = parse_release_name("Movie.Name.2024 (1).mkv")
        self.assertEqual(parsed.cleaned, "Movie Name 2024")

    def test_torrente_presidente_drops_release_tail(self):
        parsed = parse_release_name("Torrente.presidente.2026.Pm.TS.1O8Op.mkv")
        self.assertEqual(parsed.media_hint, "movies")
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.display_title, "Torrente presidente")
        self.assertEqual(parsed.guessit_input, "Torrente presidente 2026")

    def test_microhd_tail_does_not_pollute_title(self):
        parsed = parse_release_name("El Fuera de la Ley [MicroHD 1080p][Spanish].mkv")
        self.assertEqual(parsed.display_title, "El Fuera de la Ley")
        self.assertNotIn("MicroHD", parsed.display_title)

    def test_ocr_quality_token_does_not_pollute_title(self):
        parsed = parse_release_name("Anemona (2025) [4lk 2160p][Esp]")
        self.assertEqual(parsed.media_hint, "movies")
        self.assertEqual(parsed.year, 2025)
        self.assertEqual(parsed.display_title, "Anemona")

    def test_movie_saga_pack_is_manual(self):
        parsed = parse_release_name("Fast and Furious Saga 11 Movies 2001-2023")
        self.assertEqual(parsed.media_hint, "manual")

    def test_course_collection_is_manual(self):
        parsed = parse_release_name(
            "Lynda - Scott Simpson - Compleat Course Collection ( Linux, Ubuntu, Shell, CLI..) [AhLaN]"
        )
        self.assertEqual(parsed.media_hint, "manual")

    def test_media_decision_tv_strong_allows_lookup_but_does_not_block(self):
        decision = decide_media(
            "Satisfacion garantizada [HDTV 1080p][Cap.101]",
            "tv",
        )
        self.assertEqual(decision.media_type, "tv")
        self.assertEqual(decision.confidence, "high")
        self.assertTrue(decision.allow_external_lookup)
        self.assertIsNone(decision.block_reason)
        self.assertEqual(decision.episode_hint["season"], 1)
        self.assertEqual(decision.episode_hint["episodes"], [1])
        self.assertIn("parser_tv_signal", decision.reason_codes)

    def test_media_decision_detects_category_conflict(self):
        decision = decide_media("La Agencia [4k 2160p][Cap.201]", "movies")
        self.assertEqual(decision.media_type, "tv")
        self.assertEqual(decision.block_reason, "category_conflict")
        self.assertFalse(decision.allow_external_lookup)

    def test_full_identity_config_preserves_default_parser_behavior(self):
        rules = factory_identity_rules()
        parsed = parse_release_name("la reina del flow.s03e53.1080p.mkv", rules=rules)
        self.assertEqual(parsed.media_hint, "tv")
        self.assertEqual(parsed.display_title, "la reina del flow")
        self.assertEqual(parsed.season, 3)
        self.assertEqual(parsed.episodes, [53])

    def test_parser_accepts_direct_editable_rules(self):
        parsed = parse_release_name(
            "Mi.Pelicula.MarcaEditable.2024.video",
            rules={
                "extensions": [".video"],
                "site_words": ["MarcaEditable"],
            },
        )
        self.assertEqual(parsed.cleaned, "Mi Pelicula 2024")
        self.assertEqual(parsed.display_title, "Mi Pelicula")
        self.assertEqual(parsed.year, 2024)

    def test_domain_tlds_feed_the_editable_domain_pattern(self):
        parsed = parse_release_name(
            "tracker.local-Mi.Pelicula.2024.mkv",
            rules={"domain_tlds": ["local"]},
        )
        self.assertEqual(parsed.cleaned, "Mi Pelicula 2024")
        self.assertEqual(parsed.display_title, "Mi Pelicula")

    def test_empty_domain_tlds_do_not_activate_a_hidden_fallback(self):
        parsed = parse_release_name(
            "tracker.com-Mi.Pelicula.2024.mkv",
            rules={"domain_tlds": []},
        )

        self.assertIn("tracker com", parsed.cleaned)
        self.assertIn("tracker com", parsed.display_title)

    def test_parser_accepts_nested_config_and_custom_pattern(self):
        parsed = parse_release_name(
            "Mi Serie TEMP2 CAP7.mkv",
            config={
                "parser": {
                    "patterns": {
                        "series_sxe": r"\bTEMP(\d{1,2})\s+CAP\s*(\d{1,3})\b",
                    }
                }
            },
        )
        self.assertEqual(parsed.media_hint, "tv")
        self.assertEqual(parsed.season, 2)
        self.assertEqual(parsed.episodes, [7])

    def test_editable_manual_exact_name_forces_manual(self):
        parsed = parse_release_name(
            "Proyecto.Secreto.mkv",
            "movies",
            rules={"manual_exact_names": ["proyecto secreto"]},
        )
        self.assertEqual(parsed.media_hint, "manual")
        self.assertEqual(parsed.confidence, "low")

    def test_explicit_rules_override_config(self):
        parsed = parse_release_name(
            "Pelicula Regla Config 2024.mkv",
            config={"parser": {"site_words": ["Config"]}},
            rules={"site_words": ["Regla"]},
        )
        self.assertEqual(parsed.display_title, "Pelicula Config")

    def test_compact_web_cleanup_is_visible_and_editable(self):
        default = parse_release_name("Mi.Pelicula.WEBRip1080p.mkv")
        changed = parse_release_name(
            "Mi.Pelicula.WEBRip1080p.mkv",
            rules={"patterns": {"compact_web": r"(?!)"}},
        )

        self.assertEqual(default.display_title, "Mi Pelicula")
        self.assertEqual(changed.display_title, "Mi Pelicula WEBRip1080p")

    def test_legacy_576p_and_480p_tokens_are_still_removed(self):
        for token in ("576", "576p", "480", "480p"):
            with self.subTest(token=token):
                parsed = parse_release_name(f"Mi.Pelicula.{token}.mkv", "movies")
                self.assertEqual(parsed.display_title, "Mi Pelicula")

    def test_multiple_year_manual_option_really_blocks_lookup(self):
        decision = decide_media(
            "Saga.Parte.1999.2024.mkv",
            "",
            rules={"year": {"multiple": "manual"}},
        )

        self.assertEqual(decision.media_type, "manual")
        self.assertFalse(decision.allow_external_lookup)
        self.assertEqual(decision.block_reason, "manual_or_ambiguous")

    def test_parse_with_trace_is_serializable_and_complete(self):
        payload = parse_with_trace("Show.Name.S01E02.2024.1080p.mkv", "tv")
        self.assertEqual(payload["original"], "Show.Name.S01E02.2024.1080p.mkv")
        self.assertEqual(payload["cleaned"], "Show Name S01E02 2024 1080p")
        self.assertEqual(payload["title"], "Show Name")
        self.assertEqual(payload["candidates"], ["Show Name"])
        self.assertEqual(
            payload["title_evidence"],
            [
                {
                    "value": "Show Name",
                    "role": "primary",
                    "source": "parser",
                    "group_id": "parser:0",
                }
            ],
        )
        self.assertEqual(payload["year"], 2024)
        self.assertEqual(payload["category"], "tv")
        self.assertEqual(payload["confidence"], "high")
        self.assertEqual(payload["tv"]["season"], 1)
        self.assertEqual(payload["tv"]["episodes"], [2])
        self.assertEqual(payload["guessit"], "Show Name 2024 S01E02")
        self.assertTrue(payload["steps"])
        self.assertTrue(all(set(step) == {"rule", "before", "after"} for step in payload["steps"]))
        json.dumps(payload, ensure_ascii=False)

    def test_parse_with_trace_has_no_shared_mutable_state(self):
        first = parse_with_trace("One.2020.mkv")
        first["steps"].append({"rule": "external", "before": "", "after": ""})
        second = parse_with_trace("Two.2021.mkv")
        self.assertNotIn("external", [step["rule"] for step in second["steps"]])

    def test_factory_parser_rules_returns_independent_copies(self):
        first = factory_parser_rules()
        first["extensions"].append(".custom")
        second = factory_parser_rules()
        self.assertNotIn(".custom", second["extensions"])


if __name__ == "__main__":
    unittest.main()
