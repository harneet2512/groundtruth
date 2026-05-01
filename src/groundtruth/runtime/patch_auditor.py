"""Canonical deterministic patch-shape auditor.

The auditor is intentionally repository-native and model-free. It reads the
current git diff and the v7 plan JSON, then emits the same shape for hooks,
tools, and benchmark reports.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
from pathlib import Path
from typing import Any

from groundtruth.runtime.repo_adapters import (
    is_generated_or_vendor,
    is_source_file,
    is_test_file,
)

ROOT_SCAFFOLD_PATTERNS = (
    # Python
    "*_test.py",
    "test_*.py",
    "*_demo.py",
    "demo_*.py",
    "*_verification.py",
    "final_*.py",
    "comprehensive_*.py",
    "repro*.py",
    "reproduce*.py",
    "minimal_*.py",
    # Go
    "*_test.go",
    "main_test.go",
    "repro*.go",
    "reproduce*.go",
    # JavaScript / TypeScript
    "*.spec.js",
    "*.spec.ts",
    "*.test.js",
    "*.test.ts",
    "repro*.js",
    "repro*.ts",
    "reproduce*.js",
    "reproduce*.ts",
    # Rust
    "repro*.rs",
    "reproduce*.rs",
    "test_*.rs",
    # Java / Kotlin
    "*Test.java",
    "Test*.java",
    "Repro*.java",
    "*Test.kt",
    "Repro*.kt",
    # Ruby / PHP / C#
    "*_spec.rb",
    "Repro*.cs",
)
FORBIDDEN_PATTERNS = ()


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def _load_plan(plan_path: str | None, plan: dict[str, Any] | None) -> dict[str, Any]:
    if plan is not None:
        return dict(plan)
    if not plan_path:
        return {}
    try:
        loaded = json.loads(Path(plan_path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _git_name_status(repo_root: str) -> list[tuple[str, str]]:
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, "diff", "--name-status", "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    rows: list[tuple[str, str]] = []
    for raw in proc.stdout.splitlines():
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        path = parts[-1].strip()
        if path:
            rows.append((status, _norm(path)))
    return rows


def _is_test_file(path: str) -> bool:
    return is_test_file(path)


def _is_source_file(path: str) -> bool:
    return is_source_file(path)


def _is_root_scaffold(status: str, path: str) -> bool:
    norm = _norm(path)
    return status.startswith("A") and "/" not in norm and any(
        fnmatch.fnmatch(norm, pattern) for pattern in ROOT_SCAFFOLD_PATTERNS
    )


def _matches_any(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    norm = _norm(path)
    return any(fnmatch.fnmatch(norm, _norm(pattern)) for pattern in patterns)


def _expected_missing(changed: set[str], expected: list[Any]) -> list[str]:
    missing: list[str] = []
    for item in expected:
        if isinstance(item, dict):
            pattern = str(item.get("path") or item.get("pattern") or "")
            required = bool(item.get("required", True))
        else:
            pattern = str(item)
            required = True
        if not pattern or not required:
            continue
        norm_pattern = _norm(pattern)
        if not any(path == norm_pattern or fnmatch.fnmatch(path, norm_pattern) for path in changed):
            missing.append(norm_pattern)
    return missing


def _focus_file(item: Any) -> str:
    if isinstance(item, dict):
        return _norm(str(item.get("file") or item.get("path") or ""))
    return _norm(str(item))


def audit_patch(
    repo_root: str,
    *,
    plan_path: str | None = None,
    plan: dict[str, Any] | None = None,
    name_status: list[tuple[str, str]] | None = None,
    test_result: dict[str, Any] | None = None,
    log_dir: str | None = None,
    task_id: str = "unknown",
) -> dict[str, Any]:
    """Classify the current patch shape against a v7 plan JSON."""
    loaded_plan = _load_plan(plan_path, plan)
    rows = name_status if name_status is not None else _git_name_status(repo_root)
    changed = {_norm(path) for _status, path in rows if path}

    cluster = {_norm(p) for p in loaded_plan.get("cluster_files", []) if p}
    focus_ranked = [
        file
        for item in loaded_plan.get("agent_focus_files", [])
        for file in [_focus_file(item)]
        if file
    ]
    focus = set(focus_ranked)
    expected = loaded_plan.get("expected_side_files", [])
    if not isinstance(expected, list):
        expected = []

    root_scaffolds = sorted(path for status, path in rows if _is_root_scaffold(status, path))
    root_scaffold_set = set(root_scaffolds)
    source_files = sorted(path for path in changed if path not in root_scaffold_set and _is_source_file(path))
    test_files = sorted(path for path in changed if path not in root_scaffold_set and _is_test_file(path))
    cluster_touched = sorted(path for path in changed if path in cluster)
    focus_touched_set = {path for path in changed if path in focus}
    focus_touched = [path for path in focus_ranked if path in focus_touched_set]
    outside_cluster = sorted(path for path in changed if cluster and path not in cluster)
    forbidden = sorted(path for path in changed if is_generated_or_vendor(path))
    expected_missing = _expected_missing(changed, expected)

    cluster_touch_rate = 0.0
    if changed:
        cluster_touch_rate = round(len(cluster_touched) / len(changed), 4)
    brief_edit_overlap = round(len(focus_touched_set) / len(focus), 4) if focus else 0.0
    focus_edit_precision = round(len(focus_touched_set) / len(changed), 4) if changed else 0.0
    focus_hit_at_1 = bool(focus_ranked[:1] and focus_ranked[0] in focus_touched_set)
    focus_hit_at_3 = bool(set(focus_ranked[:3]) & focus_touched_set)

    warnings: list[str] = []
    empty_patch = not changed
    tests_only = bool(test_files) and not source_files
    if empty_patch:
        warnings.append("empty_patch")
    if root_scaffolds:
        warnings.append("root_scaffold_files_added")
    if tests_only:
        warnings.append("tests_only_patch")
    if cluster and not cluster_touched and changed:
        warnings.append("no_cluster_file_touched")
    if focus and not focus_touched and changed:
        warnings.append("no_agent_focus_file_touched")
    if outside_cluster:
        warnings.append("edits_outside_candidate_cluster")
    if forbidden:
        warnings.append("forbidden_files_touched")
    if expected_missing:
        warnings.append("expected_side_files_missing")

    if test_result is not None and test_result.get("executed"):
        test_executed = True
        test_passed_value = bool(test_result.get("all_passed"))
        test_passed = test_passed_value
        test_passed_count = int(test_result.get("passed", 0) or 0)
        test_failed_count = int(test_result.get("failed", 0) or 0)
        test_errored_count = int(test_result.get("errored", 0) or 0)
    else:
        test_executed = False
        test_passed = False
        test_passed_count = 0
        test_failed_count = 0
        test_errored_count = 0
    if test_executed and not test_passed:
        warnings.append("failing_tests")

    if empty_patch or root_scaffolds or forbidden or tests_only:
        recommendation = "likely_invalid"
    elif warnings:
        recommendation = "needs_replan"
    else:
        recommendation = "on_plan"

    result = {
        "source_files_touched": source_files,
        "test_files_touched": test_files,
        "root_scaffold_files_added": root_scaffolds,
        "cluster_files_touched": cluster_touched,
        "cluster_touch_rate": cluster_touch_rate,
        "agent_focus_files_touched": focus_touched,
        "edited_ranked_focus_files": focus_touched,
        "brief_edit_overlap": brief_edit_overlap,
        "focus_hit_at_1": focus_hit_at_1,
        "focus_hit_at_3": focus_hit_at_3,
        "focus_edit_precision": focus_edit_precision,
        "outside_cluster_files": outside_cluster,
        "forbidden_files_touched": forbidden,
        "expected_side_files_missing": expected_missing,
        "tests_only_patch": tests_only,
        "empty_patch": empty_patch,
        "warnings": warnings,
        "recommendation": recommendation,
        "test_execution": {
            "executed": test_executed,
            "passed": test_passed_count,
            "failed": test_failed_count,
            "errored": test_errored_count,
            "all_passed": test_passed,
        },
    }
    if log_dir is not None:
        from groundtruth.runtime.telemetry import append_block

        append_block("gt_patch_shape", result, log_dir=log_dir, task_id=task_id)
    return result
