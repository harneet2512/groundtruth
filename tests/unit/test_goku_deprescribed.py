"""C6A — de-prescribe the goku L5 hooks (strip imperative 'Next action:').

Research basis: SWE-PRM (NeurIPS 2025, arXiv 2509.02360) — action-prescriptive
mid-trajectory feedback LOWERED success and anchored the agent on the prescribed
move. GroundTruth's L5 goku hooks must therefore state the verifiable observed
fact only (diagnostic), never issue an imperative "Next action:" directive.

This test invokes each of the five goku hook MESSAGE BUILDERS directly (not the
governor, which adds gating/post-processing) and asserts none of the rendered
messages contains the imperative marker "Next action:".

The five goku hooks are exactly those dispatched by
``L5Governor.goku_check`` (governor.py): patch-collapsed, finish-without-witness,
structural-witness-ignored, weak-verification-after-edit, no-durable-progress.

Red-before-green: at HEAD three of the five (weak_verification_after_edit,
patch_collapsed_or_lost, no_durable_progress_goku) still emit a 'Next action:'
line, so this test MUST fail before the fix and pass after.
"""

from __future__ import annotations

from groundtruth.trajectory import hooks
from groundtruth.trajectory.state import (
    IterationBand,
    L5TrajectoryState,
)

_FORBIDDEN = "Next action:"


def _state_structural_witness_ignored() -> L5TrajectoryState:
    """Trigger hook_structural_witness_ignored: a GT next-action was emitted
    and has gone unexamined for >=3 real agent actions."""
    s = L5TrajectoryState(instance_id="t", current_iter=10, max_iter=100)
    s.latest_gt_next_action_type = "READ_CALLER_CONTRACT"
    s.latest_gt_next_action_file = "pkg/mod.py"
    s.structural_witness_followed = False
    s.actions_since_gt_next_action = 3
    return s


def _state_weak_verification_after_edit() -> L5TrajectoryState:
    """Trigger hook_weak_verification_after_edit: a source edit was followed
    only by a broad pass, never a targeted check."""
    s = L5TrajectoryState(instance_id="t", current_iter=10, max_iter=100)
    s.edited_source_files = ["pkg/mod.py"]
    s.last_edit_iter = 8
    s.last_passing_targeted_iter = 0  # < last_edit_iter
    s.broad_pass_after_edit_count = 1
    return s


def _state_finish_without_structural_witness() -> L5TrajectoryState:
    """Trigger hook_finish_without_structural_witness: agent finishes after a
    source edit with no caller/consumer examined and no targeted check."""
    s = L5TrajectoryState(instance_id="t", current_iter=10, max_iter=100)
    s.edited_source_files = ["pkg/mod.py"]
    s.last_edit_iter = 8
    s.structural_witness_followed = False
    s.last_passing_targeted_iter = 0  # < last_edit_iter
    return s


def _state_patch_collapsed_or_lost() -> L5TrajectoryState:
    """Trigger hook_patch_collapsed_or_lost: durable diff went nonzero->zero."""
    s = L5TrajectoryState(instance_id="t", current_iter=10, max_iter=100)
    s.patch_collapsed = True
    return s


def _state_no_durable_progress_goku() -> L5TrajectoryState:
    """Trigger hook_no_durable_progress_goku: no durable source edit by the
    late/final band."""
    s = L5TrajectoryState(instance_id="t", current_iter=90, max_iter=100)
    s.band = IterationBand.FINALIZATION
    s.edited_source_files = []
    return s


def _all_goku_messages() -> dict[str, str]:
    """Build a message from every goku hook; assert each actually fired."""
    msgs: dict[str, str] = {}

    m = hooks.hook_structural_witness_ignored(
        _state_structural_witness_ignored(), witness_file="pkg/mod.py"
    )
    msgs["structural_witness_ignored"] = m or ""

    m = hooks.hook_weak_verification_after_edit(_state_weak_verification_after_edit())
    msgs["weak_verification_after_edit"] = m or ""

    m = hooks.hook_finish_without_structural_witness(
        _state_finish_without_structural_witness()
    )
    msgs["finish_without_structural_witness"] = m or ""

    m = hooks.hook_patch_collapsed_or_lost(_state_patch_collapsed_or_lost())
    msgs["patch_collapsed_or_lost"] = m or ""

    m = hooks.hook_no_durable_progress_goku(_state_no_durable_progress_goku())
    msgs["no_durable_progress_goku"] = m or ""

    return msgs


def test_all_five_goku_hooks_fire_under_test_conditions() -> None:
    """Sanity / negative control: each builder must return a NON-empty message
    under its trigger condition, otherwise the de-prescription assertion below
    would pass vacuously."""
    msgs = _all_goku_messages()
    assert len(msgs) == 5
    for name, msg in msgs.items():
        assert msg, f"goku hook {name} returned no message under its trigger condition"


def test_no_goku_hook_emits_next_action_directive() -> None:
    """Every goku hook message must be diagnostic — no 'Next action:' imperative.

    Fails at HEAD on the three still-prescriptive hooks; passes after C6A."""
    msgs = _all_goku_messages()
    offenders = {name: msg for name, msg in msgs.items() if _FORBIDDEN in msg}
    assert not offenders, (
        "goku hooks must be diagnostic (SWE-PRM NeurIPS 2025); these still "
        f"emit an imperative '{_FORBIDDEN}' line: {sorted(offenders)}"
    )


def test_no_goku_hook_emits_imperative_verb_opener() -> None:
    """The de-prescribed hooks must not open a sentence with a bare imperative
    verb (run/re-apply/make/inspect ...) — the SWE-PRM failure mode. Diagnostic
    hooks describe the observed state; they do not command a move.

    Scoped to the three hooks converted by C6A; the two template hooks
    (structural_witness_ignored, finish_without_structural_witness) were already
    diagnostic and are covered by the directive test above."""
    converted = {
        "weak_verification_after_edit": _all_goku_messages()["weak_verification_after_edit"],
        "patch_collapsed_or_lost": _all_goku_messages()["patch_collapsed_or_lost"],
        "no_durable_progress_goku": _all_goku_messages()["no_durable_progress_goku"],
    }
    imperative_openers = ("run ", "re-apply", "reapply", "make one", "inspect ", "fix ")
    for name, msg in converted.items():
        for raw_line in msg.splitlines():
            line = raw_line.strip().lower()
            assert not line.startswith(imperative_openers), (
                f"goku hook {name} still issues an imperative directive: {raw_line!r}"
            )
