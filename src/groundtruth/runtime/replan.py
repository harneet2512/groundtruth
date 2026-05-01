"""Evidence-triggered deterministic replan decisions."""

from __future__ import annotations

from collections import Counter
from typing import Any

from groundtruth.runtime.patch_auditor import ROOT_SCAFFOLD_PATTERNS, _matches_any, _norm


def evaluate_replan_triggers(
    *,
    edited_files: list[str],
    plan: dict[str, Any],
    warning_history: list[str] | None = None,
    viewed_files: list[str] | None = None,
    test_result: dict[str, Any] | None = None,
    patch_shape: dict[str, Any] | None = None,
    log_dir: str | None = None,
    task_id: str = "unknown",
) -> dict[str, Any]:
    """Return whether observed edit evidence crosses a replan threshold."""
    del viewed_files
    cluster = {_norm(p) for p in plan.get("cluster_files", []) if p}
    focus_ranked = _focus_files(plan)
    focus = set(focus_ranked)
    edits = [_norm(p) for p in edited_files if p]
    warnings = warning_history or list((patch_shape or {}).get("warnings", []) or [])
    reasons: list[str] = []
    patch_shape = patch_shape or {}

    if test_result and test_result.get("executed") and not test_result.get("all_passed"):
        reasons.append("failing_tests_after_edit")

    if not cluster:
        reasons.append("missing_or_empty_plan_cluster")

    if edits and _matches_any(edits[0], ROOT_SCAFFOLD_PATTERNS) and "/" not in edits[0]:
        reasons.append("first_edit_root_scaffold")

    focus_hits = [path for path in edits if path in focus]
    if focus and edits and not focus_hits:
        reasons.append("first_edit_missed_focus")
    if focus and len(edits) >= 3 and not focus_hits:
        reasons.append("no_focus_file_after_three_edits")

    outside = [path for path in edits if cluster and path not in cluster]
    if len(outside) >= 3:
        reasons.append("three_edits_outside_cluster")

    cluster_hits = [path for path in edits if path in cluster]
    if cluster and len(edits) >= 5 and not cluster_hits:
        reasons.append("no_cluster_file_after_five_edits")

    test_edits = [
        path
        for path in edits
        if path.startswith("tests/") or "/tests/" in path or path.startswith("test_")
    ]
    source_edits = [path for path in edits if path not in test_edits]
    if len(edits) >= 2 and test_edits and not source_edits:
        reasons.append("tests_only_after_two_edits")

    if patch_shape.get("expected_side_files_missing"):
        reasons.append("expected_side_files_missing")

    repeated = [warning for warning, count in Counter(warnings).items() if count >= 2]
    if repeated:
        reasons.append("repeated_warning:" + ",".join(sorted(repeated)))

    result = {
        "should_replan": bool(reasons),
        "reasons": reasons,
        "replan_stage": _stage(reasons),
        "corrective_instruction": _instruction(reasons),
        "next_actions": _next_actions(reasons, focus_ranked, test_result, patch_shape),
        "agent_focus_files": focus_ranked[:3],
        "validation_failures": _validation_failures(test_result),
    }
    if log_dir is not None:
        from groundtruth.runtime.telemetry import append_block

        append_block("gt_replan", result, log_dir=log_dir, task_id=task_id)
    return result


def _focus_files(plan: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in plan.get("agent_focus_files", []) or []:
        if isinstance(item, dict):
            path = item.get("file") or item.get("path")
        else:
            path = item
        norm = _norm(str(path or ""))
        if norm and norm not in out:
            out.append(norm)
    return out


def _stage(reasons: list[str]) -> str:
    if not reasons:
        return "stay_course"
    recompute_reasons = {
        "missing_or_empty_plan_cluster",
        "no_focus_file_after_three_edits",
        "no_cluster_file_after_five_edits",
    }
    if any(reason in recompute_reasons for reason in reasons):
        return "recompute"
    return "corrective"


def _instruction(reasons: list[str]) -> str:
    if not reasons:
        return "Continue with the current GT plan."
    if "missing_or_empty_plan_cluster" in reasons:
        return "Load or recompute the v7 plan before using runtime drift checks."
    if "first_edit_root_scaffold" in reasons:
        return "Stop creating root scaffolds; edit the localized source/test cluster instead."
    if "no_focus_file_after_three_edits" in reasons:
        return "Stop broad edits and recompute or reopen the compact focus plan before continuing."
    if "first_edit_missed_focus" in reasons:
        return "Redirect to the ranked agent_focus_files before making more broad edits."
    if "failing_tests_after_edit" in reasons:
        return "Selected contract tests are failing; revisit the source change before adding more files."
    if "tests_only_after_two_edits" in reasons:
        return "Touch the source implementation covered by the contract before adding more tests."
    if "expected_side_files_missing" in reasons:
        return "Check expected side files from the plan before submitting."
    if any(reason.endswith("outside_cluster") for reason in reasons):
        return "Re-check localization before continuing outside the candidate cluster."
    return "Recompute the GT plan from the original issue and observed edits."


def _next_actions(
    reasons: list[str],
    focus_ranked: list[str],
    test_result: dict[str, Any] | None,
    patch_shape: dict[str, Any],
) -> list[str]:
    if not reasons:
        return ["Continue with the current focused edit path."]
    actions: list[str] = []
    if "first_edit_root_scaffold" in reasons:
        actions.append("Remove root-level repro/scaffold files from the patch.")
    if "first_edit_missed_focus" in reasons or "no_focus_file_after_three_edits" in reasons:
        if focus_ranked:
            actions.append(f"Open and edit ranked focus file first: {focus_ranked[0]}.")
        else:
            actions.append("Recompute the v7 plan before continuing.")
    if "failing_tests_after_edit" in reasons:
        failures = _validation_failures(test_result)
        if failures:
            actions.append(f"Use visible failing test evidence first: {', '.join(failures[:3])}.")
        else:
            actions.append("Inspect the selected visible test failure before expanding the patch.")
    if "expected_side_files_missing" in reasons:
        missing = [str(item) for item in patch_shape.get("expected_side_files_missing", []) or []]
        if missing:
            actions.append(f"Review expected side file(s): {', '.join(missing[:3])}.")
    if "tests_only_after_two_edits" in reasons and focus_ranked:
        actions.append(f"Patch source behavior before adding more tests: {focus_ranked[0]}.")
    if not actions:
        actions.append(_instruction(reasons))
    return actions[:3]


def _validation_failures(test_result: dict[str, Any] | None) -> list[str]:
    if not test_result:
        return []
    names = test_result.get("failing_test_names", [])
    return [str(name) for name in names[:20]] if isinstance(names, list) else []
