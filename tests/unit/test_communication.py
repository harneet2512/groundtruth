"""Tests for the deterministic communication state machine."""

from __future__ import annotations

from groundtruth.core.communication import (
    CommunicationPolicy,
    LoopState,
    SessionState,
    TaskPhase,
)


def make_policy(**kwargs: int) -> CommunicationPolicy:
    return CommunicationPolicy(**kwargs)


class TestSessionStateBasics:
    def test_initial_state_is_exploring(self) -> None:
        state = SessionState()
        assert state.phase == TaskPhase.EXPLORING
        assert state.edits_made == 0
        assert state.checks_run == 0
        assert state.last_tool is None
        assert state.tools_called == {}

    def test_record_tool_call_increments_counters(self) -> None:
        policy = make_policy()
        s0 = SessionState()
        s1 = policy.record_tool_call(s0, "search")
        assert s1.tools_called["search"] == 1
        assert s1.last_tool == "search"
        s2 = policy.record_tool_call(s1, "search")
        assert s2.tools_called["search"] == 2

    def test_state_immutability(self) -> None:
        """record_* must return NEW state, not mutate the original."""
        policy = make_policy()
        s0 = SessionState()
        s1 = policy.record_tool_call(s0, "search")
        assert s0.tools_called == {}
        assert s0.last_tool is None

        s2 = policy.record_edit(s0)
        assert s0.edits_made == 0
        assert s0.phase == TaskPhase.EXPLORING
        assert s2.edits_made == 1


class TestPhaseTransitions:
    def test_edit_transitions_exploring_to_editing(self) -> None:
        policy = make_policy()
        s0 = SessionState()
        s1 = policy.record_edit(s0)
        assert s1.phase == TaskPhase.EDITING

    def test_edit_does_not_regress_patch_exists(self) -> None:
        policy = make_policy()
        s0 = SessionState(phase=TaskPhase.PATCH_EXISTS)
        s1 = policy.record_edit(s0)
        assert s1.phase == TaskPhase.PATCH_EXISTS
        assert s1.edits_made == 1

    def test_check_tool_transitions_to_patch_exists(self) -> None:
        policy = make_policy()
        s0 = SessionState(phase=TaskPhase.EDITING)
        s1 = policy.record_tool_call(s0, "check-diff")
        assert s1.phase == TaskPhase.PATCH_EXISTS
        assert s1.checks_run == 1

    def test_test_tool_transitions_to_tested(self) -> None:
        policy = make_policy()
        s0 = SessionState(phase=TaskPhase.PATCH_EXISTS)
        s1 = policy.record_tool_call(s0, "test")
        assert s1.phase == TaskPhase.TESTED


class TestLoopDetection:
    def test_search_spinning_after_threshold(self) -> None:
        policy = make_policy(search_spin_threshold=5)
        state = SessionState()
        for _ in range(5):
            state = policy.record_tool_call(state, "search")
        assert policy.detect_loop(state) == LoopState.SEARCH_SPINNING

    def test_no_search_spinning_if_edits_made(self) -> None:
        policy = make_policy(search_spin_threshold=5)
        state = SessionState()
        for _ in range(5):
            state = policy.record_tool_call(state, "search")
        state = policy.record_edit(state)
        assert policy.detect_loop(state) == LoopState.NORMAL

    def test_check_looping_after_threshold(self) -> None:
        policy = make_policy(check_loop_threshold=2)
        state = SessionState(phase=TaskPhase.EDITING)
        # First check transitions to PATCH_EXISTS.
        state = policy.record_tool_call(state, "check-diff")
        # Second check triggers loop.
        state = policy.record_tool_call(state, "check-diff")
        assert state.checks_run == 2
        assert policy.detect_loop(state) == LoopState.CHECK_LOOPING

    def test_normal_when_below_thresholds(self) -> None:
        policy = make_policy()
        state = SessionState()
        state = policy.record_tool_call(state, "search")
        assert policy.detect_loop(state) == LoopState.NORMAL


class TestFraming:
    def test_impact_exploring_framing(self) -> None:
        policy = make_policy()
        state = SessionState(phase=TaskPhase.EXPLORING)
        framing = policy.get_framing(state, "impact")
        assert framing is not None
        assert "Edit" in framing and "check" in framing

    def test_impact_patch_exists_framing(self) -> None:
        policy = make_policy()
        state = SessionState(phase=TaskPhase.PATCH_EXISTS)
        framing = policy.get_framing(state, "impact")
        assert framing is not None
        assert "Patch exists" in framing

    def test_references_patch_exists_framing(self) -> None:
        policy = make_policy()
        state = SessionState(phase=TaskPhase.PATCH_EXISTS)
        framing = policy.get_framing(state, "references")
        assert framing is not None
        assert "check-diff" in framing

    def test_search_spinning_framing_overrides(self) -> None:
        policy = make_policy(search_spin_threshold=3)
        state = SessionState()
        for _ in range(3):
            state = policy.record_tool_call(state, "search")
        framing = policy.get_framing(state, "search")
        assert framing is not None
        assert "without editing" in framing

    def test_check_looping_framing(self) -> None:
        policy = make_policy(check_loop_threshold=2)
        state = SessionState(phase=TaskPhase.EDITING)
        state = policy.record_tool_call(state, "check-diff")
        state = policy.record_tool_call(state, "check-diff")
        framing = policy.get_framing(state, "check-diff")
        assert framing is not None
        assert "Submit" in framing

    def test_no_framing_for_normal_check(self) -> None:
        policy = make_policy()
        state = SessionState(phase=TaskPhase.EDITING)
        framing = policy.get_framing(state, "check-diff")
        assert framing is None

    def test_no_framing_for_unmatched_tool(self) -> None:
        policy = make_policy()
        state = SessionState(phase=TaskPhase.EXPLORING)
        framing = policy.get_framing(state, "some_other_tool")
        assert framing is None
