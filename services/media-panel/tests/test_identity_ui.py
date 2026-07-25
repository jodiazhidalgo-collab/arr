import unittest
from pathlib import Path

from media_panel import server


class IdentityUiStaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web = Path(server.__file__).resolve().parent / "web"
        cls.index = (cls.web / "index.html").read_text(encoding="utf-8")
        cls.panel = (cls.web / "static" / "js" / "panel.js").read_text(
            encoding="utf-8"
        )
        js_root = cls.web / "static" / "js" / "limpieza-arr"
        css_root = cls.web / "static" / "css" / "limpieza-arr"
        cls.utils = (js_root / "utils.js").read_text(encoding="utf-8")
        cls.controls = (js_root / "controls.js").read_text(encoding="utf-8")
        cls.testers = (js_root / "testers.js").read_text(encoding="utf-8")
        cls.view = (js_root / "view.js").read_text(encoding="utf-8")
        cls.styles = "\n".join(
            (css_root / name).read_text(encoding="utf-8")
            for name in ("layout.css", "controls.css", "tester.css")
        )

    def test_top_tab_and_modular_assets_are_loaded_in_order(self) -> None:
        self.assertLess(
            self.index.index('data-view="limpieza-arr"'),
            self.index.index('data-view="reglas"'),
        )
        for asset in (
            "/static/css/limpieza-arr/layout.css",
            "/static/css/limpieza-arr/controls.css",
            "/static/css/limpieza-arr/tester.css",
            "/static/js/limpieza-arr/utils.js",
            "/static/js/limpieza-arr/controls.js",
            "/static/js/limpieza-arr/testers.js",
            "/static/js/limpieza-arr/view.js",
        ):
            self.assertIn(asset, self.index)
        self.assertLess(
            self.index.index("/static/js/limpieza-arr/view.js"),
            self.index.index("/static/js/panel.js"),
        )

    def test_hash_and_dirty_draft_survive_subtab_navigation(self) -> None:
        self.assertIn('`#limpieza-arr/${section}`', self.view)
        self.assertIn("arr-identity-section", self.view)
        self.assertIn("ui.state.section = section;", self.view)
        self.assertNotIn("ui.state.draft = ui.clone(ui.state.document.rules);\n    rememberSection", self.view)
        self.assertIn('window.addEventListener("beforeunload"', self.view)
        self.assertIn("confirmDraftLoss", self.view)
        self.assertIn("arr-identity-open-", self.controls)

    def test_save_reset_import_and_cache_contracts_are_explicit(self) -> None:
        self.assertIn('const API_ROOT = "/api/identity-rules";', self.view)
        self.assertIn("expected_revision: Number(ui.state.document.revision || 0)", self.view)
        self.assertIn("ui.state.draft = ui.clone(ui.state.document.defaults);", self.view)
        self.assertIn("Pulsa Guardar para aplicarlos", self.view)
        self.assertIn("const MAX_IMPORT_BYTES = 4 * 1024 * 1024;", self.view)
        self.assertIn("El JSON supera el límite de 4 MB", self.view)
        self.assertIn("`${API_ROOT}/cache/clear`", self.view)
        self.assertIn("Tu borrador se conserva", self.view)
        self.assertIn("payload.repair_required", self.view)
        self.assertIn("Boolean(ui.state.document.repair_required)", self.view)

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

    def test_title_testers_use_unsaved_draft_and_show_score_breakdown(self) -> None:
        self.assertIn("rules: ui.state.draft", self.testers)
        self.assertIn("/api/identity-rules/test-${section}", self.testers)
        self.assertIn("Probar título", self.testers)
        self.assertIn("% del umbral", self.testers)
        self.assertIn("candidate.reasons", self.testers)
        self.assertIn('data-candidate-action="alias"', self.testers)
        self.assertIn('data-candidate-action="forced"', self.testers)
        self.assertIn("ui.bindCandidateActions();", self.testers)

    def test_responsive_styles_contain_wide_tables_without_page_overflow(self) -> None:
        self.assertIn("overflow-x: auto", self.styles)
        self.assertIn("min-width: 0", self.styles)
        self.assertIn("@media (max-width: 520px)", self.styles)
        self.assertIn("@media (max-width: 460px)", self.styles)
        self.assertNotIn("100vw", self.styles)


if __name__ == "__main__":
    unittest.main()
