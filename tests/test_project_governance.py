from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_root_readme_declares_arr_truth_source_and_entrypoints():
    text = read("README.md")

    for expected in (
        "ARR_PANEL_URL",
        "config/arr-orchestrator/orchestrator.db",
        "job_events",
        "job_detail()",
        "diagnostics/arr",
        "diagnosticos_codex",
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
    assert "compileall" in hook
    assert "pytest" in hook
    assert "requirements-dev.txt" in workflow
    assert "python -m pytest -q" in workflow
    assert "@jodiazhidalgo-collab" in codeowners
