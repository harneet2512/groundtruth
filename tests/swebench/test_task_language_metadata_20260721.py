"""RED-first WIDE-13 tests: mixed-language task selection is metadata-driven."""

from __future__ import annotations

from pathlib import Path

from scripts.swebench.task_language import derive_task_language

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "swebench_live_lite_full.yml"


def test_declared_language_wins_over_patch_shape() -> None:
    task = {"repo_language": "go", "patch": "diff --git a/main.py b/main.py\n"}
    assert derive_task_language(task) == "go"


def test_patch_metadata_derives_non_python_language() -> None:
    task = {
        "repo": "example/mixed",
        "patch": "diff --git a/cmd/main.go b/cmd/main.go\n+package main\n",
        "test_patch": "diff --git a/cmd/main_test.go b/cmd/main_test.go\n",
    }
    assert derive_task_language(task) == "go"


def test_unknown_task_language_is_not_python_default() -> None:
    assert derive_task_language({"repo": "unknown/repo", "patch": "docs only"}) is None


def test_live_lite_workflow_uses_authoritative_deriver() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "from task_language import derive_task_language" in text
    assert 'r["repo_language"] = "python"' not in text


def test_mixed_patch_tie_is_deterministic() -> None:
    task = {
        "patch": "diff --git a/a.ts b/a.ts\ndiff --git a/b.go b/b.go\n",
    }
    assert derive_task_language(task) == "go"
