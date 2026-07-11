import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module


INFO_HASH_A = "a" * 40
INFO_HASH_B = "b" * 40


def result(
    tracker: str,
    tracker_id: str,
    *,
    info_hash: str = "",
    seeders: int = 1,
    peers: int = 0,
    link: str = "",
) -> dict:
    return app_module.normalize_result(
        {
            "title": "The.Batman.2022.UHD.BluRay.2160p.TrueHD.Atmos.7.1.DV.HEVC.REMUX-FraMeSToR",
            "size": "75751014400",
            "seeders": seeders,
            "peers": peers,
            "tracker": tracker,
            "tracker_id": tracker_id,
            "download_url": link or f"https://example.invalid/{tracker_id}.torrent",
            "info_hash": info_hash,
        }
    )


class SearchDeduplicationTests(unittest.TestCase):
    def test_torznab_parser_keeps_infohash(self) -> None:
        xml = f"""
        <rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item>
          <title>The Batman</title>
          <guid>magnet:?xt=urn:btih:{INFO_HASH_A}</guid>
          <link>magnet:?xt=urn:btih:{INFO_HASH_A}</link>
          <size>1000</size>
          <jackettindexer id="therarbg">TheRARBG</jackettindexer>
          <torznab:attr name="infohash" value="{INFO_HASH_A.upper()}" />
          <torznab:attr name="seeders" value="180" />
          <torznab:attr name="peers" value="437" />
        </item></channel></rss>
        """

        rows = app_module.parse_results(xml)

        self.assertEqual(rows[0]["info_hash"], INFO_HASH_A)

    def test_same_infohash_is_one_card_with_best_source_and_count(self) -> None:
        rows = [
            result("LimeTorrents", "limetorrents", info_hash=INFO_HASH_A, seeders=33, peers=47),
            result(
                "TheRARBG",
                "therarbg",
                info_hash=INFO_HASH_A,
                seeders=180,
                peers=437,
                link=f"magnet:?xt=urn:btih:{INFO_HASH_A}",
            ),
        ]

        with (
            patch.object(app_module, "cached_info_hashes", return_value={}),
            patch.object(app_module, "cached_identity_checks", return_value=set()),
        ):
            grouped = app_module.deduplicate_exact_results(rows)

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["tracker"], "TheRARBG")
        self.assertEqual(grouped[0]["source_count"], 2)
        self.assertEqual(len(grouped[0]["sources"]), 2)
        self.assertEqual(grouped[0]["id"], app_module.result_cache_id({"info_hash": INFO_HASH_A}))

    def test_same_title_with_different_hashes_stays_separate(self) -> None:
        rows = [
            result("LimeTorrents", "limetorrents", info_hash=INFO_HASH_A),
            result("TheRARBG", "therarbg", info_hash=INFO_HASH_B),
        ]

        with (
            patch.object(app_module, "cached_info_hashes", return_value={}),
            patch.object(app_module, "cached_identity_checks", return_value=set()),
        ):
            grouped = app_module.deduplicate_exact_results(rows)

        self.assertEqual(len(grouped), 2)
        self.assertTrue(all(item["source_count"] == 1 for item in grouped))

    def test_missing_hash_is_grouped_only_after_exact_resolution(self) -> None:
        rows = [
            result("LimeTorrents", "limetorrents"),
            result("TheRARBG", "therarbg", info_hash=INFO_HASH_A),
        ]

        with (
            patch.object(app_module, "cached_info_hashes", return_value={}),
            patch.object(app_module, "cached_identity_checks", return_value=set()),
            patch.object(app_module, "resolve_result_info_hash", return_value=INFO_HASH_A),
        ):
            grouped = app_module.deduplicate_exact_results(rows)

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["source_count"], 2)

    def test_failed_hash_resolution_never_hides_a_result(self) -> None:
        rows = [
            result("LimeTorrents", "limetorrents"),
            result("TheRARBG", "therarbg", info_hash=INFO_HASH_A),
        ]

        with (
            patch.object(app_module, "cached_info_hashes", return_value={}),
            patch.object(app_module, "cached_identity_checks", return_value=set()),
            patch.object(app_module, "resolve_result_info_hash", return_value=""),
        ):
            grouped = app_module.deduplicate_exact_results(rows)

        self.assertEqual(len(grouped), 2)

    def test_result_cache_preserves_group_sources(self) -> None:
        rows = [
            result("LimeTorrents", "limetorrents", info_hash=INFO_HASH_A, seeders=33),
            result("TheRARBG", "therarbg", info_hash=INFO_HASH_A, seeders=180),
        ]
        with (
            patch.object(app_module, "cached_info_hashes", return_value={}),
            patch.object(app_module, "cached_identity_checks", return_value=set()),
        ):
            grouped = app_module.deduplicate_exact_results(rows)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            cache_path = Path(temporary) / "result_cache.json"
            with patch.object(app_module, "RESULT_CACHE_PATH", cache_path):
                app_module.cache_results(grouped)
                cached = app_module.cached_result(grouped[0]["id"])

        self.assertIsNotNone(cached)
        self.assertEqual(cached["source_count"], 2)
        self.assertEqual(len(cached["sources"]), 2)

    def test_far_size_candidate_is_not_resolved_or_hidden(self) -> None:
        far = result("LimeTorrents", "limetorrents")
        far["size"] = "1000"
        rows = [far, result("TheRARBG", "therarbg", info_hash=INFO_HASH_A)]

        with (
            patch.object(app_module, "cached_info_hashes", return_value={}),
            patch.object(app_module, "cached_identity_checks", return_value=set()),
            patch.object(app_module, "resolve_result_info_hash") as resolver,
        ):
            grouped = app_module.deduplicate_exact_results(rows)

        resolver.assert_not_called()
        self.assertEqual(len(grouped), 2)


class SearchDeduplicationFrontendTests(unittest.TestCase):
    def test_card_uses_compact_tracker_plus_n_without_new_layout(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn("`${item.tracker} +${sourceCount - 1}`", script)
        self.assertIn("const sourceCount = Math.max(1, numberValue(item.source_count));", script)


if __name__ == "__main__":
    unittest.main()
