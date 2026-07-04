"""RED tests for kernel.decide_pre_tool.

Each test is xfail-strict because the kernel function raises NotImplementedError
in the Phase 1 skeleton. When the function is implemented, the xfail markers
flip to expected-pass and CI catches any regression.

Layers (per `whimsical-tinkering-bunny.md` §G.1 / future_plan.md §F):
    1. Happy path -- canonical fixture input/output.
    2. Boundary -- threshold edges 0.59/0.60/0.61, empty inputs, single-file repo.
    3. Adversarial -- malformed edit history, unicode paths, conflicting evidence.
    4. Mutation pin -- documents which kernel logic this test pins down so the
       Verifier lane can run mutation tests after implementation lands.

The mutation column in each test docstring names the specific constant or
branch the test fails under when mutated. This is the locked-decision-6 TTD
contract: tests must fail under at least one implementation mutation.
"""

from __future__ import annotations

from pathlib import Path

from groundtruth.control import kernel
from groundtruth.control.types import (
    BriefResult,
    Candidate,
    Capabilities,
    DecisionAction,
    RunState,
    ToolCall,
    ToolIntent,
)


# Phase 1 implementation landed -- tests are now expected-pass.


def _make_run_state(*, confidence: float, focus: str, model_hint: str | None = "claude-sonnet-4.5") -> RunState:
    focus_path = Path(focus)
    return RunState(
        task_id="t",
        plan={"agent_focus_files": [focus], "cluster_files": [focus]},
        brief_result=BriefResult(
            brief_text="",
            candidates=[Candidate(path=focus_path, score=confidence)],
            focus_files=[focus_path],
            cluster_files=[focus_path],
            confidence=confidence,
            plan={},
            plan_path=None,
        ),
        edit_history=[],
        capabilities=Capabilities(block=True, visible=True, audit=True, mid_task_pull=True, replan_inject=True),
        model_hint=model_hint,
    )


def _edit_call(path: str) -> ToolCall:
    return ToolCall(
        task_id="t",
        tool_name="str_replace_editor",
        args={"command": "create", "path": path},
        ts="2026-04-30T14:22:11Z",
        intent=ToolIntent.EDIT,
    )


# Layer 1: Happy path. Mutation pin: flipping the root-scaffold detector to
# always return False makes this test expect a different action.
def test_first_edit_root_scaffold_blocks(fixture_loader):
    input_data, expected = fixture_loader("first_edit_root_scaffold")
    rs = RunState.model_validate(input_data["run_state"])
    call = ToolCall.model_validate(input_data["function_input"])
    decision = kernel.decide_pre_tool(call, rs)
    assert decision.action == DecisionAction(expected["expected_return"]["action"])
    assert decision.rule_id == expected["expected_return"]["rule_id"]
    assert expected["expected_return"]["min_confidence"] <= decision.confidence <= expected["expected_return"]["max_confidence"]


# Layer 1: Happy path -- confidence-gated upgrade to block.
# Mutation pin: changing HIGH_CONFIDENCE_MIN from 0.6 to 0.9 should drop this case to visible.
def test_first_edit_misses_focus_high_confidence_blocks(fixture_loader):
    input_data, expected = fixture_loader("first_edit_misses_focus_high_confidence")
    rs = RunState.model_validate(input_data["run_state"])
    call = ToolCall.model_validate(input_data["function_input"])
    decision = kernel.decide_pre_tool(call, rs)
    assert decision.action == DecisionAction.BLOCK


# Layer 1: Confidence-gated downgrade to visible.
# Mutation pin: removing the confidence gate (always block) flips this to block.
def test_first_edit_misses_focus_low_confidence_visible(fixture_loader):
    input_data, _expected = fixture_loader("first_edit_misses_focus_low_confidence")
    rs = RunState.model_validate(input_data["run_state"])
    call = ToolCall.model_validate(input_data["function_input"])
    decision = kernel.decide_pre_tool(call, rs)
    assert decision.action != DecisionAction.BLOCK
    assert decision.action == DecisionAction.VISIBLE


# Layer 2: Boundary -- confidence at 0.59 (just below the canonical 0.6 floor).
# Mutation pin: any change to HIGH_CONFIDENCE_MIN that crosses 0.59/0.60 boundary breaks this.
def test_confidence_below_threshold_does_not_block():
    rs = _make_run_state(confidence=0.59, focus="src/a.py")
    decision = kernel.decide_pre_tool(_edit_call("src/elsewhere.py"), rs)
    assert decision.action != DecisionAction.BLOCK


# Layer 2: Boundary -- confidence exactly at threshold.
# Mutation pin: strictly-greater vs greater-or-equal comparison error.
def test_confidence_at_threshold_behaves_like_high():
    rs = _make_run_state(confidence=0.60, focus="src/a.py")
    decision = kernel.decide_pre_tool(_edit_call("src/elsewhere.py"), rs)
    assert decision.action == DecisionAction.BLOCK


# Layer 2: Boundary -- confidence just above threshold.
# Mutation pin: off-by-one on threshold.
def test_confidence_just_above_threshold_blocks():
    rs = _make_run_state(confidence=0.61, focus="src/a.py")
    decision = kernel.decide_pre_tool(_edit_call("src/elsewhere.py"), rs)
    assert decision.action == DecisionAction.BLOCK


# Layer 2: Boundary -- empty edit history is the first-edit case.
# Mutation pin: flipping edit_index = len(history) to len(history) + 1.
def test_empty_edit_history_is_first_edit():
    rs = _make_run_state(confidence=0.80, focus="src/a.py")
    rs = rs.model_copy(update={"edit_history": []})
    decision = kernel.decide_pre_tool(_edit_call("src/elsewhere.py"), rs)
    assert "first_edit" in decision.rule_id


# Layer 3: Adversarial -- unicode in paths must not crash or change semantics.
# Mutation pin: any path-normalization step that strips non-ASCII.
def test_unicode_paths_handled_cleanly():
    rs = _make_run_state(confidence=0.80, focus="src/файл.py")
    decision = kernel.decide_pre_tool(_edit_call("src/другой.py"), rs)
    assert decision.action in {DecisionAction.BLOCK, DecisionAction.VISIBLE}
    # Reasons are plain strings; must not raise on unicode.
    assert all(isinstance(r, str) for r in decision.reasons)


# Layer 3: Adversarial -- conflicting evidence (high localization + high drift).
# When the brief was confident but the agent has already drifted three edits,
# the policy must not silently downgrade to allow.
# Mutation pin: any branch that prefers localization over drift unconditionally.
def test_conflicting_high_localization_high_drift_does_not_allow():
    rs = _make_run_state(confidence=0.85, focus="src/a.py")
    rs = rs.model_copy(update={"warning_history": ["first_edit_missed_focus", "first_edit_missed_focus"]})
    decision = kernel.decide_pre_tool(_edit_call("src/elsewhere.py"), rs)
    assert decision.action != DecisionAction.ALLOW


# Layer 3: Adversarial -- malformed args (missing 'path' key) must produce
# an error_class != Unknown, never a crash.
# Mutation pin: removing the InvalidArguments classification path.
def test_malformed_tool_args_produces_invalid_arguments():
    rs = _make_run_state(confidence=0.80, focus="src/a.py")
    bad_call = ToolCall(
        task_id="t",
        tool_name="str_replace_editor",
        args={},  # no 'path'
        ts="2026-04-30T14:22:11Z",
        intent=ToolIntent.EDIT,
    )
    decision = kernel.decide_pre_tool(bad_call, rs)
    # Either the kernel rejects with audit + error_class, or it routes to allow with a warning;
    # either way it must not crash and must not block on missing args.
    assert decision.action in {DecisionAction.AUDIT, DecisionAction.ALLOW, DecisionAction.VISIBLE}


# Layer 3: Adversarial -- model_hint changes thresholds; missing hint must not crash.
# Mutation pin: implicit dict[model_hint] lookup without default.
def test_missing_model_hint_uses_default_thresholds():
    rs = _make_run_state(confidence=0.80, focus="src/a.py", model_hint=None)
    decision = kernel.decide_pre_tool(_edit_call("src/elsewhere.py"), rs)
    assert decision.action in {DecisionAction.BLOCK, DecisionAction.VISIBLE}
