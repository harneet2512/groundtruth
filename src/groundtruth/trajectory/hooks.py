"""L5 hook implementations — 7 trajectory-aware intervention hooks."""

from __future__ import annotations

from .state import L5TrajectoryState, IterationBand
from .parsers import FailureRecord


_MAX_L5_TOKENS = 180


def _iteration_prefix(state: L5TrajectoryState) -> str:
    ratio = state.current_iter / max(state.max_iter, 1)
    if ratio >= 0.60:
        return f"Iteration: {state.current_iter}/{state.max_iter}\n"
    return ""


def _late_repair_suffix(state: L5TrajectoryState) -> str:
    if state.band in (IterationBand.LATE_REPAIR, IterationBand.FINALIZATION):
        return "\nDo not restart exploration. Repair the current hypothesis."
    return ""


def hook_no_durable_source_progress(
    state: L5TrajectoryState,
    edited_path: str,
) -> str | None:
    if state.edited_source_files:
        return None
    if state.band == IterationBand.FINALIZATION:
        return (
            f'[GT L5: No Durable Source Progress]\n'
            f'{_iteration_prefix(state)}'
            f'Evidence: edits so far are scaffold/test/non-source.\n'
            f'Mismatch: task requires changing project behavior.\n'
            f'Next action: stop scaffolding. Make one durable source edit.'
        )
    return (
        f'[GT L5: No Durable Source Progress]\n'
        f'{_iteration_prefix(state)}'
        f'Evidence: {edited_path} is not a durable source edit.\n'
        f'Mismatch: task requires changing project behavior.\n'
        f'Next action: make one source/config edit connected to the issue.'
    )


def hook_premature_commitment(
    state: L5TrajectoryState,
    edited_file: str,
    confirming_edges_opened: int,
    l3_contract_line: str = "",
) -> str | None:
    if state.band in (IterationBand.LATE_REPAIR, IterationBand.FINALIZATION):
        return None
    if confirming_edges_opened > 0:
        return None
    if state.verification_commands_run > 0:
        return None
    ctx = f"Context: {l3_contract_line}\n" if l3_contract_line else ""
    return (
        f'[GT L5: Premature Commitment]\n'
        f'{_iteration_prefix(state)}'
        f'Evidence: source edit to {edited_file} before inspecting a confirming test/caller.\n'
        f'Mismatch: patch hypothesis is unconfirmed.\n'
        f'{ctx}'
        f'Next action: run tests or inspect one confirming caller/test before expanding the patch.'
    )


def hook_patch_hypothesis(
    state: L5TrajectoryState,
    edited_file: str,
    l3_contract_line: str = "",
) -> str | None:
    """Deprecated: not called from governor. Retained for potential future wiring."""
    if not l3_contract_line:
        return None
    if state.band == IterationBand.FINALIZATION:
        return None
    return (
        f'[GT L5: Patch Hypothesis]\n'
        f'{_iteration_prefix(state)}'
        f'Evidence: edited {edited_file}.\n'
        f'Context: {l3_contract_line}\n'
        f'Next action: run targeted verification to confirm this fix.'
    )


def hook_hypothesis_falsified(
    state: L5TrajectoryState,
    failure: FailureRecord | None = None,
    l3_contract_line: str = "",
) -> str | None:
    """THE KEY HOOK — fires after test failure following a source edit."""
    if not state.has_source_edit_before_last_failure:
        return None
    if failure is None:
        return None

    edited = state.edited_source_files[-1] if state.edited_source_files else "unknown"
    fail_desc = failure.render_compact(max_chars=120)
    ctx = f"Context: {l3_contract_line}\n" if l3_contract_line else ""

    return (
        f'[GT L5: Hypothesis Falsified]\n'
        f'{_iteration_prefix(state)}'
        f'Evidence: verification failed after editing {edited}.\n'
        f'{fail_desc}\n'
        f'{ctx}'
        f'Next action: revise the edit that produces the wrong result.{_late_repair_suffix(state)}'
    )


def hook_same_failure_persisted(
    state: L5TrajectoryState,
    failure: FailureRecord | None = None,
    l3_repair_line: str = "",
) -> str | None:
    if state.repeated_failure_count < 1:
        return None
    if failure is None:
        return None

    edited = state.edited_source_files[-1] if state.edited_source_files else "unknown"
    ctx = f"Context: {l3_repair_line}\n" if l3_repair_line else ""

    return (
        f'[GT L5: Same Failure Persisted]\n'
        f'{_iteration_prefix(state)}'
        f'Evidence: same failure repeated after your last edit to {edited}.\n'
        f'Mismatch: last patch did not change the behavior producing the error.\n'
        f'{ctx}'
        f'Next action: change the code path, not the surface.{_late_repair_suffix(state)}'
    )


def hook_symptom_convergence(
    state: L5TrajectoryState,
    concentrated_module: str,
    bridge_file: str,
) -> str | None:
    """Deprecated: not called from governor. Retained for potential future wiring."""
    if state.band == IterationBand.FINALIZATION:
        return None
    return (
        f'[GT L5: Symptom Convergence]\n'
        f'{_iteration_prefix(state)}'
        f'Evidence: recent work is concentrated in {concentrated_module}.\n'
        f'Mismatch: bridge evidence points outside this module.\n'
        f'Next action: inspect {bridge_file} before another same-module edit.'
    )


def hook_unverified_patch(
    state: L5TrajectoryState,
    _command: str = "",
    _edited_files: list[str] | None = None,
    test_file_suggestions: list[str] | None = None,
) -> str | None:
    """Fires when agent runs broad tests after a source edit without targeted verification.

    NOT suppressed in FINALIZATION — unverified patch risk is highest at finish.
    Debounced: fires once per edit cycle (reset when new source edit recorded).
    """
    if not state.has_unverified_patch():
        return None
    if state.last_l5_hook == "unverified_patch" and state.last_l5_iter >= state.last_edit_iter:
        return None

    edited = state.edited_source_files[-1] if state.edited_source_files else "unknown"
    suggestions = ""
    if test_file_suggestions:
        suggestions = f"Test files connected to edited code: {', '.join(test_file_suggestions[:3])}\n"

    return (
        f'[GT L5: Unverified Patch]\n'
        f'{_iteration_prefix(state)}'
        f'Evidence: broad test suite passed after editing {edited}, '
        f'but no targeted test was run for the changed code.\n'
        f'Mismatch: a broad passing suite does not confirm the fix is correct.\n'
        f'{suggestions}'
        f'Next action: run a test that specifically exercises the changed function.'
        f'{_late_repair_suffix(state)}'
    )


def hook_unsafe_finish(
    state: L5TrajectoryState,
    l3_repair_line: str = "",
) -> str | None:
    # Branch A: unresolved verification failure
    if state.has_unresolved_failure():
        last_fail = state.last_failure()
        fail_info = ""
        if last_fail:
            fail_info = f"Last failure: {last_fail.get('failing_unit', 'unknown')}\n"
        ctx = f"Context: {l3_repair_line}\n" if l3_repair_line else ""
        return (
            f'[GT L5: Unsafe Finish]\n'
            f'{_iteration_prefix(state)}'
            f'Evidence: unresolved verification failure remains.\n'
            f'{fail_info}'
            f'{ctx}'
            f'Next action: fix or verify before finishing.'
        )

    if not state.edited_source_files:
        return None

    # Branch B: unverified patch (broad tests passed but no targeted verification)
    if state.has_unverified_patch():
        edited = state.edited_source_files[-1]
        return (
            f'[GT L5: Unsafe Finish]\n'
            f'{_iteration_prefix(state)}'
            f'Evidence: broad tests passed after editing {edited}, but no targeted '
            f'verification was run for the changed code.\n'
            f'Mismatch: finishing with an unverified patch.\n'
            f'Next action: run one targeted test for the changed function before finishing.'
        )

    # Branch C: no verification at all
    if state.verification_commands_run == 0:
        return (
            f'[GT L5: Unsafe Finish]\n'
            f'{_iteration_prefix(state)}'
            f'Evidence: no verification command was run after your edit.\n'
            f'Mismatch: finishing now may submit an unverified patch.\n'
            f'Next action: run one targeted test before finishing.'
        )


# --- Decision 34: P0 Generalized Event-Driven Hooks ---


def hook_structural_witness_ignored(
    state: L5TrajectoryState,
    witness_file: str | None = None,
) -> str | None:
    """P0: GT emitted a structural next_action, agent did not follow within 3 real actions."""
    if not state.latest_gt_next_action_type:
        return None
    if state.structural_witness_followed:
        return None
    if state.actions_since_gt_next_action < 3:
        return None

    # DIAGNOSTIC, not prescriptive (SWE-PRM NeurIPS 2025, arXiv 2509.02360):
    # content/location prescription ("inspect X") anchors and lowered
    # resolution. State the verifiable observation and let the agent decide.
    file_hint = f" involving {witness_file}" if witness_file else ""
    return (
        f'[GT L5: Unexamined structural signal]\n'
        f'{_iteration_prefix(state)}'
        f'A high-confidence structural relation{file_hint} has not been '
        f'examined in {state.actions_since_gt_next_action} actions. '
        f'It may be relevant to the edit.'
        f'{_late_repair_suffix(state)}'
    )


def hook_weak_verification_after_edit(
    state: L5TrajectoryState,
) -> str | None:
    """P0: Source edit followed only by broad verification, no targeted."""
    if not state.edited_source_files:
        return None
    if state.last_passing_targeted_iter >= state.last_edit_iter:
        return None
    if state.broad_pass_after_edit_count < 1:
        return None

    edited = state.edited_source_files[-1]
    # DIAGNOSTIC, not prescriptive (SWE-PRM NeurIPS 2025, arXiv 2509.02360):
    # an imperative "Next action: run ..." anchors the agent and lowered
    # resolution. State the verifiable observation; let the agent decide.
    return (
        f'[GT L5: Weak Verification After Edit]\n'
        f'{_iteration_prefix(state)}'
        f'A broad check passed after editing {edited}, but no targeted check '
        f'has exercised the changed code. The edit is unconfirmed at the '
        f'function level.'
        f'{_late_repair_suffix(state)}'
    )


def hook_finish_without_structural_witness(
    state: L5TrajectoryState,
) -> str | None:
    """P0: Agent finishes with 0 callers/consumers read after source edit."""
    if not state.edited_source_files:
        return None
    if state.structural_witness_followed:
        return None
    if state.last_passing_targeted_iter >= state.last_edit_iter:
        return None

    edited = state.edited_source_files[-1]
    # Diagnostic verify-before-finish (SWE-agent guardrail class, +10.7pp).
    # States the unverified-finish fact; no specific-caller location prescription.
    return (
        f'[GT L5: Finish without verification]\n'
        f'{_iteration_prefix(state)}'
        f'Finishing after editing {edited} without any caller/consumer of '
        f'the changed code having been examined. The change is unverified '
        f'against its dependents.'
    )


def hook_patch_collapsed_or_lost(
    state: L5TrajectoryState,
) -> str | None:
    """P0: Durable diff went from nonzero to zero."""
    if not state.patch_collapsed:
        return None

    # DIAGNOSTIC, not prescriptive (SWE-PRM NeurIPS 2025, arXiv 2509.02360):
    # report the observed loss of the durable diff; do not prescribe the move.
    return (
        f'[GT L5: Patch Collapsed]\n'
        f'{_iteration_prefix(state)}'
        f'The durable diff went from nonzero to zero — the prior source '
        f'changes are no longer present.'
        f'{_late_repair_suffix(state)}'
    )


def hook_no_durable_progress_goku(
    state: L5TrajectoryState,
) -> str | None:
    """P0: No durable product file edit by late band."""
    if state.edited_source_files:
        return None
    if state.band not in (IterationBand.LATE_REPAIR, IterationBand.FINALIZATION):
        return None

    # DIAGNOSTIC, not prescriptive (SWE-PRM NeurIPS 2025, arXiv 2509.02360):
    # state the absence of a durable source edit; do not prescribe the move.
    return (
        f'[GT L5: No Durable Progress]\n'
        f'{_iteration_prefix(state)}'
        f'No durable source edit exists at this late stage — all edits so far '
        f'are scaffold, test, or non-source. The task requires changing '
        f'project behavior.'
    )


class L5bSafetyChecker:
    """Validates L5b interventions before emission."""

    RESTART_PHRASES = [
        "start over", "restart", "begin again", "from scratch",
        "reset", "redo", "start fresh", "go back to the beginning",
        "abandon this approach entirely",
    ]
    BROAD_EXPLORATION_PHRASES = [
        "explore the codebase", "look around", "browse the project",
        "search for all", "find all", "grep the entire",
    ]

    @staticmethod
    def validate(text: str, iteration_ratio: float = 0.0) -> tuple[bool, str | None]:
        text_lower = text.lower()
        for phrase in L5bSafetyChecker.RESTART_PHRASES:
            if phrase in text_lower:
                idx = text_lower.index(phrase)
                before = text_lower[max(0, idx - 10):idx].strip()
                if before.endswith("do not") or before.endswith("don't") or before.endswith("never"):
                    continue
                return False, f"restart_language: '{phrase}'"
        if iteration_ratio >= 0.60:
            for phrase in L5bSafetyChecker.BROAD_EXPLORATION_PHRASES:
                if phrase in text_lower:
                    return False, f"late_broad_exploration: '{phrase}'"
        token_estimate = len(text) // 4
        if token_estimate > 180:
            return False, f"exceeds_token_cap: {token_estimate} > 180"
        return True, None
