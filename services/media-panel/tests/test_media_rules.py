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

    @classmethod
    def cleaning_rules(cls):
        rules = cls.full_document()["rules"]
        return {block: rules[block] for block in server.MOVIE_RULE_BLOCKS}

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

    def test_series_rules_proxy_uses_only_series_worker_and_preserves_cas(self) -> None:
        current = self.full_document()
        requested = {
            "rules": current["rules"],
            "expected_fingerprint": "sha256:old",
        }
        saved = self.full_document("sha256:new")
        with patch.object(
            server,
            "_proxy_upstream_json",
            side_effect=((200, current), (200, saved)),
        ) as upstream, patch.object(server, "_media_rules_payload") as media_get, patch.object(
            server,
            "_save_media_rules",
        ) as media_save:
            get_status, get_result = server._series_rules_payload()
            post_status, post_result = server._save_series_rules(requested)

        self.assertEqual(get_status, 200)
        self.assertEqual(post_status, 200)
        self.assertTrue(get_result["connected"])
        self.assertTrue(get_result["editable"])
        self.assertEqual(post_result["fingerprint"], "sha256:new")
        self.assertEqual(
            upstream.call_args_list[0].args,
            (f"{server.SERIES_WORKER_URL}/settings/rules",),
        )
        self.assertEqual(upstream.call_args_list[0].kwargs, {"timeout": 8})
        self.assertEqual(
            upstream.call_args_list[1].args,
            (f"{server.SERIES_WORKER_URL}/settings/rules", requested),
        )
        self.assertEqual(upstream.call_args_list[1].kwargs, {"timeout": 20})
        media_get.assert_not_called()
        media_save.assert_not_called()

    def test_series_rules_degrade_without_affecting_other_profiles(self) -> None:
        unavailable = {
            "ok": False,
            "error": "connection refused",
            "upstream_status": 502,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            series_rules = root / "series.json"
            defaults = root / "defaults.json"
            series_rules.write_text(
                json.dumps(self.cleaning_rules()),
                encoding="utf-8",
            )
            defaults.write_text(
                json.dumps(self.cleaning_rules()),
                encoding="utf-8",
            )
            with patch.object(
                server,
                "_proxy_upstream_json",
                return_value=(502, unavailable),
            ), patch.object(server, "_media_rules_payload") as media_get, patch.object(
                server, "SERIES_RULES_PATH", series_rules
            ), patch.object(server, "DEFAULT_RULES_PATH", defaults):
                get_status, get_result = server._series_rules_payload()
                post_status, post_result = server._save_series_rules(
                    {"rules": {}, "expected_fingerprint": "sha256:old"}
                )

        self.assertEqual(get_status, 200)
        self.assertTrue(get_result["ok"])
        self.assertFalse(get_result["connected"])
        self.assertFalse(get_result["editable"])
        self.assertEqual(get_result["rules"], self.cleaning_rules())
        self.assertEqual(get_result["active"], self.cleaning_rules())
        self.assertEqual(get_result["defaults"], self.cleaning_rules())
        self.assertEqual(get_result["rules_path"], server.SERIES_RULES_ALIAS)
        self.assertNotIn("trailers", get_result["rules"])
        self.assertIsInstance(get_result["fingerprint"], str)
        self.assertEqual(post_status, 503)
        self.assertEqual(post_result["error"], "series_worker_unavailable")
        media_get.assert_not_called()

    def test_series_rules_get_rejects_an_incomplete_worker_document(self) -> None:
        fallback = {
            "rules": self.cleaning_rules(),
            "active": self.cleaning_rules(),
            "defaults": self.cleaning_rules(),
            "rules_path": server.SERIES_RULES_ALIAS,
            "fingerprint": "sha256:fallback",
        }
        with patch.object(
            server,
            "_proxy_upstream_json",
            return_value=(200, {"ok": True, "rules": {}}),
        ), patch.object(server, "_series_rules_fallback", return_value=fallback):
            status, result = server._series_rules_payload()

        self.assertEqual(status, 200)
        self.assertFalse(result["connected"])
        self.assertFalse(result["editable"])
        self.assertEqual(result["rules"], self.cleaning_rules())
        self.assertEqual(result["fingerprint"], "sha256:fallback")

    def test_series_rules_preserve_worker_cas_conflict(self) -> None:
        conflict = {
            "ok": False,
            "error": "fingerprint_conflict",
            "current_fingerprint": "sha256:new",
        }
        with patch.object(
            server,
            "_proxy_upstream_json",
            return_value=(409, conflict),
        ):
            status, result = server._save_series_rules(
                {"rules": {}, "expected_fingerprint": "sha256:old"}
            )

        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "fingerprint_conflict")
        self.assertEqual(result["current_fingerprint"], "sha256:new")
        self.assertTrue(result["connected"])
        self.assertTrue(result["editable"])

    def test_profile_routes_preserve_status_and_proxy_series_separately(self) -> None:
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
        series_saved = {
            "ok": True,
            "profile": "series",
            "rules": {},
            "fingerprint": "sha256:new",
        }
        with patch.object(server, "_save_series_rules", return_value=(200, series_saved)) as series_save, patch.object(
            server,
            "_save_media_rules_profile",
        ) as scoped_save, patch.object(
            server,
            "_save_media_rules",
        ) as legacy_save:
            server.Handler.do_POST(series_handler)
        series_save.assert_called_once_with({"rules": {}})
        scoped_save.assert_not_called()
        legacy_save.assert_not_called()
        self.assertEqual(series_handler.response, (200, series_saved))


class PanelProfilePayloadTests(unittest.TestCase):
    def test_review_profiles_use_separate_roots_and_series_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movie-review"
            series_root = base / "series-review"
            movie = movie_root / "Movie"
            movie.mkdir(parents=True)
            (movie / "reason.json").write_text(
                json.dumps({"job_id": "m1", "category": "movies"}),
                encoding="utf-8",
            )
            series = series_root / "Show"
            series.mkdir(parents=True)
            (series / "reason.json").write_text(
                json.dumps(
                    {
                        "job_id": "s1",
                        "media_decision": {"media_type": "tv"},
                    }
                ),
                encoding="utf-8",
            )
            (series / "details.txt").write_text(
                f"Revisar {series_root / 'Show'}",
                encoding="utf-8",
            )
            unknown = movie_root / "Unknown"
            unknown.mkdir()
            (unknown / "reason.json").write_text(
                json.dumps({"job_id": "u1"}),
                encoding="utf-8",
            )

            with patch.object(server, "REVIEW_DIR", movie_root), patch.object(
                server,
                "SERIES_REVIEW_DIR",
                series_root,
            ):
                movies = server._review_payload(profile="movies")
                shows = server._review_payload(profile="series")
                legacy = server._review_payload()

        self.assertEqual(
            {item["name"] for item in movies["items"]},
            {"Movie", "Unknown"},
        )
        self.assertEqual(
            {item["name"] for item in shows["items"]},
            {"Show"},
        )
        self.assertEqual({item["name"] for item in legacy["items"]}, {"Movie", "Unknown"})
        self.assertEqual(movies["profile"], "movies")
        self.assertEqual(shows["profile"], "series")
        self.assertEqual(movies["review_dir"], str(movie_root))
        self.assertEqual(shows["review_dir"], server.SERIES_REVIEW_ALIAS)
        self.assertTrue(shows["connected"])
        self.assertEqual(
            shows["items"][0]["path"],
            f"{server.SERIES_REVIEW_ALIAS}/Show",
        )
        self.assertIn(server.SERIES_REVIEW_ALIAS, shows["items"][0]["reason_text"])
        self.assertNotIn(str(series_root), json.dumps(shows))

    def test_series_reports_use_their_real_root_without_movie_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            movie_root = base / "movie-reports"
            series_root = base / "series-reports"
            movie_root.mkdir()
            series_root.mkdir()
            (movie_root / "movie-report.json").write_text("{}", encoding="utf-8")
            series_report = series_root / "series-job" / "series_result.json"
            series_report.parent.mkdir()
            series_report.write_text("{}", encoding="utf-8")
            with patch.object(server, "REPORT_ROOT", movie_root), patch.object(
                server,
                "SERIES_REPORT_ROOT",
                series_root,
            ):
                movies = server._reports_payload(profile="movies")
                series = server._reports_payload(profile="series")

        self.assertEqual([item["name"] for item in movies["files"]], ["movie-report.json"])
        self.assertTrue(movies["connected"])
        self.assertEqual(movies["report_root"], str(movie_root))
        self.assertNotIn("path", movies["files"][0])
        self.assertEqual([item["name"] for item in series["files"]], ["series_result.json"])
        self.assertEqual(series["report_root"], server.SERIES_REPORT_ALIAS)
        self.assertEqual(
            series["files"][0]["path"],
            f"{server.SERIES_REPORT_ALIAS}/series-job/series_result.json",
        )
        self.assertTrue(series["connected"])
        self.assertNotIn(str(series_root), json.dumps(series))

    def test_status_keeps_legacy_and_adds_per_service_truth(self) -> None:
        orchestrator = {
            "status": "ok",
            "mode": "active",
            "series_mode": "legacy",
            "dependencies": {"db": True},
        }
        worker = {"status": "ok"}
        series_worker = {"status": "ok", "atomic_rules": True}

        def health(url, timeout=8):
            del timeout
            if url.endswith(":8787/health"):
                return orchestrator
            if url.endswith(":8791/health"):
                return series_worker
            return worker

        with patch.object(server, "_upstream_json", side_effect=health):
            result = server._status_payload()

        self.assertEqual(result["orchestrator"], orchestrator)
        self.assertEqual(result["media_worker"], worker)
        self.assertEqual(result["series_worker"], series_worker)
        self.assertTrue(result["services"]["orchestrator"]["connected"])
        self.assertTrue(result["services"]["movies"]["connected"])
        self.assertTrue(result["services"]["trailers"]["connected"])
        self.assertTrue(result["services"]["series"]["connected"])
        self.assertTrue(result["services"]["series"]["editable"])
        self.assertEqual(result["mode"], "active")
        self.assertEqual(result["series_mode"], "legacy")
        self.assertTrue(result["health"]["series"])
        self.assertEqual(result["services"]["series"]["mode"], "legacy")
        self.assertFalse(result["services"]["series"]["routing_active"])
        self.assertEqual(
            result["services"]["series"]["message"],
            "Motor sano · modo legacy",
        )

    def test_status_marks_only_series_disconnected_when_its_health_fails(self) -> None:
        def health(url, timeout=8):
            del timeout
            if url.endswith(":8791/health"):
                return {"ok": False, "error": "connection refused"}
            return {"status": "ok"}

        with patch.object(server, "_upstream_json", side_effect=health):
            result = server._status_payload()

        self.assertTrue(result["services"]["orchestrator"]["connected"])
        self.assertTrue(result["services"]["movies"]["connected"])
        self.assertTrue(result["services"]["trailers"]["connected"])
        self.assertFalse(result["services"]["series"]["connected"])
        self.assertFalse(result["services"]["series"]["editable"])
        self.assertEqual(
            result["services"]["series"]["message"],
            "Motor de series no conectado",
        )

    def test_review_and_report_routes_forward_valid_profile(self) -> None:
        review_handler = _CapturedHandler("/api/review?profile=series")
        reports_handler = _CapturedHandler("/api/reports?profile=series")
        with patch.object(
            server,
            "_review_payload",
            return_value={"ok": True, "items": [], "profile": "series"},
        ) as review, patch.object(
            server,
            "_reports_payload",
            return_value={"ok": True, "files": [], "profile": "series"},
        ) as reports:
            server.Handler.do_GET(review_handler)
            server.Handler.do_GET(reports_handler)

        review.assert_called_once_with(profile="series")
        reports.assert_called_once_with(profile="series")
        self.assertEqual(review_handler.response[0], 200)
        self.assertEqual(reports_handler.response[0], 200)

        invalid = _CapturedHandler("/api/review?profile=tv")
        server.Handler.do_GET(invalid)
        self.assertEqual(invalid.response[0], 400)
        self.assertEqual(invalid.response[1]["error"], "invalid_profile")

    def test_history_and_codex_routes_forward_and_validate_profile(self) -> None:
        jobs_handler = _CapturedHandler("/api/jobs?profile=series")
        codex_handler = _CapturedHandler("/api/codex-diagnostics?profile=movies")
        with patch.object(
            server,
            "_jobs_payload",
            return_value={"ok": True, "jobs": [], "profile": "series"},
        ) as jobs, patch.object(
            server,
            "_codex_diagnostics_payload",
            return_value={"ok": True, "files": [], "profile": "movies"},
        ) as codex:
            server.Handler.do_GET(jobs_handler)
            server.Handler.do_GET(codex_handler)

        jobs.assert_called_once_with(profile="series")
        codex.assert_called_once_with(profile="movies")
        self.assertEqual(jobs_handler.response[0], 200)
        self.assertEqual(codex_handler.response[0], 200)

        for path in (
            "/api/jobs?profile=tv",
            "/api/codex-diagnostics?profile=common",
        ):
            handler = _CapturedHandler(path)
            server.Handler.do_GET(handler)
            self.assertEqual(handler.response[0], 400)
            self.assertEqual(handler.response[1]["error"], "invalid_profile")


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
