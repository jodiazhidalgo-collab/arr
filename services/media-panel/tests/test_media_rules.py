import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_panel import server


class _CapturedHandler:
    def __init__(self, path: str, payload=None) -> None:
        self.path = path
        self.payload = payload or {}
        self.response = None
        self.headers = {"Content-Length": "0"}

    def _json(self, status, payload) -> None:
        self.response = (status, payload)

    def _read_payload(self, strict=False):
        return self.payload

    def _content_length(self):
        return int(self.headers["Content-Length"])


class MediaRulesProxyTests(unittest.TestCase):
    @staticmethod
    def full_document(fingerprint="sha256:old"):
        rules = {
            "version": 1,
            "entrada": {"extensiones_video": [".mkv"]},
            "video": {"idioma_final": "spa"},
            "audio": {"bitrate_ac3": "640k"},
            "subtitulos": {"titulo_final": "Español"},
            "limpieza": {"crear_capitulos": True},
            "trailers": {"nombre_final": "trailer"},
        }
        return {
            "ok": True,
            "rules": rules,
            "active": rules.copy(),
            "defaults": rules.copy(),
            "fingerprint": fingerprint,
            "applied": True,
        }

    def test_get_and_post_use_media_worker_settings_endpoint(self) -> None:
        payload = {"rules": {}, "expected_fingerprint": "abc"}
        expected = {"ok": True, "fingerprint": "def", "applied": True}
        with patch.object(server, "_proxy_upstream_json", return_value=(200, expected)) as upstream:
            self.assertEqual(server._media_rules_payload(), (200, expected))
            upstream.assert_called_once_with(f"{server.WORKER_URL}/settings/rules", timeout=8)
        with patch.object(server, "_proxy_upstream_json", return_value=(200, expected)) as upstream:
            self.assertEqual(server._save_media_rules(payload), (200, expected))
            upstream.assert_called_once_with(
                f"{server.WORKER_URL}/settings/rules", payload, timeout=20
            )

    def test_api_preserves_worker_status_and_body(self) -> None:
        conflict = {"ok": False, "error": "fingerprint_conflict"}
        get_handler = _CapturedHandler("/api/rules")
        with patch.object(server, "_media_rules_payload", return_value=(503, conflict)):
            server.Handler.do_GET(get_handler)
        self.assertEqual(get_handler.response, (503, conflict))

        payload = {"rules": {}, "expected_fingerprint": "old"}
        post_handler = _CapturedHandler("/api/rules", payload)
        with patch.object(server, "_save_media_rules", return_value=(409, conflict)) as save:
            server.Handler.do_POST(post_handler)
        save.assert_called_once_with(payload)
        self.assertEqual(post_handler.response, (409, conflict))

    def test_movie_and_trailer_get_expose_only_their_bounded_blocks(self) -> None:
        document = self.full_document()
        for profile, blocks in (
            ("movies", server.MOVIE_RULE_BLOCKS),
            ("trailers", server.TRAILER_RULE_BLOCKS),
        ):
            with self.subTest(profile=profile), patch.object(
                server,
                "_media_rules_payload",
                return_value=(200, document),
            ):
                status, result = server._media_rules_profile_payload(profile, blocks)

            self.assertEqual(status, 200)
            self.assertEqual(tuple(result["rules"]), blocks)
            self.assertEqual(tuple(result["defaults"]), blocks)
            self.assertEqual(result["profile"], profile)
            self.assertTrue(result["connected"])
            self.assertTrue(result["editable"])

    def test_movie_save_preserves_hidden_trailer_and_version_with_cas(self) -> None:
        current = self.full_document()
        requested = {
            block: current["rules"][block].copy()
            for block in server.MOVIE_RULE_BLOCKS
        }
        requested["video"]["idioma_final"] = "es"
        saved_full = self.full_document("sha256:new")
        saved_full["rules"]["video"] = requested["video"]
        with patch.object(
            server,
            "_media_rules_payload",
            return_value=(200, current),
        ), patch.object(
            server,
            "_save_media_rules",
            return_value=(200, saved_full),
        ) as save:
            status, result = server._save_media_rules_profile(
                {"rules": requested, "expected_fingerprint": "sha256:old"},
                "movies",
                server.MOVIE_RULE_BLOCKS,
            )

        self.assertEqual(status, 200)
        forwarded = save.call_args.args[0]
        self.assertEqual(forwarded["expected_fingerprint"], "sha256:old")
        self.assertEqual(forwarded["rules"]["trailers"], current["rules"]["trailers"])
        self.assertEqual(forwarded["rules"]["version"], 1)
        self.assertEqual(forwarded["rules"]["video"]["idioma_final"], "es")
        self.assertNotIn("trailers", result["rules"])
        self.assertNotIn("version", result["rules"])
        self.assertEqual(result["fingerprint"], "sha256:new")

    def test_trailer_save_preserves_every_movie_block(self) -> None:
        current = self.full_document()
        trailer = {"trailers": {"nombre_final": "avance"}}
        saved_full = self.full_document("sha256:new")
        saved_full["rules"]["trailers"] = trailer["trailers"]
        with patch.object(
            server,
            "_media_rules_payload",
            return_value=(200, current),
        ), patch.object(
            server,
            "_save_media_rules",
            return_value=(200, saved_full),
        ) as save:
            status, result = server._save_media_rules_profile(
                {"rules": trailer, "expected_fingerprint": "sha256:old"},
                "trailers",
                server.TRAILER_RULE_BLOCKS,
            )

        self.assertEqual(status, 200)
        forwarded = save.call_args.args[0]["rules"]
        for block in server.MOVIE_RULE_BLOCKS:
            self.assertEqual(forwarded[block], current["rules"][block])
        self.assertEqual(result["rules"], trailer)

    def test_scoped_save_rejects_foreign_blocks_and_stale_fingerprint(self) -> None:
        with patch.object(server, "_media_rules_payload") as get_rules, patch.object(
            server,
            "_save_media_rules",
        ) as save:
            status, result = server._save_media_rules_profile(
                {
                    "rules": {"trailers": {}},
                    "expected_fingerprint": "sha256:old",
                },
                "movies",
                server.MOVIE_RULE_BLOCKS,
            )
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "invalid_rules")
        get_rules.assert_not_called()
        save.assert_not_called()

        with patch.object(server, "_media_rules_payload") as get_rules:
            status, result = server._save_media_rules_profile(
                {
                    "rules": {"video": {}},
                    "expected_fingerprint": "sha256:old",
                    "trailers": {"nombre_final": "oculto"},
                },
                "movies",
                server.MOVIE_RULE_BLOCKS,
            )
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "invalid_rules")
        get_rules.assert_not_called()

        current = self.full_document("sha256:newer")
        with patch.object(
            server,
            "_media_rules_payload",
            return_value=(200, current),
        ), patch.object(server, "_save_media_rules") as save:
            status, result = server._save_media_rules_profile(
                {
                    "rules": {"video": current["rules"]["video"]},
                    "expected_fingerprint": "sha256:old",
                },
                "movies",
                server.MOVIE_RULE_BLOCKS,
            )
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "fingerprint_conflict")
        self.assertEqual(result["current_fingerprint"], "sha256:newer")
        save.assert_not_called()

    def test_series_get_is_local_and_post_is_always_503(self) -> None:
        defaults = self.full_document()["rules"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "defaults.json"
            path.write_text(json.dumps(defaults), encoding="utf-8")
            with patch.object(server, "DEFAULT_RULES_PATH", path), patch.object(
                server,
                "_media_rules_payload",
            ) as worker_get, patch.object(server, "_save_media_rules") as worker_save:
                result = server._series_rules_payload()
                status, unavailable = server._series_rules_unavailable()

        worker_get.assert_not_called()
        worker_save.assert_not_called()
        self.assertEqual(tuple(result["rules"]), server.MOVIE_RULE_BLOCKS)
        self.assertNotIn("trailers", result["rules"])
        self.assertFalse(result["connected"])
        self.assertFalse(result["editable"])
        self.assertEqual(result["message"], "Motor de series no conectado")
        self.assertEqual(status, 503)
        self.assertEqual(unavailable["error"], "series_engine_not_connected")

    def test_profile_routes_preserve_status_and_never_proxy_series(self) -> None:
        movie = {"ok": True, "profile": "movies", "rules": {}}
        get_handler = _CapturedHandler("/api/movie-rules")
        with patch.object(
            server,
            "_media_rules_profile_payload",
            return_value=(200, movie),
        ) as get_profile:
            server.Handler.do_GET(get_handler)
        get_profile.assert_called_once_with("movies", server.MOVIE_RULE_BLOCKS)
        self.assertEqual(get_handler.response, (200, movie))

        series_handler = _CapturedHandler("/api/series-rules", {"rules": {}})
        series_handler.headers["Content-Type"] = "application/json"
        with patch.object(server, "_save_media_rules_profile") as scoped_save, patch.object(
            server,
            "_save_media_rules",
        ) as legacy_save:
            server.Handler.do_POST(series_handler)
        scoped_save.assert_not_called()
        legacy_save.assert_not_called()
        self.assertEqual(series_handler.response[0], 503)


class PanelProfilePayloadTests(unittest.TestCase):
    def test_review_profiles_keep_unclassified_items_visible_in_both_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            movie = root / "Movie"
            movie.mkdir()
            (movie / "reason.json").write_text(
                json.dumps({"job_id": "m1", "category": "movies"}),
                encoding="utf-8",
            )
            series = root / "Show"
            series.mkdir()
            (series / "reason.json").write_text(
                json.dumps(
                    {
                        "job_id": "s1",
                        "media_decision": {"media_type": "tv"},
                    }
                ),
                encoding="utf-8",
            )
            unknown = root / "Unknown"
            unknown.mkdir()
            (unknown / "reason.json").write_text(
                json.dumps({"job_id": "u1"}),
                encoding="utf-8",
            )

            with patch.object(server, "REVIEW_DIR", root):
                movies = server._review_payload(profile="movies")
                shows = server._review_payload(profile="series")
                legacy = server._review_payload()

        self.assertEqual(
            {item["name"] for item in movies["items"]},
            {"Movie", "Unknown"},
        )
        self.assertEqual(
            {item["name"] for item in shows["items"]},
            {"Show", "Unknown"},
        )
        self.assertEqual({item["name"] for item in legacy["items"]}, {"Movie", "Show", "Unknown"})
        self.assertEqual(movies["profile"], "movies")
        self.assertEqual(shows["profile"], "series")

    def test_series_reports_are_empty_and_do_not_expose_movie_worker_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "movie-report.json").write_text("{}", encoding="utf-8")
            with patch.object(server, "REPORT_ROOT", root):
                movies = server._reports_payload(profile="movies")
                series = server._reports_payload(profile="series")

        self.assertEqual([item["name"] for item in movies["files"]], ["movie-report.json"])
        self.assertTrue(movies["connected"])
        self.assertEqual(series["files"], [])
        self.assertEqual(series["report_root"], str(root))
        self.assertFalse(series["connected"])

    def test_status_keeps_legacy_and_adds_per_service_truth(self) -> None:
        orchestrator = {"status": "ok", "dependencies": {"db": True}}
        worker = {"status": "ok"}

        def health(url, timeout=8):
            del timeout
            return orchestrator if url.endswith(":8787/health") else worker

        with patch.object(server, "_upstream_json", side_effect=health):
            result = server._status_payload()

        self.assertEqual(result["orchestrator"], orchestrator)
        self.assertEqual(result["media_worker"], worker)
        self.assertTrue(result["services"]["orchestrator"]["connected"])
        self.assertTrue(result["services"]["movies"]["connected"])
        self.assertTrue(result["services"]["trailers"]["connected"])
        self.assertFalse(result["services"]["series"]["connected"])
        self.assertEqual(
            result["services"]["series"]["message"],
            "Motor de series no conectado",
        )

    def test_review_and_report_routes_forward_valid_profile(self) -> None:
        review_handler = _CapturedHandler("/api/review?profile=series")
        reports_handler = _CapturedHandler("/api/reports?profile=movies")
        with patch.object(
            server,
            "_review_payload",
            return_value={"ok": True, "items": [], "profile": "series"},
        ) as review, patch.object(
            server,
            "_reports_payload",
            return_value={"ok": True, "files": [], "profile": "movies"},
        ) as reports:
            server.Handler.do_GET(review_handler)
            server.Handler.do_GET(reports_handler)

        review.assert_called_once_with(profile="series")
        reports.assert_called_once_with(profile="movies")
        self.assertEqual(review_handler.response[0], 200)
        self.assertEqual(reports_handler.response[0], 200)

        invalid = _CapturedHandler("/api/review?profile=tv")
        server.Handler.do_GET(invalid)
        self.assertEqual(invalid.response[0], 400)
        self.assertEqual(invalid.response[1]["error"], "invalid_profile")


class MediaRulesPanelContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        web = Path(server.__file__).resolve().parent / "web"
        cls.panel_js = (web / "static" / "js" / "panel.js").read_text(encoding="utf-8")
        cls.identity_js = (
            web / "static" / "js" / "limpieza-arr" / "view.js"
        ).read_text(encoding="utf-8")
        cls.server_py = Path(server.__file__).read_text(encoding="utf-8")

    def test_media_save_sends_fingerprint_and_requires_activation(self) -> None:
        self.assertIn(
            "expected_fingerprint: documentState.fingerprint ?? null",
            self.panel_js,
        )
        self.assertIn("ruleDocumentEditable(documentState)", self.panel_js)
        self.assertIn("savedState.ok === false", self.panel_js)
        self.assertIn(
            "Reglas guardadas y activas para trabajos nuevos.", self.panel_js
        )
        self.assertIn(
            "La configuración se muestra completa, pero no se enviará ningún cambio.",
            self.panel_js,
        )
        self.assertIn(
            'input.dataset.rulePath === "video.idiomas_indeterminados_como_es" '
            "&& index === 0",
            self.panel_js,
        )

    def test_panel_no_longer_writes_media_rules_directly(self) -> None:
        self.assertNotIn("def _save_rules(", self.server_py)
        self.assertNotIn("tmp.replace(RULES_PATH)", self.server_py)
        self.assertIn("def _save_media_rules(", self.server_py)

    def test_identity_confirms_activation_for_new_jobs(self) -> None:
        self.assertIn(
            "Configuración guardada, versionada y activa para trabajos nuevos.",
            self.identity_js,
        )

    def test_save_tooltips_identify_owner_and_panel_endpoint(self) -> None:
        for endpoint in (
            "/api/movie-rules",
            "/api/series-rules",
            "/api/trailer-rules",
            "/api/watcher-rules/movies",
            "/api/watcher-rules/tv",
        ):
            self.assertIn(f'endpoint: "{endpoint}"', self.panel_js)
        self.assertIn('data-tooltip="${esc(sourceConfig.endpoint)}"', self.panel_js)
        self.assertIn(
            'data-tooltip="Orquestador ${ui.esc(API_ROOT)}/${ui.esc(state.profile)}"',
            self.identity_js,
        )


if __name__ == "__main__":
    unittest.main()
