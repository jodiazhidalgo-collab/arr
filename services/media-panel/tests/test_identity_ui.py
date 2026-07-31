import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

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


class IdentityUiStaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web = Path(server.__file__).resolve().parent / "web"
        cls.index = (cls.web / "index.html").read_text(encoding="utf-8")
        cls.panel = (cls.web / "static" / "js" / "panel.js").read_text(
            encoding="utf-8"
        )
        cls.panel_styles = (cls.web / "static" / "css" / "panel.css").read_text(
            encoding="utf-8"
        )
        js_root = cls.web / "static" / "js" / "limpieza-arr"
        css_root = cls.web / "static" / "css" / "limpieza-arr"
        cls.utils = (js_root / "utils.js").read_text(encoding="utf-8")
        cls.controls = (js_root / "controls.js").read_text(encoding="utf-8")
        cls.resolver_result = (js_root / "resolver-result.js").read_text(
            encoding="utf-8"
        )
        cls.testers = (js_root / "testers.js").read_text(encoding="utf-8")
        cls.view = (js_root / "view.js").read_text(encoding="utf-8")
        cls.styles = "\n".join(
            (css_root / name).read_text(encoding="utf-8")
            for name in (
                "layout.css",
                "controls.css",
                "tester.css",
                "resolver-result.css",
            )
        )

    def test_top_tab_and_modular_assets_are_loaded_in_order(self) -> None:
        navigation = (
            ('data-view="identidad"', "Identidad ARR"),
            ('data-view="limpieza-peliculas"', "Limpieza películas"),
            ('data-view="limpieza-series"', "Limpieza series"),
            ('data-view="ajustes"', "Ajustes"),
            ('data-view="motor"', "Motor"),
            ('data-view="historial"', "Historial"),
            ('data-view="revision"', "Revisión"),
            ('data-view="informes"', "Informes"),
        )
        positions = []
        for marker, label in navigation:
            self.assertIn(marker, self.index)
            self.assertIn(label, self.index)
            positions.append(self.index.index(marker))
        self.assertEqual(positions, sorted(positions))
        for asset in (
            "/static/css/limpieza-arr/layout.css",
            "/static/css/limpieza-arr/controls.css",
            "/static/css/limpieza-arr/tester.css",
            "/static/css/limpieza-arr/resolver-result.css",
            "/static/js/limpieza-arr/utils.js",
            "/static/js/limpieza-arr/controls.js",
            "/static/js/limpieza-arr/resolver-result.js",
            "/static/js/limpieza-arr/testers.js",
            "/static/js/limpieza-arr/view.js",
        ):
            self.assertIn(asset, self.index)
        self.assertLess(
            self.index.index("/static/js/limpieza-arr/resolver-result.js"),
            self.index.index("/static/js/limpieza-arr/testers.js"),
        )
        self.assertLess(
            self.index.index("/static/js/limpieza-arr/view.js"),
            self.index.index("/static/js/panel.js"),
        )

    def test_hash_and_dirty_draft_survive_subtab_navigation(self) -> None:
        self.assertIn('`#identidad/${ui.profileSlug(state.profile)}/${section}`', self.view)
        self.assertIn("arr-identity-section-${profile}", self.view)
        self.assertIn("state.section = section;", self.view)
        self.assertNotIn("ui.state.draft = ui.clone(ui.state.document.rules);\n    rememberSection", self.view)
        self.assertIn('window.addEventListener("beforeunload"', self.view)
        self.assertIn("confirmDraftLoss", self.view)
        self.assertIn("arr-identity-open-${ui.activeProfile}-${section}", self.controls)
        self.assertIn("ui.states", self.utils)
        self.assertIn("requestEpoch", self.utils)
        self.assertIn("ui.storageGet", self.controls)
        self.assertIn("ui.storageSet", self.controls)
        self.assertNotIn("localStorage.setItem", self.controls)

    def test_late_identity_response_cannot_paint_or_replace_another_profile(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const app = { innerHTML: "" };
            global.location = { hash: "#identidad/comun/parser" };
            global.history = { replaceState: () => {} };
            global.localStorage = { getItem: () => null, setItem: () => {} };
            global.document = { getElementById: id => id === "app" ? app : null, querySelectorAll: () => [] };
            global.window = { addEventListener: () => {}, confirm: () => true };
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
            const ui = window.ArrIdentityUI;
            const resolvers = {};
            const calls = [];
            ui.api = path => {
              calls.push(path);
              return new Promise(resolve => { resolvers[path] = resolve; });
            };
            const payload = (profile, marker) => ({
              ok: true,
              profile,
              revision: 1,
              rules: { parser: { marker }, resolver: {} },
              defaults: { parser: {}, resolver: {} },
              schema: { parser: { groups: [] }, resolver: { groups: [] } }
            });
            const renders = [];
            ui.render = () => { renders.push(ui.activeProfile); };

            (async () => {
              ui.setActiveProfile("common");
              const commonRequest = ui.loadRules({ profile: "common" });
              location.hash = "#identidad/peliculas/parser";
              ui.setActiveProfile("movies");
              const moviesRequest = ui.loadRules({ profile: "movies" });
              resolvers["/api/identity-rules/movies"](payload("movies", "MOVIES"));
              await moviesRequest;
              resolvers["/api/identity-rules/common"](payload("common", "COMMON"));
              await commonRequest;
              if (calls.join("|") !== "/api/identity-rules/common|/api/identity-rules/movies") throw new Error(`Rutas mezcladas: ${calls}`);
              if (ui.states.common.draft.parser.marker !== "COMMON") throw new Error("Common perdió su documento");
              if (ui.states.movies.draft.parser.marker !== "MOVIES") throw new Error("Movies perdió su documento");
              if (ui.activeProfile !== "movies") throw new Error("La respuesta tardía cambió el perfil");
              if (renders.join("|") !== "movies") throw new Error(`La respuesta tardía repintó: ${renders}`);
            })().catch(error => {
              console.error(error.stack || error);
              process.exitCode = 1;
            });
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "view.js",
        )

    def test_save_reset_import_and_cache_contracts_are_explicit(self) -> None:
        self.assertIn('const API_ROOT = "/api/identity-rules";', self.view)
        self.assertIn('`${API_ROOT}/${profile}`', self.view)
        self.assertIn("expected_revision: Number(state.document.revision || 0)", self.view)
        self.assertIn("state.draft = ui.clone(state.document.defaults);", self.view)
        self.assertIn("Pulsa Guardar para aplicarlos", self.view)
        self.assertIn("const MAX_IMPORT_BYTES = 4 * 1024 * 1024;", self.view)
        self.assertIn("El JSON supera el límite de 4 MB", self.view)
        self.assertIn("`${API_ROOT}/${profile}/cache/clear`", self.view)
        self.assertIn("Tu borrador se conserva", self.view)
        self.assertIn("payload.repair_required", self.view)
        self.assertIn("Boolean(state.document.repair_required)", self.view)
        self.assertIn("validRulesDocument(payload, profile)", self.view)
        self.assertIn("validCachePayload(payload)", self.view)
        self.assertIn("ui.isProfileActive(profile)", self.view)
        self.assertIn('body: "{}"', self.view)

    def test_save_cursor_waits_only_during_a_real_save(self) -> None:
        self.assertIn(
            '!state.dirty && !saving ? \'data-idle-disabled="true"\'',
            self.view,
        )
        self.assertIn(
            'save.toggleAttribute("data-idle-disabled", !state.dirty);',
            self.view,
        )
        self.assertIn(
            '#identity-save:disabled[data-idle-disabled="true"]',
            self.panel_styles,
        )
        self.assertIn("cursor: default;", self.panel_styles)
        self.assertIn(".btn:disabled", self.panel_styles)
        self.assertIn("cursor: wait;", self.panel_styles)

    def test_save_tooltip_remains_visible_when_button_is_disabled(self) -> None:
        self.assertIn(".btn[data-tooltip]:hover::after", self.panel_styles)
        self.assertIn("content: attr(data-tooltip);", self.panel_styles)
        self.assertIn('data-tooltip="Orquestador ${ui.esc(API_ROOT)}/${ui.esc(state.profile)}"', self.view)

    def test_schema_controls_are_dynamic_and_fully_editable(self) -> None:
        for marker in (
            "ui.renderGroups = function",
            "ui.renderControl = function",
            'data-list-add=',
            'data-list-delete',
            'data-list-duplicate',
            'data-list-move=',
            'data-pair-add=',
            'data-pair-delete',
            'data-reset-control=',
            'data-reset-group=',
        ):
            self.assertIn(marker, self.controls)

    def test_title_testers_use_unsaved_draft_and_human_resolver_contract(self) -> None:
        self.assertIn("const submittedRules = ui.clone(state.draft);", self.testers)
        self.assertIn("rules: submittedRules", self.testers)
        self.assertIn("${ui.identityApiRoot(profile)}/test-${section}", self.testers)
        self.assertIn("Probar título", self.testers)
        self.assertIn("error?.payload", self.testers)
        self.assertNotIn("% del umbral", self.testers)
        self.assertNotIn("candidate.reasons", self.testers)
        self.assertNotIn("identity-decision", self.testers)
        self.assertIn('data-candidate-action="alias"', self.resolver_result)
        self.assertIn('data-candidate-action="forced"', self.resolver_result)
        self.assertIn("item?.path", self.resolver_result)
        self.assertIn("resolverControlLabels", self.resolver_result)
        self.assertIn("Configurado", self.resolver_result)
        self.assertIn("Aplicado", self.resolver_result)
        self.assertIn("Ventaja sobre el segundo", self.resolver_result)
        self.assertIn("Diagnóstico técnico", self.resolver_result)
        self.assertIn("ui.bindCandidateActions();", self.testers)
        self.assertIn("if (state.activeTest)", self.testers)
        self.assertNotIn('id="identity-test-result" aria-live', self.testers)
        self.assertIn('role="status" aria-live="polite"', self.view)

    def test_resolver_renderer_covers_all_human_result_families(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            global.window = {};
            global.location = { hash: "#identidad/comun/resolver" };
            global.localStorage = { getItem: () => null, setItem: () => {} };
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
            const ui = window.ArrIdentityUI;
            ui.state.document = { schema: { resolver: { groups: [{ controls: [
              { path: "resolver.scoring.title_exact", label: "Titulo exacto" },
              { path: "resolver.scoring.title_similarity_max", label: "Similitud de titulo" },
              { path: "resolver.scoring.season_invalid", label: "Temporada imposible" },
              { path: "resolver.acceptance.min_score", label: "Puntuacion minima" },
              { path: "resolver.acceptance.min_margin", label: "Margen minimo" }
            ] }] } } };
            const context = { requestId: 7, category: "movies", parserTitle: "Titulo", parserYear: "2024" };
            const candidate = {
              tmdb_id: 10, title: "Titulo <seguro>", original_title: "Original", year: 2024, score: 55,
              matching_rules: [
                { path: "resolver.title_matching.roman_arabic_equivalence", detail: "III = 3" },
                { path: "resolver.title_matching.allow_omitted_part_number", detail: "Numero de saga omitido" },
                { path: "resolver.title_matching.score_parser_candidates", detail: "Titulo auxiliar del parser: Titulo <auxiliar>" }
              ],
              breakdown: [
                { key: "title_exact", path: "resolver.scoring.title_exact", configured: 35, applied: 35 },
                { key: "title_similarity_max", path: "resolver.scoring.title_similarity_max", configured: 20, applied: 20 }
              ]
            };
            const decision = (status, values = {}) => ({
              status, accepted: status === "ACCEPTED", has_scoring: true, bypass: false,
              score: 55, second_score: 20, min_score: 75, score_passed: false,
              margin: 35, min_margin: 12, margin_passed: true, source: "search", ...values
            });
            const render = payload => ui.renderResolverResult(payload, context);
            const requireText = (html, text) => { if (!html.includes(text)) throw new Error(`Falta ${text}`); };
            const rejectText = (html, text) => { if (html.includes(text)) throw new Error(`Sobra ${text}`); };

            const accepted = render({
              status: "ACCEPTED", ok: true,
              decision: decision("ACCEPTED", { score: 55, min_score: 50, score_passed: true, margin: 35, margin_passed: true }),
              candidates: [candidate],
              queries: [
                { endpoint: "/search/movie", params: { query: "Titulo", language: "es-ES", year: 2024 }, status_code: 200 },
                { endpoint: "/movie/10", params: { language: "es-ES" }, status_code: 200 }
              ]
            });
            ["ACEPTADA", "único candidato", "CUMPLIDA", "Titulo exacto", "Configurado", "Aplicado", "+35", "Segundo candidato", "No existe", "Reglas aplicadas", "Equivalencia romana aplicada: III = 3", "Número de saga omitido aceptado", "Título auxiliar utilizado: Titulo &lt;auxiliar&gt;", "2 consultas · 2 correctas", "Buscar película", "Idioma es-ES · Año 2024", "Correcta"].forEach(text => requireText(accepted, text));
            rejectText(accepted, "Ventaja sobre el segundo");
            rejectText(accepted, "RESUELTO POR IDIOMA");
            rejectText(accepted, "grupo ambiguo");
            rejectText(accepted, "% del umbral");
            rejectText(accepted, "HTTP 200");
            rejectText(accepted, "<seguro>");
            rejectText(accepted, "Titulo <auxiliar>");
            requireText(accepted, "Titulo &lt;seguro&gt;");

            const withoutRules = ui.renderResolverCandidate(
              { ...candidate, matching_rules: [] }, 0, context
            );
            rejectText(withoutRules, "Reglas aplicadas");

            const acceptedByLanguage = render({
              status: "ACCEPTED", ok: true,
              decision: decision("ACCEPTED", {
                score: 125, min_score: 75, score_passed: true,
                second_score: 125, has_second_candidate: true,
                margin: 0, min_margin: 12, margin_passed: false,
                original_language_preference: {
                  applied: true, enabled: true, language: "en", selected_original_language: "en"
                }
              }),
              candidates: [
                { ...candidate, title: "¡Canta!", original_title: "Sing", original_language: "en", score: 125 },
                { ...candidate, tmdb_id: 11, title: "Canta", original_title: "Mindenki", original_language: "hu", score: 125 }
              ]
            });
            ["ACEPTADA", "idioma original inglés", "grupo ambiguo", "RESUELTO POR IDIOMA"].forEach(text => requireText(acceptedByLanguage, text));
            rejectText(acceptedByLanguage, "RECHAZADA POR EMPATE");

            const acceptedByOldest = render({
              status: "ACCEPTED", ok: true,
              decision: decision("ACCEPTED", {
                score: 125, min_score: 75, score_passed: true,
                second_score: 125, has_second_candidate: true,
                margin: 0, min_margin: 12, margin_passed: false,
                oldest_exact_title_preference: {
                  applied: true, enabled: true, selected_year: 1979,
                  reason_code: "oldest_exact_title_without_year"
                }
              }),
              candidates: [
                { ...candidate, year: 1979, score: 125 },
                { ...candidate, tmdb_id: 11, year: 2024, score: 125 }
              ]
            });
            ["ACEPTADA", "año 1979 es el más antiguo", "RESUELTO POR MÁS ANTIGUA"].forEach(text => requireText(acceptedByOldest, text));
            rejectText(acceptedByOldest, "RECHAZADA POR EMPATE");

            const acceptedByBoth = render({
              status: "ACCEPTED", ok: true,
              decision: decision("ACCEPTED", {
                score: 125, min_score: 75, score_passed: true,
                second_score: 125, has_second_candidate: true,
                margin: 0, min_margin: 12, margin_passed: false,
                original_language_preference: {
                  applied: true, enabled: true, language: "en", selected_original_language: "en"
                },
                oldest_exact_title_preference: {
                  applied: true, enabled: true, selected_year: 1979,
                  reason_code: "oldest_exact_title_without_year"
                }
              }),
              candidates: [candidate, { ...candidate, tmdb_id: 11 }]
            });
            ["idioma original inglés", "RESUELTO POR IDIOMA"].forEach(text => requireText(acceptedByBoth, text));
            rejectText(acceptedByBoth, "RESUELTO POR MÁS ANTIGUA");
            rejectText(acceptedByBoth, "año 1979 es el más antiguo");

            const tie = render({
              status: "REJECTED_MARGIN", ok: true,
              decision: decision("REJECTED_MARGIN", { score: 125, score_passed: true, second_score: 125, has_second_candidate: true, margin: 0, margin_passed: false }),
              candidates: [{ ...candidate, score: 125 }, { ...candidate, tmdb_id: 11, score: 125 }]
            });
            ["RECHAZADA POR EMPATE", "misma puntuación", "NO CUMPLIDO"].forEach(text => requireText(tie, text));
            rejectText(tie, "REJECTED_MARGIN");

            const singleMargin = render({
              status: "REJECTED_MARGIN",
              decision: decision("REJECTED_MARGIN", { score: 10, min_score: 0, score_passed: true, second_score: 0, has_second_candidate: false, margin: 10, min_margin: 12, margin_passed: false }),
              candidates: [{ ...candidate, score: 10 }]
            });
            ["RECHAZADA POR MARGEN", "No existe un segundo candidato", "Segundo candidato", "No existe"].forEach(text => requireText(singleMargin, text));
            rejectText(singleMargin, "sobre el segundo");

            const both = render({ status: "REJECTED_SCORE", decision: decision("REJECTED_SCORE", { margin: 0, margin_passed: false }), candidates: [candidate] });
            requireText(both, "RECHAZADA POR PUNTUACIÓN Y MARGEN");

            const bypass = render({
              status: "ACCEPTED",
              decision: decision("ACCEPTED", { bypass: true, source: "forced_match" }),
              candidates: [candidate],
              queries: [{ endpoint: "/movie/10", status_code: 200 }]
            });
            ["coincidencia forzada validada", "NO APLICA", "1 consulta · 1 correcta"].forEach(text => requireText(bypass, text));
            rejectText(bypass, "1 consultas");

            const season = render({
              status: "REJECTED_SCORE",
              decision: decision("REJECTED_SCORE"),
              candidates: [{ ...candidate, breakdown: [{ key: "season_invalid", path: "resolver.scoring.season_invalid", configured: -100, applied: -100 }] }]
            });
            ["Temporada imposible", "-100"].forEach(text => requireText(season, text));

            const noCandidates = render({ status: "NO_CANDIDATES", decision: { status: "NO_CANDIDATES", has_scoring: false }, candidates: [] });
            requireText(noCandidates, "SIN CANDIDATOS");
            rejectText(noCandidates, "Puntuación obtenida");

            for (const [status, title] of [
              ["REJECTED", "IDENTIDAD NO SEGURA"],
              ["INVALID_RULES", "CONFIGURACIÓN NO VÁLIDA"],
              ["PARSER_ERROR", "ERROR DEL PARSER"],
              ["TMDB_UNAVAILABLE", "TMDB NO DISPONIBLE"],
              ["TMDB_ERROR", "CONSULTA TMDB RECHAZADA"],
              ["ORCHESTRATOR_UNAVAILABLE", "MOTOR NO DISPONIBLE"],
              ["INVALID_UPSTREAM_RESPONSE", "RESPUESTA DEL MOTOR NO VÁLIDA"],
              ["REQUEST_ERROR", "PRUEBA NO COMPLETADA"]
            ]) {
              const html = render({ status, decision: { status, has_scoring: false } });
              requireText(html, title);
              rejectText(html, "Puntuación obtenida");
            }
            for (const [reason_code, title] of [
              ["category_conflict", "CATEGORÍA CONTRADICTORIA"],
              ["category_not_resolvable", "CATEGORÍA NO RESOLUBLE"],
              ["empty_title", "TÍTULO NO IDENTIFICADO"],
              ["forced_target_invalid", "TMDB FORZADO NO VÁLIDO"],
              ["forced_type_mismatch", "TIPO FORZADO INCORRECTO"],
              ["forced_title_mismatch", "TÍTULO FORZADO INCORRECTO"],
              ["forced_year_mismatch", "AÑO FORZADO INCORRECTO"]
            ]) {
              requireText(render({ status: "REJECTED", details: { reason_code }, decision: { status: "REJECTED", has_scoring: false } }), title);
            }
            const invalid = render({ status: "INVALID_RULES", message: "Peso <incorrecto>", decision: { status: "INVALID_RULES", has_scoring: false } });
            requireText(invalid, "Motivo concreto:");
            requireText(invalid, "Peso &lt;incorrecto&gt;");
            rejectText(invalid, "Peso <incorrecto>");
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "resolver-result.js",
        )

    def test_tester_actions_are_bound_to_immutable_parser_context(self) -> None:
        self.assertIn("const context = Object.freeze({", self.testers)
        self.assertIn("result.parser_test?.result || result.parser_test", self.testers)
        self.assertIn("ui.beginTestRequest(section, profile)", self.testers)
        self.assertIn("ui.isCurrentTestRequest(request)", self.testers)
        self.assertIn("ui.invalidateTestResult(section, { profile })", self.testers)
        self.assertIn("`${parserTitle} | ${candidateTitle}`", self.testers)
        self.assertIn("`${parserTitle} | ${parserYear} | ${tmdbId}`", self.testers)
        self.assertIn("`${parserTitle} | ${tmdbId}`", self.testers)
        self.assertIn("No se puede forzar una película sin año", self.testers)
        self.assertNotIn("const raw = ui.state.testNames.resolver", self.testers)
        self.assertNotIn("`${dataset.title}", self.testers)

    def test_resolver_transport_failure_uses_the_human_result_flow(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            global.window = {};
            global.location = { hash: "#identidad/comun/resolver" };
            global.localStorage = { getItem: () => null, setItem: () => {} };
            const name = { value: "Titulo.2024.mkv", focus: () => {} };
            const category = { value: "movies" };
            const button = { disabled: false, textContent: "Probar título" };
            const resultBox = { innerHTML: "" };
            const statusBox = { className: "", textContent: "" };
            global.document = {
              getElementById: id => ({
                "identity-test-name": name,
                "identity-test-category": category,
                "identity-test-button": button,
                "identity-test-result": resultBox,
                "identity-status": statusBox
              }[id] || null),
              querySelectorAll: () => [],
              querySelector: () => null
            };
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[3], "utf8"));
            const ui = window.ArrIdentityUI;
            ui.state.section = "resolver";
            ui.state.draft = {};
            ui.state.document = { schema: { resolver: { groups: [] } } };
            ui.api = async () => { throw new Error("Red <cortada>"); };

            (async () => {
              await ui.runTitleTest();
              if (ui.state.lastResult.resolver?.status !== "REQUEST_ERROR") throw new Error("No se conservó el estado humano");
              if (!resultBox.innerHTML.includes("PRUEBA NO COMPLETADA")) throw new Error("No se pintó el resultado humano");
              if (!resultBox.innerHTML.includes("Red &lt;cortada&gt;")) throw new Error("No se mostró o escapó la causa");
              if (resultBox.innerHTML.includes("Error de prueba")) throw new Error("Reapareció el renderer antiguo");
              if (!statusBox.textContent.includes("PRUEBA NO COMPLETADA")) throw new Error("El resultado no se anunció");
              if (ui.state.activeTest !== null || button.disabled) throw new Error("La prueba no liberó el bloqueo");
              ui.api = async () => ({
                ok: true,
                status: "ACCEPTED",
                decision: { status: "ACCEPTED", accepted: true, has_scoring: false, bypass: true, source: "tmdb_id" },
                candidates: [],
                parser_test: { result: { title: "Titulo", year: 2024, category: "movies" } }
              });
              await ui.runTitleTest();
              if (!statusBox.textContent.includes("ACEPTADA")) throw new Error("La decisión correcta no se anunció");
              if (ui.state.activeTest !== null || button.disabled) throw new Error("La segunda prueba no liberó el bloqueo");
            })().catch(error => {
              console.error(error.stack || error);
              process.exitCode = 1;
            });
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "resolver-result.js",
            self.web / "static" / "js" / "limpieza-arr" / "testers.js",
        )

    def test_form_controls_and_subtabs_have_accessible_relationships(self) -> None:
        self.assertIn('for="${ui.esc(primaryId)}"', self.controls)
        self.assertIn('id="${ui.esc(controlId)}"', self.controls)
        self.assertIn('aria-describedby="${ui.esc(helpId)}"', self.controls)
        self.assertIn("ui.focusListPosition", self.controls)
        self.assertIn("ui.focusControl", self.controls)
        self.assertIn("ui.focusGroupReset", self.controls)
        self.assertIn('document.getElementById("identity-reset")?.focus()', self.view)
        self.assertIn('for="identity-test-name"', self.testers)
        self.assertIn('for="identity-test-category"', self.testers)
        self.assertIn('role="tabpanel"', self.view)
        self.assertIn('aria-controls="identity-panel-parser"', self.view)
        self.assertIn('aria-controls="identity-panel-resolver"', self.view)
        self.assertIn('class="identity-tabpanel"', self.view)
        self.assertIn("ArrowLeft", self.view)
        self.assertIn("ArrowRight", self.view)
        self.assertIn(".identity-sr-only", self.styles)
        self.assertIn(".identity-tabpanel[hidden]", self.styles)

    def test_only_one_title_test_can_remain_active_across_subtabs(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            global.window = {};
            global.location = { hash: "#identidad/comun/parser" };
            global.localStorage = { getItem: () => null, setItem: () => {} };
            const name = { value: "Blade.Runner.1982.1080p", focus: () => {} };
            const category = { value: "movies" };
            const button = { disabled: false, textContent: "Probar título" };
            const resultBox = { innerHTML: "" };
            global.document = {
              getElementById: id => ({
                "identity-test-name": name,
                "identity-test-category": category,
                "identity-test-button": button,
                "identity-test-result": resultBox
              }[id] || null),
              querySelectorAll: () => [],
              querySelector: () => null
            };
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));

            const ui = window.ArrIdentityUI;
            ui.state.section = "parser";
            ui.state.draft = {};
            let resolveRequest;
            let fetchCount = 0;
            ui.api = () => {
              fetchCount += 1;
              return new Promise(resolve => { resolveRequest = resolve; });
            };

            (async () => {
              const first = ui.runTitleTest();
              await Promise.resolve();
              ui.state.section = "resolver";
              location.hash = "#identidad/comun/resolver";
              await ui.runTitleTest();
              ui.invalidateTestResult("parser", { updateDom: false });
              await ui.runTitleTest();
              if (fetchCount !== 1) throw new Error(`Se lanzaron ${fetchCount} pruebas solapadas`);
              if (!button.disabled) throw new Error("El botón se reactivó con la petición aún pendiente");
              if (!ui.state.activeTest) throw new Error("El lock global se liberó antes de tiempo");
              resolveRequest({ result: { title: "Blade Runner", category: "movies" } });
              await first;
              if (ui.state.activeTest !== null) throw new Error("El lock global no se liberó al terminar");
              if (button.disabled) throw new Error("El tester visible no se reactivó al terminar");
              if (ui.state.lastResult.parser !== null) throw new Error("Se pintó un resultado invalidado");
            })().catch(error => {
              console.error(error.stack || error);
              process.exitCode = 1;
            });
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "testers.js",
        )

    def test_late_async_route_cannot_overwrite_the_current_view(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const listeners = {};
            const fakeApp = { innerHTML: "" };
            const fakeTitle = { textContent: "" };
            const tabButtons = ["identidad", "limpieza-peliculas", "limpieza-series", "ajustes", "motor", "historial", "revision", "informes"].map(view => ({
              dataset: { view },
              classList: { toggle: () => {} },
              addEventListener: () => {}
            }));
            global.location = { hash: "#motor", href: "" };
            global.localStorage = { getItem: () => null, setItem: () => {} };
            global.document = {
              getElementById: id => id === "app" ? fakeApp : id === "title" ? fakeTitle : null,
              querySelectorAll: selector => selector === ".tabs button" ? tabButtons : [],
              addEventListener: () => {}
            };
            global.window = {
              ArrIdentityUI: { show: async () => {} },
              addEventListener: (name, callback) => { listeners[name] = callback; }
            };
            let resolveStatus;
            let resolveJobs;
            const statusPending = new Promise(resolve => { resolveStatus = resolve; });
            const jobsPending = new Promise(resolve => { resolveJobs = resolve; });
            const response = payload => ({ ok: true, status: 200, text: async () => JSON.stringify(payload) });
            global.fetch = path => {
              if (path === "/api/status") return statusPending.then(response);
              if (path === "/api/jobs") return jobsPending.then(response);
              if (path === "/api/review?profile=movies") return Promise.resolve(response({ items: [], review_dir: "<REVIEW>" }));
              throw new Error(`Fetch inesperado: ${path}`);
            };

            (async () => {
              vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
              location.hash = "#revision";
              listeners.hashchange();
              await new Promise(resolve => setImmediate(resolve));
              resolveStatus({ orchestrator: {}, media_worker: {}, paths: {} });
              resolveJobs({ jobs: [] });
              await new Promise(resolve => setImmediate(resolve));
              await new Promise(resolve => setImmediate(resolve));
              if (fakeTitle.textContent !== "Revisión") throw new Error(`Título obsoleto: ${fakeTitle.textContent}`);
              if (!fakeApp.innerHTML.includes("repetidas_vs_error")) throw new Error("La vista Revision no quedó visible");
              if (fakeApp.innerHTML.includes("Rutas vivas")) throw new Error("Motor sobrescribió la ruta actual");
            })().catch(error => {
              console.error(error.stack || error);
              process.exitCode = 1;
            });
            """,
            self.web / "static" / "js" / "panel.js",
        )

    def test_responsive_styles_contain_wide_tables_without_page_overflow(self) -> None:
        self.assertIn("overflow-x: auto", self.styles)
        self.assertIn("min-width: 0", self.styles)
        self.assertIn("@media (max-width: 520px)", self.styles)
        self.assertIn("@media (max-width: 460px)", self.styles)
        self.assertIn(".identity-toolbar .identity-status { flex: 0 0 auto; }", self.styles)
        self.assertIn("flex-direction: column", self.styles)
        self.assertNotIn("100vw", self.styles)


if __name__ == "__main__":
    unittest.main()
