from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_root_readme_stays_minimal_and_points_to_review_docs():
    text = read("README.md")

    for expected in (
        "AGENTS.md",
        "docs/AI_REVIEW.md",
        "README_DIAGNOSTICO_CODEX.md",
    ):
        assert expected in text

    for hidden_from_front_page in (
        "ARR_PANEL_URL",
        "services/arr-orchestrator",
        "config/arr-orchestrator/orchestrator.db",
        "job_events",
        "job_detail()",
        "diagnostics/arr",
        "diagnosticos_codex",
    ):
        assert hidden_from_front_page not in text


def test_ai_review_declares_arr_truth_source_and_entrypoints():
    text = read("docs/AI_REVIEW.md")

    for expected in (
        "ARR_PANEL_URL",
        "services/arr-orchestrator",
        "services/buscador-puente-arr",
        "services/media-panel",
        "services/media-worker",
        "config/arr-orchestrator/orchestrator.db",
        "job_events",
        "job_detail()",
        "diagnostics/arr",
        "diagnosticos_codex",
        "AGENTS.md",
    ):
        assert expected in text


def test_diagnostic_readme_is_bridge_not_parallel_norm():
    text = read("README_DIAGNOSTICO_CODEX.md")

    assert "puente rapido" in text
    assert "AGENTS.md" in text
    assert "Seguridad de exportacion" in text
    assert "sin tokens" in text
    assert "<CODEX_DIAGS>" in text
    assert "<ARR_ROOT_WIN>" in text
    assert "related_files.json" in text
    assert "config_snapshot" in text

def test_gitignore_keeps_runtime_backups_and_diagnostics_out():
    text = read(".gitignore")

    for expected in (
        "backups/",
        "backups - copia/",
        "_codex_runtime/*",
        "config/",
        "diagnosticos_codex/",
        "diagnostics/",
        "*.zip",
    ):
        assert expected in text


def test_git_hooks_and_ci_are_present():
    hook = read(".githooks/pre-commit")
    workflow = read(".github/workflows/ci.yml")
    codeowners = read(".github/CODEOWNERS")

    assert "git diff --cached --check" in hook
    assert "compileall -q conftest.py services tests" in hook
    assert "node --check services/media-panel/media_panel/web/static/js/panel.js" in hook
    assert "pytest" in hook
    assert "requirements-dev.txt" in workflow
    assert "python -m pytest -q" in workflow
    assert "compileall -q conftest.py services tests" in workflow
    assert "node --check services/media-panel/media_panel/web/static/js/panel.js" in workflow
    assert "windows-latest" in workflow
    assert "ubuntu-latest" in workflow
    assert "pytest-junit-${{ matrix.os }}.xml" in workflow
    assert "arr-pytest-evidence-${{ matrix.os }}" in workflow
    assert "actions/checkout@v6" in workflow
    assert "actions/setup-python@v6" in workflow
    assert "actions/setup-node@v6" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "@jodiazhidalgo-collab" in codeowners


def test_review_docs_match_the_validation_contract():
    ai_review = read("docs/AI_REVIEW.md")
    evidence = read("docs/evidencia-pytest-y-validacion-local.md")

    for text in (ai_review, evidence):
        assert "compileall -q conftest.py services tests" in text
        assert "node --check services/media-panel/media_panel/web/static/js/panel.js" in text
        assert "arr-pytest-evidence-windows-latest" in text
        assert "arr-pytest-evidence-ubuntu-latest" in text


def test_ui_checker_supports_a_non_mutating_cas_and_report_error_pass():
    script = read(".agents/skills/playwright-ui-check-arr/scripts/ui_check.ps1")

    for expected in (
        "[switch]$ReadOnly",
        'const readOnly = process.argv[7] === "1";',
        "simulated_swap_without_server_write",
        "ARR_UI_CAS_RELOAD_ENABLED_DURING_SAVE",
        "runReportFailure(browser)",
        "runReadOnlyGuardSelfTest()",
        "handleReadOnlyRoute(route, label)",
        "result.context_wiring",
        "ARR_UI_REPORT_ERROR_HIDDEN",
        'read_only_verified: true',
        'context.route("**/*"',
        "ARR_UI_READ_ONLY_API_MUTATION",
        'serviceWorkers: "block"',
        "acceptDownloads: false",
        "ARR_UI_READ_ONLY_FORBIDDEN_ENDPOINT",
        "sanitizedRequestTarget",
        "bucket.expected_report_error",
        "read_only_mutation_attempt_count",
        "health_atomic_preflight_allowed",
    ):
        assert expected in script
    assert "route.continue()" not in script
    assert script.count("route.fallback()") >= 3
    assert "reportFailure = await runReportFailure(browser);" in script
    assert (
        "$runner $PanelUrl $artifactDir $TimeoutMs $keep $Browser $readOnlyArg 2>&1"
        in script
    )
    assert '$readOnlyArg = if ($ReadOnly) { "1" } else { "0" }' in script
    assert "$readOnly = if ($ReadOnly)" not in script
    main = script.split('(async () => {', 1)[1]
    assert main.index("readOnlyGuard = await runReadOnlyGuardSelfTest();") < main.index(
        "const launched = await launchBrowser();"
    )
    read_only_guard = script.split("async function checkedBrowserContext", 1)[1].split(
        "function createObservationBucket", 1
    )[0]
    assert "url: request.url()" not in read_only_guard
    observer = script.split("function attachObservers", 1)[1].split(
        "function blockingNetworkEvents", 1
    )[0]
    assert "url:" not in observer
    assert "/\\b409\\b/.test(text)" in observer
    assert "/\\b503\\b/.test(text)" in observer
    assert "path: sanitizedRequestTarget(rawLocation && rawLocation.url)" in observer
    assert "must_not_escape" in script
    assert "result.attempts.length === 4" in script
    guard_self_test = script.split("async function runReadOnlyGuardSelfTest", 1)[1].split(
        "async function endpointChecks", 1
    )[0]
    assert "page." not in guard_self_test
    report_failure = script.split("async function runReportFailure", 1)[1].split(
        "async function launchBrowser", 1
    )[0]
    init_script = report_failure.split("await page.addInitScript(() => {", 1)[1].split(
        "});", 1
    )[0]
    assert "try {" in init_script
    assert 'localStorage.setItem("arr-media-panel-informes-profile", "series");' in init_script
    assert "} catch {" in init_script
