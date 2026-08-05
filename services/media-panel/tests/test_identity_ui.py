import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path
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

    def test_favicon_is_published_and_linked(self) -> None:
        favicon = self.web / "static" / "favicon.ico"
        self.assertTrue(favicon.is_file())
        self.assertGreater(favicon.stat().st_size, 0)
        self.assertIn(
            '<link rel="icon" href="/favicon.ico?v=20260731" type="image/x-icon">',
            self.index,
        )

        handler = object.__new__(server.Handler)
        handler.path = "/favicon.ico"
        with patch.object(server.Handler, "_send") as send:
            server.Handler.do_GET(handler)
        send.assert_called_once_with(200, favicon.read_bytes(), "image/x-icon")

    def test_batches_are_grouped_with_human_progress_and_persistent_view(self) -> None:
        for marker in (
            "function groupedJobs(jobs)",
            "Lote detectado · ${total} vídeos",
            "Procesando capítulo ${Number(active?.batch?.index || 0)} de ${total}",
            "Ver capítulos e incidencias",
            "function bindBatchDetails()",
            "restorePanelScroll(\"motor\")",
            "restorePanelScroll(\"historial\")",
            "arr-media-panel-batch-open",
        ):
            self.assertIn(marker, self.panel)
        self.assertIn(".batch-child.has-incident", self.panel_styles)
        self.assertIn("overflow-wrap: anywhere", self.panel_styles)

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
        self.assertIn("arr-identity-scroll-${profile}-${section}", self.view)
        self.assertIn("ui.storeScrollPosition", self.view)
        self.assertIn("ui.restoreScrollPosition", self.view)
        self.assertIn("ui.states", self.utils)
        self.assertIn("requestEpoch", self.utils)
        self.assertIn("ui.storageGet", self.controls)
        self.assertIn("ui.storageSet", self.controls)
        self.assertNotIn("localStorage.setItem", self.controls)

    def test_common_profile_is_explicitly_marked_as_shared(self) -> None:
        self.assertIn("Compartido: afecta realmente a Películas y Series", self.view)
        self.assertIn('state.profile === "common"', self.view)
        self.assertIn('profile !== "common" && section === "parser"', self.view)
        self.assertIn("profileControl(profile, path)", self.view)

    def test_v2_profile_filter_routes_and_scroll_are_kept_exactly(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const values = new Map();
            let restored = null;
            global.location = { hash: "#identidad/peliculas/parser" };
            global.history = { replaceState: () => {} };
            global.localStorage = {
              getItem: key => values.has(key) ? values.get(key) : null,
              setItem: (key, value) => values.set(key, String(value))
            };
            global.document = {
              documentElement: { scrollTop: 0 },
              getElementById: () => null,
              querySelectorAll: () => []
            };
            global.window = {
              scrollY: 321,
              addEventListener: () => {},
              requestAnimationFrame: callback => { callback(); return 1; },
              scrollTo: options => { restored = options; },
              confirm: () => true
            };
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
            const ui = window.ArrIdentityUI;
            const controls = [
              "resolver.algorithm.mode",
              "resolver.coverage.max_candidates",
              "resolver.locales.movies.language",
              "resolver.aliases.movies",
              "resolver.forced_matches.movies",
              "resolver.movies.year.required",
              "resolver.locales.tv.language",
              "resolver.aliases.tv",
              "resolver.forced_matches.tv",
              "resolver.tv.season.required"
            ].map(path => ({ path, type: "toggle", label: path }));
            const documentState = {
              schema: {
                parser: { title: "Parser", groups: [{ id: "parser", controls: [{ path: "parser.extensions" }] }] },
                resolver: { title: "Resolver", groups: [{ id: "resolver", controls }] }
              }
            };
            const paths = profile => ui.sectionSchemaForProfile(documentState, "resolver", profile)
              .groups.flatMap(group => group.controls.map(control => control.path));
            const common = paths("common");
            const movies = paths("movies");
            const tv = paths("tv");
            const exact = (actual, expected, label) => {
              if (JSON.stringify(actual) !== JSON.stringify(expected)) {
                throw new Error(`${label}: ${JSON.stringify(actual)}`);
              }
            };
            exact(common, [
              "resolver.algorithm.mode", "resolver.coverage.max_candidates",
              "resolver.locales.movies.language", "resolver.aliases.movies",
              "resolver.forced_matches.movies", "resolver.locales.tv.language",
              "resolver.aliases.tv", "resolver.forced_matches.tv"
            ], "common");
            exact(movies, ["resolver.movies.year.required"], "movies");
            exact(tv, ["resolver.tv.season.required"], "tv");
            if (ui.sectionSchemaForProfile(documentState, "parser", "movies").groups.length) {
              throw new Error("Parser apareció fuera de Común");
            }
            const target = ui.resolveTarget(location.hash);
            if (target.hash !== "#identidad/peliculas/resolver" || target.section !== "resolver") {
              throw new Error(`Ruta específica no canonizada: ${JSON.stringify(target)}`);
            }
            ui.renderedIdentityRoute = { profile: "common", section: "resolver" };
            ui.storeScrollPosition();
            if (values.get("arr-identity-scroll-common-resolver") !== "321") {
              throw new Error("Scroll no persistido por perfil y sección");
            }
            values.set("arr-identity-scroll-common-resolver", "456");
            ui.restoreScrollPosition("common", "resolver");
            if (restored?.top !== 456 || restored?.left !== 0) {
              throw new Error(`Scroll no restaurado: ${JSON.stringify(restored)}`);
            }
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "view.js",
        )

    def test_open_group_ids_migrate_from_v1_to_the_active_v2_scope(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const values = new Map([
              ["arr-identity-open-common-resolver", JSON.stringify(["resolver_search", "resolver_acceptance"])],
              ["arr-identity-open-movies-resolver", JSON.stringify(["resolver_scoring"])],
              ["arr-identity-open-tv-resolver", JSON.stringify([])]
            ]);
            global.location = { hash: "#identidad/comun/resolver" };
            global.localStorage = {
              getItem: key => values.has(key) ? values.get(key) : null,
              setItem: (key, value) => values.set(key, String(value))
            };
            global.window = {};
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
            const ui = window.ArrIdentityUI;
            ui.setActiveProfile("common");
            if (!ui.groupIsOpen("resolver", "resolver_coverage", false)
                || !ui.groupIsOpen("resolver", "resolver_adjudication", false)) {
              throw new Error("Los grupos abiertos de Common no migraron");
            }
            const common = JSON.parse(values.get("arr-identity-open-common-resolver"));
            if (common.includes("resolver_search") || !common.includes("resolver_coverage")) {
              throw new Error(`Persistencia Common no canonizada: ${JSON.stringify(common)}`);
            }
            ui.setActiveProfile("movies");
            if (!ui.groupIsOpen("resolver", "resolver_movies", false)) {
              throw new Error("El grupo de Películas no heredó el estado abierto");
            }
            ui.setActiveProfile("tv");
            if (ui.groupIsOpen("resolver", "resolver_tv", true)) {
              throw new Error("Una lista antigua vacía no debe abrir el grupo de Series");
            }
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "controls.js",
        )

    def test_watcher_deep_links_are_distinct_and_old_alias_migrates(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const values = new Map([
              ["arr-media-panel-ajustes-vigilante", "tv"],
              ["arr-media-panel-section-settings", "vigilantes"],
              ["arr-media-panel-route", "#ajustes/vigilantes"]
            ]);
            global.location = { hash: "" };
            global.history = { replaceState: () => {} };
            global.localStorage = {
              getItem: key => values.has(key) ? values.get(key) : null,
              setItem: (key, value) => values.set(key, value)
            };
            global.document = {
              getElementById: () => ({}),
              querySelectorAll: () => [],
              addEventListener: () => {}
            };
            global.window = {
              ArrIdentityUI: {
                identityRouteFromHash: () => null,
                resolveTarget: hash => hash === "#limpieza-arr/resolver"
                  ? { profile: "common", section: "resolver", hash: "#identidad/comun/resolver", legacy: true }
                  : null,
                show: async () => {}
              },
              addEventListener: () => {}
            };
            const source = fs.readFileSync(process.argv[1], "utf8")
              .replace(/\ndispatchRoute\(\);\s*$/, "");
            vm.runInThisContext(source + `
              const storedWatcher = canonicalRouteFromHash("#desconocido");
              values.set("arr-media-panel-route", "#reglas/trailers");
              const storedRules = canonicalRouteFromHash("#desconocido");
              values.set("arr-media-panel-route", "#limpieza-arr/resolver");
              const storedIdentity = canonicalRouteFromHash("#desconocido");
              globalThis.__watcherRoutes = {
                movies: exactCanonicalRoute("#ajustes/vigilante-peliculas"),
                series: exactCanonicalRoute("#ajustes/vigilante-series"),
                alias: canonicalRouteFromHash("#ajustes/vigilantes"),
                partial: canonicalRouteFromHash("#ajustes"),
                storedWatcher,
                storedRules,
                storedIdentity
              };
            `);
            const watcherResults = global.__watcherRoutes;
            const requireRoute = (route, hash, profile) => {
              if (route.hash !== hash || route.watcherProfile !== profile) {
                throw new Error(`Ruta incorrecta: ${JSON.stringify(route)}`);
              }
            };
            requireRoute(watcherResults.movies, "#ajustes/vigilante-peliculas", "movies");
            requireRoute(watcherResults.series, "#ajustes/vigilante-series", "tv");
            requireRoute(watcherResults.alias, "#ajustes/vigilante-series", "tv");
            requireRoute(watcherResults.partial, "#ajustes/vigilante-series", "tv");
            requireRoute(watcherResults.storedWatcher, "#ajustes/vigilante-series", "tv");
            if (watcherResults.storedRules.hash !== "#ajustes/trailers") {
              throw new Error(`Ruta #reglas almacenada no migrada: ${JSON.stringify(watcherResults.storedRules)}`);
            }
            if (watcherResults.storedIdentity.hash !== "#identidad/comun/resolver") {
              throw new Error(`Ruta #limpieza-arr almacenada no migrada: ${JSON.stringify(watcherResults.storedIdentity)}`);
            }
            """,
            self.web / "static" / "js" / "panel.js",
        )

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
            const payload = (profile, marker) => {
              const common = profile === "common";
              return {
                ok: true,
                profile,
                revision: 1,
                rules: common
                  ? { schema_version: 2, parser: { marker }, resolver: {} }
                  : { schema_version: 2, resolver: { [profile]: { marker } } },
                defaults: common
                  ? { schema_version: 2, parser: {}, resolver: {} }
                  : { schema_version: 2, resolver: { [profile]: {} } },
                schema: common
                  ? { parser: { groups: [] }, resolver: { groups: [] } }
                  : { resolver: { groups: [] } }
              };
            };
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
              if (ui.states.movies.draft.resolver.movies.marker !== "MOVIES") throw new Error("Movies perdió su documento");
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
        self.assertIn('`${API_ROOT}/${profile}/reset`', self.view)
        self.assertIn('body: JSON.stringify({ expected_revision:', self.view)
        self.assertIn("Valores de fábrica restablecidos y activos", self.view)
        self.assertIn("const MAX_IMPORT_BYTES = 4 * 1024 * 1024;", self.view)
        self.assertIn("El JSON supera el límite de 4 MB", self.view)
        self.assertIn('const EXPORT_FORMAT = "arr-identity-export-v2";', self.view)
        self.assertIn("parsed.format !== EXPORT_FORMAT", self.view)
        self.assertIn("parsed.profile !== profile", self.view)
        self.assertIn("parsed.schema_version !== ACTIVE_SCHEMA_VERSION", self.view)
        self.assertIn("rules.schema_version !== ACTIVE_SCHEMA_VERSION", self.view)
        self.assertIn("base_revision: state.document.revision", self.view)
        self.assertIn("base_fingerprint: state.document.fingerprint", self.view)
        self.assertIn('`${API_ROOT}/${profile}/validate`', self.view)
        self.assertIn("validImportValidation(validated, profile)", self.view)
        self.assertIn("`${API_ROOT}/${profile}/cache/clear`", self.view)
        self.assertIn("Tu borrador se conserva", self.view)
        self.assertIn("payload.repair_required", self.view)
        self.assertIn("validRulesDocument(payload, profile)", self.view)
        self.assertIn("validCachePayload(payload)", self.view)
        self.assertIn("ui.isProfileActive(profile)", self.view)
        self.assertIn('body: "{}"', self.view)

    def test_reset_uses_real_endpoint_and_import_validates_v2_before_apply(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const values = new Map();
            const statusBox = { className: "", textContent: "" };
            const resetButton = { disabled: false, textContent: "", focus: () => {} };
            global.location = { hash: "#identidad/comun/resolver" };
            global.history = { replaceState: () => {} };
            global.localStorage = {
              getItem: key => values.has(key) ? values.get(key) : null,
              setItem: (key, value) => values.set(key, String(value))
            };
            global.document = {
              documentElement: { scrollTop: 0 },
              getElementById: id => id === "identity-status" ? statusBox : id === "identity-reset" ? resetButton : null,
              querySelectorAll: () => []
            };
            global.window = {
              scrollY: 0,
              addEventListener: () => {},
              scrollTo: () => {},
              confirm: () => true
            };
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
            const ui = window.ArrIdentityUI;
            ui.setActiveProfile("common");
            const state = ui.state;
            const rules = marker => ({ schema_version: 2, parser: { marker }, resolver: {} });
            const payload = (revision, marker) => ({
              ok: true,
              profile: "common",
              revision,
              rules: rules(marker),
              defaults: rules("DEFAULT"),
              schema: { parser: { groups: [] }, resolver: { groups: [] } }
            });
            state.document = payload(4, "ACTIVE");
            state.draft = rules("BORRADOR");
            state.dirty = true;
            state.section = "resolver";
            let renders = 0;
            ui.render = () => { renders += 1; };
            const calls = [];
            ui.api = async (path, options) => {
              const body = JSON.parse(options.body);
              calls.push({ path, body });
              if (path === "/api/identity-rules/common/validate") {
                return {
                  ok: true,
                  format: "arr-identity-export-v2",
                  schema_version: 2,
                  profile: "common",
                  rules: body.rules,
                  fingerprint: `sha256:${"1".repeat(64)}`,
                  effective_fingerprint: `sha256:${"2".repeat(64)}`
                };
              }
              return payload(5, "RESET");
            };

            (async () => {
              await ui.resetRules();
              if (calls.length !== 1 || calls[0].path !== "/api/identity-rules/common/reset") {
                throw new Error(`Reset no usó endpoint real: ${JSON.stringify(calls)}`);
              }
              if (calls[0].body.expected_revision !== 4) throw new Error("Reset sin CAS");
              if (state.document.revision !== 5 || state.draft.parser.marker !== "RESET" || state.dirty) {
                throw new Error("Reset no aplicó la respuesta del motor");
              }
              if (!state.notice.message.includes("activos en el motor") || state.notice.tone !== "ok") {
                throw new Error("Reset no confirmó el resultado útil");
              }

              const importFile = parsed => ({ size: 100, text: async () => JSON.stringify(parsed) });
              const input = { dataset: { profile: "common" }, files: [], value: "selected" };
              const apply = async parsed => {
                input.files = [importFile(parsed)];
                await ui.importRules({ currentTarget: input });
              };
              const baseline = JSON.stringify(state.draft);
              await apply({ format: "otro", profile: "common", schema_version: 2, rules: rules("MAL") });
              if (JSON.stringify(state.draft) !== baseline) throw new Error("Se aplicó formato inválido");
              await apply({ format: "arr-identity-export-v2", profile: "tv", schema_version: 2, rules: rules("MAL") });
              if (JSON.stringify(state.draft) !== baseline) throw new Error("Se aplicó perfil incorrecto");
              await apply({ format: "arr-identity-export-v2", profile: "common", schema_version: 1, rules: rules("MAL") });
              if (JSON.stringify(state.draft) !== baseline) throw new Error("Se aplicó esquema incorrecto");
              await apply({
                format: "arr-identity-export-v2",
                profile: "common",
                schema_version: 2,
                rules: rules("IMPORTADO")
              });
              if (state.draft.parser.marker !== "IMPORTADO" || !state.dirty) {
                throw new Error("La importación v2 válida no se aplicó");
              }
              if (calls.length !== 2 || calls[1].path !== "/api/identity-rules/common/validate") {
                throw new Error(`La importación no fue validada por el motor: ${JSON.stringify(calls)}`);
              }
              if (!state.notice.message.includes("validado")) throw new Error("La importación válida no se confirmó");
              const acceptedDraft = JSON.stringify(state.draft);
              ui.api = async () => { throw new Error("Reglas incompatibles"); };
              await apply({
                format: "arr-identity-export-v2",
                profile: "common",
                schema_version: 2,
                rules: rules("NO-DEBE-ENTRAR")
              });
              if (JSON.stringify(state.draft) !== acceptedDraft) {
                throw new Error("Se aplicó el borrador antes de la validación del motor");
              }
              if (!state.notice.message.includes("No se pudo importar")) {
                throw new Error("El rechazo del motor no se explicó");
              }
              if (renders < 3) throw new Error("La vista no se actualizó");
            })().catch(error => {
              console.error(error.stack || error);
              process.exitCode = 1;
            });
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "view.js",
        )

    def test_reset_always_confirms_locks_and_restores_after_deferred_error(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            let confirmResult = false;
            let confirmCalls = 0;
            let focusCalls = 0;
            let importClicks = 0;
            const statusBox = { className: "", textContent: "" };
            const resetButton = { focus: () => { focusCalls += 1; } };
            const importInput = { click: () => { importClicks += 1; } };
            global.location = { hash: "#identidad/comun/resolver" };
            global.history = { replaceState: () => {} };
            global.localStorage = { getItem: () => null, setItem: () => {} };
            global.document = {
              documentElement: { scrollTop: 0 },
              getElementById: id => ({
                "identity-status": statusBox,
                "identity-reset": resetButton,
                "identity-import-file": importInput
              }[id] || null),
              querySelectorAll: () => []
            };
            global.window = {
              scrollY: 0,
              addEventListener: () => {},
              scrollTo: () => {},
              confirm: () => { confirmCalls += 1; return confirmResult; }
            };
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[3], "utf8"));
            const ui = window.ArrIdentityUI;
            ui.setActiveProfile("common");
            const state = ui.state;
            const rules = marker => ({ schema_version: 2, parser: { marker }, resolver: {} });
            const documentBefore = {
              ok: true,
              profile: "common",
              revision: 8,
              rules: rules("ACTIVO"),
              defaults: rules("DEFAULT"),
              schema: { parser: { groups: [] }, resolver: { groups: [] } }
            };
            const draftBefore = rules("BORRADOR");
            state.document = documentBefore;
            state.draft = draftBefore;
            state.dirty = false;
            state.section = "resolver";
            const renderStates = [];
            ui.render = () => { renderStates.push(state.resetting); };
            let requestCalls = 0;
            let rejectReset;
            ui.api = () => {
              requestCalls += 1;
              return new Promise((_resolve, reject) => { rejectReset = reject; });
            };

            (async () => {
              await ui.resetRules();
              if (confirmCalls !== 1 || requestCalls !== 0) {
                throw new Error("Reset limpio no confirmó o envió pese a cancelar");
              }
              confirmResult = true;
              const pending = ui.resetRules();
              await Promise.resolve();
              if (!state.resetting || requestCalls !== 1 || renderStates.at(-1) !== true) {
                throw new Error("El reset diferido no mantuvo el bloqueo");
              }
              const editable = ui.renderControl({
                path: "parser.marker", type: "text", label: "Marca", help: ""
              });
              if (!editable.includes("disabled")) throw new Error("Los controles siguieron editables durante reset");
              ui.markDirty();
              if (state.dirty) throw new Error("Un control mutó el borrador durante reset");
              await ui.saveRules();
              ui.openImport();
              if (requestCalls !== 1 || importClicks !== 0) {
                throw new Error("Guardar o importar se ejecutó durante reset");
              }
              const conflict = new Error("conflict");
              conflict.status = 409;
              conflict.payload = { error: "revision_conflict" };
              rejectReset(conflict);
              await pending;
              if (state.resetting || renderStates.at(-1) !== false) {
                throw new Error("Finally no desbloqueó y repintó la vista");
              }
              if (state.document !== documentBefore || state.draft !== draftBefore || state.dirty) {
                throw new Error("El error de reset no restauró el estado exacto");
              }
              if (state.notice.tone !== "bad" || !state.notice.message.includes("Otra ventana")) {
                throw new Error("El conflicto no quedó visible tras el rerender final");
              }
              if (focusCalls !== 1 || confirmCalls !== 2) {
                throw new Error("Reset no restauró foco o no confirmó siempre");
              }
            })().catch(error => {
              console.error(error.stack || error);
              process.exitCode = 1;
            });
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "controls.js",
            self.web / "static" / "js" / "limpieza-arr" / "view.js",
        )

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

    def test_real_ordered_tags_fixture_is_readonly_and_never_stringified(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            global.window = {};
            global.location = { hash: "#identidad/comun/resolver" };
            global.localStorage = { getItem: () => null, setItem: () => {} };
            global.document = { getElementById: () => null, querySelectorAll: () => [] };
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
            const ui = window.ArrIdentityUI;
            const path = "resolver.adjudication.tie_breakers";
            const ordered = ["year", "agreements", "disagreements", "popularity", "votes", "newest_year", "lower_tmdb_id"];
            ui.state.draft = {
              resolver: { adjudication: { tie_breakers: [...ordered] } }
            };
            ui.state.document = {
              defaults: { resolver: { adjudication: { tie_breakers: [...ordered] } } }
            };
            const control = {
              path,
              type: "ordered_tags",
              label: "Desempates",
              help: "Orden canónico de desempate.",
              readonly: true
            };
            const before = JSON.stringify(ui.state.draft);
            const html = ui.renderControl(control);
            if (!html.includes("identity-readonly-list") || !html.includes("<ol")) {
              throw new Error("ordered_tags no se mostró como lista ordenada");
            }
            for (const item of ordered) {
              if (!html.includes(`<li>${item}</li>`)) throw new Error(`Falta ${item}`);
            }
            for (const forbidden of ["<input", "<select", "data-identity-path", "data-reset-control", 'value="year,']) {
              if (html.includes(forbidden)) throw new Error(`El readonly quedó editable o serializado: ${forbidden}`);
            }
            if (JSON.stringify(ui.state.draft) !== before || !Array.isArray(ui.getPath(ui.state.draft, path))) {
              throw new Error("ordered_tags se convirtió en string al renderizar");
            }
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "controls.js",
        )
        self.assertIn(
            "filter(control => !ui.controlIsReadOnly(control))",
            self.controls,
        )

    def test_title_testers_use_unsaved_draft_and_human_resolver_contract(self) -> None:
        self.assertIn("const submittedRules = ui.clone(state.draft);", self.testers)
        self.assertIn("rules: submittedRules", self.testers)
        self.assertIn("${ui.identityApiRoot(profile)}/test-${section}", self.testers)
        self.assertIn("Probar título", self.testers)
        self.assertIn("error?.payload", self.testers)
        self.assertIn('data-candidate-action="alias"', self.resolver_result)
        self.assertIn('data-candidate-action="forced"', self.resolver_result)
        self.assertIn("ACCEPTED_CONFIDENT", self.resolver_result)
        self.assertIn("ACCEPTED_FALLBACK", self.resolver_result)
        self.assertIn("RETRY_PROVIDER", self.resolver_result)
        self.assertIn("BLOCKED_HARD", self.resolver_result)
        self.assertIn("AGREE", self.resolver_result)
        self.assertIn("DISAGREE", self.resolver_result)
        self.assertIn("UNKNOWN", self.resolver_result)
        self.assertIn("coverage_limited", self.resolver_result)
        self.assertIn("decision.phase_counts", self.resolver_result)
        self.assertIn('value("discovered")', self.resolver_result)
        self.assertIn('value("enriched")', self.resolver_result)
        self.assertIn('value("plausible")', self.resolver_result)
        self.assertIn('value("eliminated")', self.resolver_result)
        self.assertNotIn("No existe un segundo candidato", self.resolver_result)
        self.assertNotIn("puntos", self.resolver_result.lower())
        self.assertIn("Diagnóstico técnico", self.resolver_result)
        self.assertIn("ui.bindCandidateActions();", self.testers)
        self.assertIn("if (state.activeTest)", self.testers)
        self.assertNotIn('id="identity-test-result" aria-live', self.testers)
        self.assertIn('role="status" aria-live="polite"', self.view)

    def test_resolver_v2_renders_decisions_evidence_funnel_and_alternatives(self) -> None:
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
            ui.state.readOnly = false;
            const context = { requestId: 7, category: "movies", parserTitle: "Alien", parserYear: "1979" };
            const render = payload => ui.renderResolverResult(payload, context);
            const requireText = (html, value) => {
              if (!html.includes(value)) throw new Error(`Falta ${value}`);
            };
            const rejectText = (html, value) => {
              if (html.includes(value)) throw new Error(`Sobra ${value}`);
            };

            const confidentPayload = {
              ok: true,
              decision: {
                status: "ACCEPTED_CONFIDENT",
                accepted: true,
                selected: { tmdb_id: 348, media_type: "movie", title: "Alien", year: 1979 },
                evidence: [
                  { tmdb_id: 348, families: [
                    { family: "title", verdict: "AGREE", detail: "Título exacto" },
                    { family: "year", verdict: "AGREE", detail: { expected: 1979, candidate: [1979] } },
                    { family: "title", verdict: "DISAGREE", detail: "duplicada" }
                  ] },
                  { tmdb_id: 8077, families: [{ family: "title", verdict: "DISAGREE" }] }
                ],
                phase_counts: { discovered: 12, enriched: 6, plausible: 2, eliminated: 10 },
                counters: { discovered: 99, enriched: 99, plausible: 99, eliminated: 99 },
                alternatives: [
                  { tmdb_id: 348, media_type: "movie", title: "Alien", year: 1979 },
                  {
                    tmdb_id: 8077, media_type: "movie", title: "Alien³", year: 1992,
                    eliminated: true, elimination_reasons: ["year_conflict"],
                    evidence: [{ family: "year", verdict: "DISAGREE" }]
                  }
                ],
                coverage_limited: false
              },
              queries: [{ endpoint: "/search/movie", params: { query: "Alien" }, status_code: 200 }]
            };
            const confident = render(confidentPayload);
            ["Aceptada", "IDENTIDAD ELEGIDA", "Alien", "Coincide", "Esperado: 1979", "TMDb: 1979", "Descubiertos", "12", "Enriquecidos", "6", "Plausibles", "2", "Eliminados", "10", "Alternativas", "Alien³", "Eliminada", "Contradicciones", "El año no coincide.", "1 consulta · 1 correcta"].forEach(value => requireText(confident, value));
            if (ui.resolverEvidence(confidentPayload).length !== 2) throw new Error("La familia duplicada no se consolidó");
            rejectText(confident, "duplicada");
            rejectText(confident, "Cobertura limitada");
            rejectText(confident.toLowerCase(), "puntos");
            rejectText(confident, "Segundo candidato");
            rejectText(confident, ">AGREE<");
            rejectText(confident, ">99<");
            rejectText(confident, "year_conflict");
            rejectText(confident, '{"expected"');
            const aliasActions = (confident.match(/data-candidate-action="alias"/g) || []).length;
            const forcedActions = (confident.match(/data-candidate-action="forced"/g) || []).length;
            if (aliasActions !== 2 || forcedActions !== 2) {
              throw new Error(`El seleccionado perdió acciones: alias=${aliasActions}, forced=${forcedActions}`);
            }
            if (!confident.includes('data-tmdb-id="348"')) throw new Error("Las acciones no pertenecen al seleccionado");
            const ids = [...confident.matchAll(/(?:^|\s)id="([^"]+)"/g)].map(match => match[1]);
            if (new Set(ids).size !== ids.length) throw new Error(`IDs HTML duplicados: ${ids.join(",")}`);

            const fallback = render({
              ok: true,
              decision: {
                status: "ACCEPTED_FALLBACK",
                accepted: true,
                selected: { tmdb_id: 1399, media_type: "tv", title: "Juego de tronos", year: 2011 },
                evidence: {
                  title: { verdict: "AGREE", detail: "Coincide" },
                  season: { verdict: "UNKNOWN", detail: "TMDb incompleto" }
                },
                counters: {
                  candidates_discovered: 60,
                  candidates_enriched: 8,
                  candidates_plausible: 3,
                  candidates_eliminated: 57
                },
                fallback_reason: "coverage_limited",
                coverage_limited: true,
                alternatives: [{ tmdb_id: 1399, media_type: "tv", title: "Juego de tronos", year: 2011 }]
              }
            });
            ["Aceptada eligiendo la más probable", "Se alcanzó el límite de cobertura", "No disponible", "Cobertura limitada", "60", "57"].forEach(value => requireText(fallback, value));

            const retryPayload = { ok: true, decision: { status: "RETRY_PROVIDER", accepted: false } };
            const retry = render(retryPayload);
            requireText(retry, "Pendiente por TMDb");
            if (ui.resolverPresentation(retryPayload).tone !== "warn") throw new Error("RETRY_PROVIDER no usa aviso");

            const blockedPayload = {
              ok: true,
              decision: {
                status: "BLOCKED_HARD",
                accepted: false,
                reason_message: "El año contradice la ficha.",
                evidence: [{ family: "year", verdict: "DISAGREE" }]
              }
            };
            const blocked = render(blockedPayload);
            ["Bloqueada por contradicción", "No coincide", "El año contradice la ficha."].forEach(value => requireText(blocked, value));
            if (ui.resolverPresentation(blockedPayload).tone !== "bad") throw new Error("ok:true pintó verde una decisión bloqueada");

            const historical = render({
              ok: true,
              status: "ACCEPTED",
              decision: { status: "ACCEPTED", accepted: true },
              candidates: [{ tmdb_id: 10, title: "Histórica", year: 1980, score: 55, breakdown: [{ applied: 55 }] }]
            });
            ["Aceptada", "resultado histórico", "Histórica"].forEach(value => requireText(historical, value));
            rejectText(historical, "55 puntos");
            rejectText(historical, "Segundo candidato");

            const escaped = render({
              ok: false,
              decision: { status: "BLOCKED_HARD", accepted: false, reason_message: "Dato <inseguro>" }
            });
            requireText(escaped, "Dato &lt;inseguro&gt;");
            rejectText(escaped, "Dato <inseguro>");
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "resolver-result.js",
        )

    def test_legacy_resolver_renderer_covers_all_human_result_families(self) -> None:
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
            ui.state.readOnly = true;
            const context = { requestId: 3, category: "movies", parserTitle: "Historica", parserYear: "1980" };
            const render = payload => ui.renderResolverResult(payload, context);
            const accepted = render({
              ok: true,
              status: "ACCEPTED",
              decision: { status: "ACCEPTED", accepted: true },
              candidates: [{
                tmdb_id: 10,
                media_type: "movie",
                title: "Histórica",
                year: 1980,
                score: 55,
                breakdown: [{ applied: 55 }]
              }]
            });
            if (!accepted.includes("Aceptada") || !accepted.includes("resultado histórico") || !accepted.includes("Histórica")) {
              throw new Error("El resultado ACCEPTED histórico dejó de ser legible");
            }
            for (const forbidden of ["55 puntos", "Segundo candidato", "Puntuación obtenida"]) {
              if (accepted.includes(forbidden)) throw new Error(`Reapareció UI v1: ${forbidden}`);
            }
            for (const status of ["REJECTED", "REJECTED_SCORE", "REJECTED_MARGIN"]) {
              const html = render({ ok: true, decision: { status, accepted: false } });
              if (!html.includes("Bloqueada (resultado histórico)")) {
                throw new Error(`No se mostró el bloqueo histórico ${status}`);
              }
            }
            const escaped = render({
              ok: false,
              decision: { status: "REQUEST_ERROR", accepted: false },
              message: "Fallo <histórico>"
            });
            if (!escaped.includes("Fallo &lt;histórico&gt;") || escaped.includes("Fallo <histórico>")) {
              throw new Error("La causa histórica no se escapó");
            }
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

    def test_candidate_actions_always_edit_the_loaded_common_draft(self) -> None:
        run_node_contract(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const statusBox = { className: "", textContent: "" };
            global.location = { hash: "#identidad/peliculas/resolver" };
            global.localStorage = { getItem: () => null, setItem: () => {} };
            global.document = {
              getElementById: id => id === "identity-status" ? statusBox : null,
              querySelector: () => null,
              querySelectorAll: () => []
            };
            global.window = {};
            vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
            vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
            const ui = window.ArrIdentityUI;
            ui.setActiveProfile("movies");
            const movieState = ui.state;
            movieState.document = { rules: { resolver: { movies: {} } } };
            movieState.draft = { resolver: { movies: {} } };
            const context = Object.freeze({ requestId: 9, category: "movies", parserTitle: "Obsession", parserYear: "2025" });
            movieState.testContext.resolver = context;
            let commonLoads = 0;
            let renders = 0;
            ui.render = () => { renders += 1; };
            ui.loadRules = async ({ profile }) => {
              if (profile !== "common") throw new Error("Se cargó un perfil incorrecto");
              commonLoads += 1;
              ui.states.common.document = { rules: {} };
              ui.states.common.draft = {
                resolver: {
                  aliases: { movies: [], tv: [] },
                  forced_matches: { movies: [], tv: [] }
                }
              };
            };

            (async () => {
              await ui.addCandidateRule({
                candidateAction: "alias", testRequestId: "9", candidateTitle: "Obsesión", tmdbId: "1436161"
              });
              const common = ui.states.common;
              if (commonLoads !== 1 || common.draft.resolver.aliases.movies[0] !== "Obsession | Obsesión") {
                throw new Error(`El alias no llegó a Common: ${JSON.stringify(common.draft)}`);
              }
              if (ui.getPath(movieState.draft, "resolver.aliases.movies") !== undefined || movieState.dirty) {
                throw new Error("La acción ensució el scope Películas");
              }
              if (!common.dirty || !statusBox.textContent.includes("Entra en Común y pulsa Guardar")) {
                throw new Error("No se informó dónde guardar la regla común");
              }
              await ui.addCandidateRule({
                candidateAction: "alias", testRequestId: "9", candidateTitle: "Obsesión", tmdbId: "1436161"
              });
              if (common.draft.resolver.aliases.movies.length !== 1) throw new Error("El alias se duplicó");
              await ui.addCandidateRule({
                candidateAction: "forced", testRequestId: "9", candidateTitle: "Obsesión", tmdbId: "1436161"
              });
              if (common.draft.resolver.forced_matches.movies[0] !== "Obsession | 2025 | 1436161") {
                throw new Error("El forzado no llegó a Common");
              }
              if (renders !== 0) throw new Error("Editar Common repintó la pestaña Películas");
            })().catch(error => {
              console.error(error.stack || error);
              process.exitCode = 1;
            });
            """,
            self.web / "static" / "js" / "limpieza-arr" / "utils.js",
            self.web / "static" / "js" / "limpieza-arr" / "testers.js",
        )

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
              if (!resultBox.innerHTML.includes("Prueba no completada")) throw new Error("No se pintó el resultado humano");
              if (!resultBox.innerHTML.includes("Red &lt;cortada&gt;")) throw new Error("No se mostró o escapó la causa");
              if (resultBox.innerHTML.includes("Error de prueba")) throw new Error("Reapareció el renderer antiguo");
              if (!statusBox.textContent.includes("PRUEBA NO COMPLETADA")) throw new Error("El resultado no se anunció");
              if (ui.state.activeTest !== null || button.disabled) throw new Error("La prueba no liberó el bloqueo");
              ui.api = async () => ({
                ok: true,
                status: "ACCEPTED_CONFIDENT",
                decision: {
                  status: "ACCEPTED_CONFIDENT",
                  accepted: true,
                  selected: { tmdb_id: 1, media_type: "movie", title: "Titulo", year: 2024 }
                },
                candidates: [],
                parser_test: { result: { title: "Titulo", year: 2024, category: "movies" } }
              });
              await ui.runTitleTest();
              if (!statusBox.textContent.includes("Aceptada")) throw new Error("La decisión correcta no se anunció");
              if (ui.state.activeTest !== null || button.disabled) throw new Error("La segunda prueba no liberó el bloqueo");
              ui.api = async () => ({
                ok: true,
                decision: { status: "BLOCKED_HARD", accepted: false },
                parser_test: { result: { title: "Titulo", year: 2024, category: "movies" } }
              });
              await ui.runTitleTest();
              if (!statusBox.textContent.includes("Bloqueada por contradicción")) throw new Error("La decisión bloqueada no se anunció");
              if (!statusBox.className.includes("bad") || statusBox.className.includes(" ok")) {
                throw new Error(`ok:true pintó verde una decisión bloqueada: ${statusBox.className}`);
              }
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
        self.assertIn('button?.focus()', self.view)
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
