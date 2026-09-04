"""Product-owned runtime controller receipts for mini-swe integration."""

from __future__ import annotations

from groundtruth.runtime.action_translation import translate_to_action
from groundtruth.runtime.context_budget import ContextBudgeter
from groundtruth.runtime.context_policy import Event, Phase, should_emit
from groundtruth.runtime.obligations import (
    OBL_EDITED_UNTESTED,
    OBL_TESTED,
    ObligationTracker,
    obligation_statuses,
    render_obligation_status_block,
)
from groundtruth.runtime.trajectory_state import (
    TrajectoryState,
    Turn,
    derive_phase,
    derive_state,
)
from groundtruth.runtime.verification_horizon import (
    render_verify_emission,
    verify_horizon_band,
)


class _View:
    idx = 1
    verbatim = "capture_snapshot must include task diff"
    sym_parts = frozenset({"capture_snapshot"})


def test_product_controller_phase_progression():
    assert derive_phase(TrajectoryState(action_count=2)) == Phase.ORIENT
    assert derive_phase(TrajectoryState(action_count=8)) == Phase.VIEW
    assert (
        derive_phase(
            TrajectoryState(
                action_count=10,
                edited_files={"src/app.py"},
                source_edit_count=1,
            )
        )
        == Phase.EDIT
    )
    assert (
        derive_phase(
            TrajectoryState(
                action_count=20,
                edited_files={"src/app.py"},
                source_edit_count=1,
                nonedit_streak=3,
            )
        )
        == Phase.VERIFY
    )
    assert (
        derive_phase(
            TrajectoryState(
                action_count=95,
                step_limit=100,
                edited_files={"src/app.py"},
                source_edit_count=1,
            )
        )
        == Phase.SUBMIT
    )


def test_derive_state_from_turns_is_language_agnostic():
    turns = [
        Turn(command="sed -n '1,80p' src/lib.rs", observation=""),
        Turn(command="python -c \"open('src/lib.rs','w').write('fn capture_snapshot() {}')\""),
        Turn(command="cargo test", observation="test result: ok. 1 passed; 0 failed"),
    ]
    state = derive_state(turns, step_limit=100)
    assert "src/lib.rs" in state.viewed_files
    assert "src/lib.rs" in state.edited_files
    assert state.test_evidence_seen is True
    assert "capture_snapshot" in state.edited_tokens
    assert state.test_count == 1


def test_context_policy_event_bound_and_wrong_phase():
    denied = should_emit("spec.obligation", Phase.VIEW)
    assert denied.allowed is False and denied.reason == "wrong_phase"
    allowed = should_emit("l3b.evidence", Phase.ORIENT, event=Event.POST_VIEW, event_bound=True)
    assert allowed.allowed is True and allowed.reason == "event_bound"


def test_obligation_lifecycle_and_render_no_exact_test_name():
    statuses = obligation_statuses([_View()], {"capture_snapshot"}, set())
    assert statuses[0][1] == OBL_EDITED_UNTESTED
    block = render_obligation_status_block(
        statuses, covering={1: ["tests/test_exact.py::test_hidden"]}
    )
    assert "graph-linked covering test available" in block
    assert "tests/test_exact.py" not in block
    tracker = ObligationTracker([_View()])
    tracker.update({"capture_snapshot"}, set(), 3)
    assert tracker.snapshot()[0]["status"] == "edited"
    tracker.update({"capture_snapshot"}, {"test_capture_snapshot"}, 4)
    assert tracker.snapshot()[0]["status"] == "tested"
    assert (
        obligation_statuses([_View()], {"capture_snapshot"}, {"test_capture_snapshot"})[0][1]
        == OBL_TESTED
    )


def test_verification_horizon_and_budget_and_action_translation():
    assert (
        verify_horizon_band(
            95,
            100,
            10,
            edit_coverage=1.0,
            test_coverage=0.0,
            has_edits=True,
        )
        == "gate"
    )
    rendered = render_verify_emission(
        "gate",
        95,
        100,
        {"src/app.ts"},
        ["tests/app.test.ts::hidden_name"],
    )
    assert "Run the narrowest relevant repo test target NOW" in rendered
    assert "hidden_name" not in rendered

    budgeter = ContextBudgeter()
    payload = "[WITNESS] foo by -> src/app.ts:10\nInspect src/app.ts"
    out1 = budgeter.trim(payload, 20)
    assert out1.text
    # D1: trim no longer auto-commits — dedup takes effect only after the gate
    # confirms delivery via commit_delivered (gate losses must not destroy facts).
    out_uncommitted = budgeter.trim(payload, 20)
    assert out_uncommitted.text, "uncommitted facts must NOT be suppressed"
    budgeter.commit_delivered(out1.pending_lines)
    out2 = budgeter.trim(payload, 20)
    assert out2.text == "", "committed facts must be suppressed"

    # D5: caller direction ("X called by -> loc") names the CALLEE at risk and
    # keeps the original fact, appending the imperative (never replacing it).
    action = translate_to_action("[WITNESS] foo called by -> src/app.ts:10", Phase.EDIT)
    assert "[WITNESS] foo called by" in action
    assert "Changing foo risks breaking the caller at src/app.ts:10" in action
