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
    if state.band == IterationBand.FINALIZATION:
        return None
    return (
        f'[GT L5: Symptom Convergence]\n'
        f'{_iteration_prefix(state)}'
        f'Evidence: recent work is concentrated in {concentrated_module}.\n'
        f'Mismatch: bridge evidence points outside this module.\n'
        f'Next action: inspect {bridge_file} before another same-module edit.'
    )


def hook_unsafe_finish(
    state: L5TrajectoryState,
    l3_repair_line: str = "",
) -> str | None:
    if not state.has_unresolved_failure():
        if not state.edited_source_files:
            return None
        if state.verification_commands_run > 0:
            return None
        return (
            f'[GT L5: Unsafe Finish]\n'
            f'{_iteration_prefix(state)}'
            f'Evidence: no verification command was run after your edit.\n'
            f'Mismatch: finishing now may submit an unverified patch.\n'
            f'Next action: run one targeted test before finishing.'
        )

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
