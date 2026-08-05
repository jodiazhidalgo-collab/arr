import io
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest
import urllib.error
from unittest.mock import patch

from media_panel import server


def run_node_contract(script: str, *paths: Path) -> None:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("Node.js no está disponible")
    result = subprocess.run(
        [node, "-e", textwrap.dedent(script), *(str(path) for path in paths)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)


class _CapturedHandler:
    def __init__(self, path: str, payload=None) -> None:
        self.path = path
        self.payload = payload or {}
        self.response = None
        self.headers = {"Content-Length": "0"}

    def _send(self, status, body, content_type) -> None:
        self.response = (status, body, content_type)

    def _json(self, status, payload) -> None:
        self.response = (status, payload)

    def _read_payload(self):
        return self.payload

    def _content_length(self):
        return int(self.headers["Content-Length"])


class RulesProxyContractTests(unittest.TestCase):
    def test_proxy_preserves_http_error_status_and_body(self) -> None:
        body = b'{"ok":false,"error":"upstream_conflict","revision":9}'
        conflict = urllib.error.HTTPError(
            "http://upstream.invalid/settings/rules",
            409,
            "Conflict",
            None,
            io.BytesIO(body),
        )
        with patch.object(
            conflict, "close", wraps=conflict.close
        ) as close_error, patch.object(
            server.urllib.request, "urlopen", side_effect=conflict
        ):
            status, payload = server._proxy_upstream_json(
                "http://upstream.invalid/settings/rules",
                {"rules": {}, "expected_fingerprint": "old"},
            )

        self.assertEqual(status, 409)
        self.assertEqual(
            payload,
            {"ok": False, "error": "upstream_conflict", "revision": 9},
        )
        close_error.assert_called_once_with()

    def test_removed_rules_endpoint_returns_not_found(self) -> None:
        get_handler = _CapturedHandler("/api/filebot-rules")
        server.Handler.do_GET(get_handler)
        self.assertEqual(get_handler.response[0], 404)

        post_handler = _CapturedHandler(
            "/api/filebot-rules",
            {"rules": {}},
        )
        server.Handler.do_POST(post_handler)
        self.assertEqual(
            post_handler.response,
            (404, {"ok": False, "error": "Ruta no reconocida."}),
        )


class RulesPanelContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        web = Path(server.__file__).resolve().parent / "web"
        cls.panel_js = (web / "static" / "js" / "panel.js").read_text(
            encoding="utf-8"
        )
        cls.panel_css = (web / "static" / "css" / "panel.css").read_text(
            encoding="utf-8"
        )
        cls.server_py = Path(server.__file__).read_text(encoding="utf-8")

    def test_profile_sources_use_only_the_scoped_endpoints(self) -> None:
        self.assertIn("const RULE_VIEW_CONFIG = Object.freeze({", self.panel_js)
        for endpoint in (
            "/api/movie-rules",
            "/api/series-rules",
            "/api/trailer-rules",
            "/api/watcher-rules/movies",
            "/api/watcher-rules/tv",
        ):
            self.assertIn(f'endpoint: "{endpoint}"', self.panel_js)
        self.assertNotIn('endpoint: "/api/rules"', self.panel_js)
        self.assertIn(
            'const CLEANING_SECTIONS = Object.freeze(["entrada", "video", '
            '"audio", "subtitulos", "limpieza"]);',
            self.panel_js,
        )
        self.assertIn(
            'const SETTINGS_SECTIONS = Object.freeze(["trailers", "vigilantes"]);',
            self.panel_js,
        )

    def test_save_updates_only_the_selected_source(self) -> None:
        self.assertIn("state.documents[source] = savedState;", self.panel_js)
        self.assertIn("state.drafts[source]", self.panel_js)
        self.assertIn("state.dirty[source]", self.panel_js)
        self.assertIn("state.requestEpoch[source]", self.panel_js)
        self.assertIn(
            "expected_fingerprint: documentState.fingerprint ?? null",
            self.panel_js,
        )

    def test_rule_load_and_save_share_one_document_generation(self) -> None:
        self.assertIn(
            "function nextRuleDocumentEpoch(state, source)", self.panel_js
        )
        self.assertGreaterEqual(
            self.panel_js.count(
                "const documentEpoch = nextRuleDocumentEpoch(state, source);"
            ),
            2,
        )
        self.assertGreaterEqual(
            self.panel_js.count(
                "if (!isCurrentRuleDocumentEpoch(state, source, documentEpoch)) return;"
            ),
            4,
        )
        self.assertIn(
            'id="reload-rules-profile" ${saving ? "disabled" : ""}',
            self.panel_js,
        )
        self.assertIn("if (state.saving[source]) return;", self.panel_js)

    def test_stale_rule_responses_cannot_replace_a_newer_generation(self) -> None:
        panel = (
            Path(server.__file__).resolve().parent
            / "web"
            / "static"
            / "js"
            / "panel.js"
        )
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const fakeApp = { innerHTML: "" };
            const fakeTitle = { textContent: "" };
            const tabButtons = ["identidad", "limpieza-peliculas", "limpieza-series", "ajustes", "motor", "historial", "revision", "informes"].map(view => ({
              dataset: { view },
              classList: { toggle: () => {} },
              addEventListener: () => {}
            }));
            const response = payload => ({ ok: true, status: 200, text: async () => JSON.stringify(payload) });
            global.location = { hash: "#motor", href: "" };
            global.history = { replaceState: () => {} };
            global.localStorage = { getItem: () => null, setItem: () => {} };
            global.document = {
              getElementById: id => id === "app" ? fakeApp : id === "title" ? fakeTitle : null,
              querySelectorAll: selector => selector === ".tabs button" ? tabButtons : [],
              addEventListener: () => {}
            };
            global.window = {
              ArrIdentityUI: { show: async () => {} },
              confirm: () => true,
              addEventListener: () => {}
            };
            global.fetch = path => {
              if (path === "/api/status") return Promise.resolve(response({ orchestrator: {}, media_worker: {}, paths: {} }));
              if (path === "/api/jobs") return Promise.resolve(response({ jobs: [] }));
              throw new Error(`Fetch inicial inesperado: ${path}`);
            };
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));

            (async () => {
              const view = "limpieza-peliculas";
              const source = "rules";
              const state = ruleViewStates[view];
              const original = { ok: true, connected: true, editable: true, fingerprint: "a".repeat(64), rules: { entrada: { extensiones_video: [".mkv"] } } };
              state.documents[source] = original;
              state.drafts[source] = { entrada: { extensiones_video: [".mp4"] } };
              state.dirty[source] = true;

              let resolveSave;
              let reloadCalls = 0;
              const saveResponse = new Promise(resolve => { resolveSave = resolve; });
              global.fetch = (path, options = {}) => {
                if (options.method === "POST") return saveResponse;
                reloadCalls += 1;
                return Promise.resolve(response(original));
              };
              const pendingSave = saveRuleSource(view, source);
              if (!state.saving[source]) throw new Error("Guardar no quedó marcado como activo");
              if (reloadRuleSource(view, source) !== undefined || reloadCalls !== 0) {
                throw new Error("Recargar se ejecutó mientras Guardar estaba activo");
              }

              const newerAfterSave = { ...original, fingerprint: "b".repeat(64), rules: { entrada: { extensiones_video: [".avi"] } } };
              nextRuleDocumentEpoch(state, source);
              state.documents[source] = newerAfterSave;
              state.drafts[source] = clone(newerAfterSave.rules);
              state.dirty[source] = false;
              state.saving[source] = false;
              resolveSave(response({ ...original, fingerprint: "c".repeat(64) }));
              await pendingSave;
              if (state.documents[source].fingerprint !== newerAfterSave.fingerprint) {
                throw new Error("Una respuesta tardía de Guardar pisó la generación nueva");
              }

              let resolveLoad;
              const loadResponse = new Promise(resolve => { resolveLoad = resolve; });
              global.fetch = () => loadResponse;
              const pendingLoad = loadRuleSource(view, source, { replace: true });
              const newerAfterLoad = { ...original, fingerprint: "d".repeat(64), rules: { entrada: { extensiones_video: [".mov"] } } };
              nextRuleDocumentEpoch(state, source);
              state.documents[source] = newerAfterLoad;
              state.drafts[source] = clone(newerAfterLoad.rules);
              state.loading[source] = false;
              resolveLoad(response({ ...original, fingerprint: "e".repeat(64) }));
              await pendingLoad;
              if (state.documents[source].fingerprint !== newerAfterLoad.fingerprint) {
                throw new Error("Una respuesta tardía de Recargar pisó la generación nueva");
              }
            })().catch(error => {
              console.error(error.stack || error);
              process.exitCode = 1;
            });
            """,
            panel,
        )

    def test_every_editable_rule_document_requires_a_cas_fingerprint(self) -> None:
        self.assertIn(
            'typeof documentState.fingerprint === "string"', self.panel_js
        )
        self.assertIn(
            'typeof payload.fingerprint === "string"', self.panel_js
        )
        self.assertIn("una huella CAS válida", self.panel_js)
        self.assertIn("error.status === 409", self.panel_js)
        self.assertIn("Conflicto al guardar", self.panel_js)

    def test_hash_priority_persistence_and_legacy_aliases_are_explicit(self) -> None:
        self.assertIn("const PANEL_ROUTE_STORAGE_KEY", self.panel_js)
        self.assertIn("function exactCanonicalRoute(hash)", self.panel_js)
        self.assertIn("function canonicalRouteFromHash", self.panel_js)
        self.assertIn("const exact = exactCanonicalRoute(hash);", self.panel_js)
        self.assertIn("const storedHash = storageGet(", self.panel_js)
        self.assertIn(
            "canonicalRouteFromHash(storedHash, { useStored: false })",
            self.panel_js,
        )
        self.assertIn("if (!stored.fallback)", self.panel_js)
        self.assertIn('history.replaceState(null, "", route.hash)', self.panel_js)
        self.assertIn("#identidad/comun/parser", self.panel_js)
        self.assertIn("#limpieza-peliculas/${section}", self.panel_js)
        self.assertIn("#ajustes/trailers", self.panel_js)
        self.assertIn("#ajustes/vigilante-peliculas", self.panel_js)
        self.assertIn("#ajustes/vigilante-series", self.panel_js)
        self.assertIn('/^#ajustes\\/vigilantes$/', self.panel_js)
        self.assertIn("/^#reglas", self.panel_js)
        self.assertIn("window.ArrIdentityUI.resolveTarget(hash)", self.panel_js)

    def test_series_is_complete_but_locked_to_its_own_contract(self) -> None:
        self.assertIn('endpoint: "/api/series-rules"', self.panel_js)
        self.assertIn("documentState.connected !== false", self.panel_js)
        self.assertIn("documentState.editable !== false", self.panel_js)
        self.assertIn('class="rules-editor-locked"', self.panel_js)
        self.assertIn("La configuración se muestra completa", self.panel_js)
        self.assertIn("config.sections.map", self.panel_js)

    def test_review_and_reports_are_profile_scoped(self) -> None:
        self.assertIn("/api/jobs?profile=${encodeURIComponent(profile)}", self.panel_js)
        self.assertIn("/api/review?profile=${encodeURIComponent(profile)}", self.panel_js)
        self.assertIn("/api/reports?profile=${encodeURIComponent(profile)}", self.panel_js)
        self.assertIn(
            "/api/codex-diagnostics?profile=${encodeURIComponent(profile)}",
            self.panel_js,
        )
        self.assertIn(
            "/api/report?profile=${encodeURIComponent(profile)}&file=${encodeURIComponent(file)}",
            self.panel_js,
        )
        self.assertIn("arr-media-panel-historial-profile", self.panel_js)
        self.assertIn("arr-media-panel-revision-profile", self.panel_js)
        self.assertIn("arr-media-panel-informes-profile", self.panel_js)

    def test_review_reason_type_controls_visible_label_and_tone(self) -> None:
        self.assertIn("const reason = reviewReasonPresentation(item);", self.panel_js)
        self.assertIn("const reasonText = reviewReasonText(item, reason);", self.panel_js)
        self.assertIn("pill(reason.label, reason.tone)", self.panel_js)
        panel = Path(server.__file__).resolve().parent / "web" / "static" / "js" / "panel.js"
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const fakeApp = { innerHTML: "" };
            const fakeTitle = { textContent: "" };
            const tabButtons = ["identidad", "limpieza-peliculas", "limpieza-series", "ajustes", "motor", "historial", "revision", "informes"].map(view => ({
              dataset: { view },
              classList: { toggle: () => {} },
              addEventListener: () => {}
            }));
            global.location = { hash: "#revision", href: "" };
            global.history = { replaceState: () => {} };
            global.localStorage = { getItem: () => null, setItem: () => {} };
            global.document = {
              getElementById: id => id === "app" ? fakeApp : id === "title" ? fakeTitle : null,
              querySelectorAll: selector => selector === ".tabs button" ? tabButtons : [],
              addEventListener: () => {}
            };
            global.window = {
              ArrIdentityUI: { show: async () => {} },
              addEventListener: () => {}
            };
            global.fetch = async path => ({
              ok: true,
              status: 200,
              text: async () => JSON.stringify(path.includes("/api/review") ? { items: [], review_dir: "<REVIEW>" } : {})
            });

            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));

            const audioItem = {
              profile: "series",
              reason_file: "Serie repetida.txt",
              reason_code: "procesamiento_fallido",
              reason_kind: "audio",
              reason_text: "Serie repetida\nNo hay audio español válido.\n"
            };
            const audio = reviewReasonPresentation(audioItem);
            if (audio.label !== "Audio no válido" || audio.tone !== "bad") {
              throw new Error(`Audio mal presentado: ${JSON.stringify(audio)}`);
            }
            const audioText = reviewReasonText(audioItem, audio);
            if (audioText.includes("Serie repetida") || !audioText.startsWith("Audio no válido\n")) {
              throw new Error(`Cabecera de audio engañosa: ${audioText}`);
            }

            const processItem = { ...audioItem, reason_kind: "process", reason_text: "Serie repetida\nFFmpeg falló.\n" };
            const processReason = reviewReasonPresentation(processItem);
            if (processReason.label !== "Error de proceso" || processReason.tone !== "bad") {
              throw new Error(`Proceso mal presentado: ${JSON.stringify(processReason)}`);
            }
            if (reviewReasonText(processItem, processReason).includes("Serie repetida")) {
              throw new Error("El error de proceso sigue apareciendo como repetido");
            }

            const duplicate = reviewReasonPresentation({ profile: "series", reason_code: "colision_existente", reason_kind: "duplicate" });
            if (duplicate.label !== "Serie repetida" || duplicate.tone !== "warn") {
              throw new Error(`Duplicado mal presentado: ${JSON.stringify(duplicate)}`);
            }
            const identity = reviewReasonPresentation({ reason_code: "category_conflict", reason_kind: "manual" });
            if (identity.label !== "Categoría contradictoria" || identity.tone !== "warn") {
              throw new Error(`Revisión manual mal presentada: ${JSON.stringify(identity)}`);
            }
            const fallback = reviewReasonPresentation({ reason_file: "Aviso antiguo.txt" });
            if (fallback.label !== "Aviso antiguo" || fallback.typed) {
              throw new Error(`Fallback legacy roto: ${JSON.stringify(fallback)}`);
            }
            """,
            panel,
        )

    def test_report_open_checks_http_status_and_renders_a_visible_error(self) -> None:
        self.assertIn("const response = await fetch(", self.panel_js)
        self.assertIn("if (!response.ok)", self.panel_js)
        self.assertIn("No se pudo abrir el informe", self.panel_js)
        self.assertIn("report-view-error", self.panel_js)

    def test_series_status_distinguishes_legacy_canary_and_active(self) -> None:
        self.assertIn("modo legacy · no enruta trabajos nuevos", self.panel_js)
        self.assertIn("modo canary · solo pruebas seleccionadas", self.panel_js)
        self.assertIn("activo para todos los trabajos nuevos", self.panel_js)
        self.assertIn('const runtimeStatus = await api("/api/status")', self.panel_js)

    def test_mobile_history_and_review_are_width_safe(self) -> None:
        self.assertIn('class="table jobs-table"', self.panel_js)
        for label in ("Nombre", "Categoría", "Estado", "Actualizado", "Diagnóstico"):
            self.assertIn(f'data-label="{label}"', self.panel_js)
        self.assertIn(".jobs-table td::before", self.panel_css)
        self.assertIn("content: attr(data-label);", self.panel_css)
        self.assertIn(".review-top > div", self.panel_css)
        self.assertIn("overflow-wrap: anywhere;", self.panel_css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", self.panel_css)
        self.assertIn(".report-row > b", self.panel_css)

    def test_removed_filebot_editor_and_proxy_are_not_exposed(self) -> None:
        for text in (
            'title: "FileBot Películas"',
            'title: "FileBot Series"',
            'endpoint: "/api/filebot-rules"',
            'source: "filebot"',
            "renderFileBotExtras",
            "fileBotPreview",
            "Protecciones activas",
            "payload.expected_revision",
        ):
            self.assertNotIn(text, self.panel_js)
        self.assertNotIn("/api/filebot-rules", self.server_py)
        self.assertNotIn("def _filebot_rules_payload(", self.server_py)
        self.assertNotIn("def _save_filebot_rules(", self.server_py)
        self.assertNotIn(".readonly-value", self.panel_css)

    def test_list_controls_use_textarea_without_trash_icons(self) -> None:
        self.assertIn("Una entrada por línea.", self.panel_js)
        self.assertNotIn("Papelera", self.panel_js)
        self.assertNotIn("trash", self.panel_js.lower())


if __name__ == "__main__":
    unittest.main()
