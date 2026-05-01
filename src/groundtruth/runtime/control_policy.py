"""Deterministic GT runtime control policy.

This layer converts observed patch/test/replan evidence into a compact
intervention decision. It is deliberately model-free and language-neutral:
hooks or harness wrappers can decide how to surface the decision, but the
policy records whether GT is only auditing, visible to the agent, or blocking.
"""

from __future__ import annotations

from typing import Any

MAX_INTERVENTION_CHARS = 1200


def decide_control_action(
    *,
    patch_shape: dict[str, Any] | None = None,
    replan_decision: dict[str, Any] | None = None,
    test_result: dict[str, Any] | None = None,
    hook_can_block: bool = False,
    final_audit_only: bool = False,
) -> dict[str, Any]:
    """Return a compact control decision for hook/harness surfaces."""
    patch_shape = patch_shape or {}
    replan_decision = replan_decision or {}
    test_result = test_result or {}

    reasons = _reasons(patch_shape, replan_decision, test_result)
    severity = _severity(reasons, final_audit_only=final_audit_only)
    should_surface = bool(reasons) and not final_audit_only
    should_block = bool(hook_can_block and severity == "block" and should_surface)
    next_actions = _next_actions(replan_decision, patch_shape, test_result)
    message = _message(severity, reasons, next_actions)

    return {
        "severity": severity,
        "reasons": reasons,
        "message": message,
        "next_actions": next_actions,
        "hook_logged": True,
        "hook_visible_to_agent": should_surface,
        "hook_blocked": should_block,
        "final_audit_only": bool(final_audit_only),
        "should_replan": bool(replan_decision.get("should_replan", False)),
        "validation_failed": bool(test_result.get("executed") and not test_result.get("all_passed")),
    }


def format_intervention(decision: dict[str, Any]) -> str:
    """Render a concise intervention suitable for hook stderr/stdout."""
    if not decision.get("hook_visible_to_agent"):
        return ""
    message = str(decision.get("message") or "")
    if len(message) <= MAX_INTERVENTION_CHARS:
        return message
    return message[: MAX_INTERVENTION_CHARS - 3].rstrip() + "..."


def _reasons(
    patch_shape: dict[str, Any],
    replan_decision: dict[str, Any],
    test_result: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    for warning in patch_shape.get("warnings", []) or []:
        out.append(str(warning))
    for reason in replan_decision.get("reasons", []) or []:
        out.append(str(reason))
    if test_result.get("executed") and not test_result.get("all_passed"):
        out.append("visible_validation_failed")
    return list(dict.fromkeys(out))


def _severity(reasons: list[str], *, final_audit_only: bool) -> str:
    if final_audit_only:
        return "audit"
    block_reasons = {
        "first_edit_root_scaffold",
        "root_scaffold_files_added",
        "tests_only_patch",
        "tests_only_after_two_edits",
        "no_focus_file_after_three_edits",
        "failing_tests_after_edit",
        "visible_validation_failed",
    }
    if any(reason in block_reasons for reason in reasons):
        return "block"
    if reasons:
        return "warn"
    return "pass"


def _next_actions(
    replan_decision: dict[str, Any],
    patch_shape: dict[str, Any],
    test_result: dict[str, Any],
) -> list[str]:
    actions = [str(action) for action in replan_decision.get("next_actions", []) or [] if action]
    if actions:
        return actions[:3]
    if test_result.get("executed") and not test_result.get("all_passed"):
        failures = test_result.get("failing_test_names", []) or []
        if isinstance(failures, list) and failures:
            return [f"Repair visible failing test first: {', '.join(map(str, failures[:3]))}."]
        return ["Inspect visible test failure before expanding the patch."]
    if patch_shape.get("root_scaffold_files_added"):
        return ["Remove root-level scaffold/repro files and patch the localized source."]
    if "tests_only_patch" in (patch_shape.get("warnings", []) or []):
        return ["Patch source behavior before adding more tests."]
    return []


def _message(severity: str, reasons: list[str], next_actions: list[str]) -> str:
    if severity == "pass":
        return "GT runtime: patch shape is currently on plan."
    if severity == "audit":
        return "GT runtime audit recorded; no agent-visible intervention was issued."
    lines = [
        f"GT runtime intervention [{severity}]",
        "Reasons: " + ", ".join(reasons[:6]),
    ]
    if next_actions:
        lines.append("Next actions:")
        for idx, action in enumerate(next_actions[:3], start=1):
            lines.append(f"{idx}. {action}")
    return "\n".join(lines)
