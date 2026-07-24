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
