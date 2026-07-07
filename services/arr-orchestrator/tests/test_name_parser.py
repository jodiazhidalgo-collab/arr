import unittest

from arr_orchestrator.name_parser import decide_media, parse_release_name


class NameParserTests(unittest.TestCase):
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

    def test_movie_with_year(self):
        parsed = parse_release_name("Erase Una Vez En... Hollywood (2019).mkv")
        self.assertEqual(parsed.media_hint, "movies")
        self.assertEqual(parsed.year, 2019)
        self.assertEqual(parsed.display_title, "Erase Una Vez En Hollywood")

    def test_bilingual_title_candidates(self):
        parsed = parse_release_name("Red One (Código Traje Rojo) (2024) cast.mp4")
        self.assertEqual(parsed.media_hint, "movies")
        self.assertEqual(parsed.year, 2024)
        self.assertIn("Red One", parsed.title_candidates)
        self.assertIn("Código Traje Rojo", parsed.title_candidates)

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


if __name__ == "__main__":
    unittest.main()
