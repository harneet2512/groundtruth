"""Tests for Phase 5 communication state machine fixes.

Real scenarios: agent search-spinning on SWE-bench tasks, empty diffs from
reverted changes, tool name normalization across all 20+ MCP tools,
and evidence-based PATCH_EXISTS transitions.
"""

from __future__ import annotations

import pytest

from groundtruth.core.communication import (
    CommunicationPolicy,
    LoopState,
    SessionState,
    TaskPhase,
    normalize_tool_name,
)


class TestNormalizeToolName:
    """Tool name normalization must work for every MCP tool in the codebase."""

    @pytest.mark.parametrize("raw,expected", [
        # Standard tools — strip "groundtruth_" prefix
        ("groundtruth_find_relevant", "find_relevant"),
        ("groundtruth_impact", "impact"),
        ("groundtruth_trace", "trace"),
        ("groundtruth_validate", "validate"),
        ("groundtruth_status", "status"),
        ("groundtruth_dead_code", "dead_code"),
        ("groundtruth_hotspots", "hotspots"),
        ("groundtruth_orient", "orient"),
        ("groundtruth_checkpoint", "checkpoint"),
        ("groundtruth_symbols", "symbols"),
        ("groundtruth_context", "context"),
        ("groundtruth_explain", "explain"),
        ("groundtruth_patterns", "patterns"),
        ("groundtruth_check_patch", "check_patch"),
        ("groundtruth_brief", "brief"),
        ("groundtruth_scope", "scope"),
        # Consolidated tools — strip both prefixes
        ("groundtruth_consolidated_check", "check"),
        ("groundtruth_consolidated_impact", "impact"),
        ("groundtruth_consolidated_orient", "orient"),
        ("groundtruth_consolidated_references", "references"),
        ("groundtruth_consolidated_search", "search"),
        # Already canonical — no-op
        ("impact", "impact"),
        ("check", "check"),
        ("search", "search"),
        ("find_relevant", "find_relevant"),
        # Edge: unknown tool — still strips prefix
        ("groundtruth_unknown_tool", "unknown_tool"),
    ])
    def test_normalization(self, raw: str, expected: str) -> None:
        assert normalize_tool_name(raw) == expected


class TestSearchSpinThreshold:
    """Search spin threshold changed from 5 to 3.
    Real scenario: SWE-bench agent calls impact, references, search
    without making any edits — should get redirect after 3 total searches."""

    def test_default_threshold_is_3(self) -> None:
        policy = CommunicationPolicy()
        assert policy.search_spin_threshold == 3

    def test_spin_detected_at_3_searches(self) -> None:
        """Agent calls 3 search tools without editing → spinning."""
        policy = CommunicationPolicy()
        state = SessionState()

        state = policy.record_tool_call(state, "impact")
        assert policy.detect_loop(state) == LoopState.NORMAL

        state = policy.record_tool_call(state, "references")
        assert policy.detect_loop(state) == LoopState.NORMAL

        state = policy.record_tool_call(state, "search")
        assert policy.detect_loop(state) == LoopState.SEARCH_SPINNING

    def test_spin_not_detected_if_edit_happened(self) -> None:
        """Agent searched 3 times but also edited — NOT spinning."""
        policy = CommunicationPolicy()
        state = SessionState()

        state = policy.record_tool_call(state, "impact")
        state = policy.record_tool_call(state, "references")
        state = policy.record_edit(state)
        state = policy.record_tool_call(state, "search")
        assert policy.detect_loop(state) == LoopState.NORMAL

    def test_spin_framing_message(self) -> None:
        """When spinning, get_framing returns a redirect."""
        policy = CommunicationPolicy()
        state = SessionState()
        for tool in ["impact", "references", "search"]:
            state = policy.record_tool_call(state, tool)

        framing = policy.get_framing(state, "trace")
        assert framing is not None
        assert "searches without editing" in framing.lower() or "multiple searches" in framing.lower()

    def test_old_threshold_5_would_not_trigger(self) -> None:
        """Verify old threshold=5 would NOT have triggered at 3 searches.
        This proves the threshold change matters."""
        policy = CommunicationPolicy(search_spin_threshold=5)
        state = SessionState()
        for tool in ["impact", "references", "search"]:
            state = policy.record_tool_call(state, tool)
        assert policy.detect_loop(state) == LoopState.NORMAL

    def test_same_search_tool_repeated(self) -> None:
        """Agent calls impact 3 times in a row — still spinning."""
        policy = CommunicationPolicy()
        state = SessionState()
        for _ in range(3):
            state = policy.record_tool_call(state, "impact")
        assert policy.detect_loop(state) == LoopState.SEARCH_SPINNING


class TestEvidenceBasedPatchExists:
    """PATCH_EXISTS transition requires evidence, not just tool name.
    Real scenarios: empty diffs, reverted diffs, no-op check calls."""

    def test_check_with_changes_transitions(self) -> None:
        """Agent calls check-diff with actual changes → PATCH_EXISTS."""
        policy = CommunicationPolicy()
        state = SessionState()
        state = policy.record_tool_call(
            state, "check", evidence={"has_changes": True}
        )
        assert state.phase == TaskPhase.PATCH_EXISTS

    def test_check_without_changes_stays_exploring(self) -> None:
        """Agent calls check-diff on an empty diff → stays in EXPLORING."""
        policy = CommunicationPolicy()
        state = SessionState()
        state = policy.record_tool_call(
            state, "check", evidence={"has_changes": False}
        )
        assert state.phase == TaskPhase.EXPLORING

    def test_check_with_no_evidence_stays_exploring(self) -> None:
        """Agent calls check-diff but handler didn't produce evidence → safe default."""
        policy = CommunicationPolicy()
        state = SessionState()
        state = policy.record_tool_call(state, "check", evidence=None)
        assert state.phase == TaskPhase.EXPLORING

    def test_check_with_empty_evidence_stays_exploring(self) -> None:
        """Evidence dict exists but missing has_changes key."""
        policy = CommunicationPolicy()
        state = SessionState()
        state = policy.record_tool_call(state, "check", evidence={})
        assert state.phase == TaskPhase.EXPLORING

    def test_validate_with_changes_transitions(self) -> None:
        """validate is also a check tool."""
        policy = CommunicationPolicy()
        state = SessionState()
        state = policy.record_tool_call(
            state, "validate", evidence={"has_changes": True}
        )
        assert state.phase == TaskPhase.PATCH_EXISTS

    def test_reverted_diff_no_transition(self) -> None:
        """Agent wrote code, then reverted everything. Diff is empty.
        Real SWE-bench scenario: check-diff called on a no-op patch."""
        policy = CommunicationPolicy()
        state = SessionState()
        state = policy.record_edit(state)  # Agent edited
        assert state.phase == TaskPhase.EDITING
        # Then reverted — diff is empty
        state = policy.record_tool_call(
            state, "check", evidence={"has_changes": False}
        )
        # Should NOT transition to PATCH_EXISTS
        assert state.phase == TaskPhase.EDITING

    def test_non_check_tool_ignores_evidence(self) -> None:
        """Evidence only matters for check tools. impact with evidence is a no-op."""
        policy = CommunicationPolicy()
        state = SessionState()
        state = policy.record_tool_call(
            state, "impact", evidence={"has_changes": True}
        )
        assert state.phase == TaskPhase.EXPLORING


class TestFullAgentLifecycle:
    """Simulate a real SWE-bench agent solving a task end to end."""

    def test_happy_path_explore_edit_check_test(self) -> None:
        """Agent: orient → impact → edit → check → test → done."""
        policy = CommunicationPolicy()
        state = SessionState()

        # Explore phase
        state = policy.record_tool_call(state, "orient")
        assert state.phase == TaskPhase.EXPLORING

        state = policy.record_tool_call(state, "impact")
        assert state.phase == TaskPhase.EXPLORING

        # Agent edits code
        state = policy.record_edit(state)
        assert state.phase == TaskPhase.EDITING

        # Agent runs check with real changes
        state = policy.record_tool_call(
            state, "check", evidence={"has_changes": True}
        )
        assert state.phase == TaskPhase.PATCH_EXISTS

        # Agent runs tests
        state = policy.record_tool_call(state, "test")
        assert state.phase == TaskPhase.TESTED

        # No loop detected at any point
        assert policy.detect_loop(state) == LoopState.NORMAL

    def test_agent_stuck_in_search_loop(self) -> None:
        """Agent: impact → references → search → search → impact (still no edit).
        Should get redirect framing after 3rd search."""
        policy = CommunicationPolicy()
        state = SessionState()

        state = policy.record_tool_call(state, "impact")
        state = policy.record_tool_call(state, "references")
        state = policy.record_tool_call(state, "search")
        # 3 searches, no edits → spinning
        assert policy.detect_loop(state) == LoopState.SEARCH_SPINNING

        framing = policy.get_framing(state, "impact")
        assert framing is not None

    def test_agent_check_loops(self) -> None:
        """Agent: edit → check → check → check.
        Running check repeatedly on same patch is check looping."""
        policy = CommunicationPolicy()
        state = SessionState()
        state = policy.record_edit(state)

        state = policy.record_tool_call(
            state, "check", evidence={"has_changes": True}
        )
        assert state.phase == TaskPhase.PATCH_EXISTS

        state = policy.record_tool_call(
            state, "check", evidence={"has_changes": True}
        )
        assert policy.detect_loop(state) == LoopState.CHECK_LOOPING

        framing = policy.get_framing(state, "check")
        assert framing is not None
        assert "submit" in framing.lower()

    def test_tool_counts_accumulate_correctly(self) -> None:
        """Verify tool call counts are accurate through a real session."""
        policy = CommunicationPolicy()
        state = SessionState()

        state = policy.record_tool_call(state, "impact")
        state = policy.record_tool_call(state, "impact")
        state = policy.record_tool_call(state, "references")

        assert state.tools_called["impact"] == 2
        assert state.tools_called["references"] == 1
        assert state.last_tool == "references"

    def test_state_immutability(self) -> None:
        """SessionState is frozen — each record_tool_call returns a NEW state."""
        policy = CommunicationPolicy()
        state_1 = SessionState()
        state_2 = policy.record_tool_call(state_1, "impact")

        assert state_1 is not state_2
        assert state_1.tools_called == {}
        assert state_2.tools_called == {"impact": 1}
        # Original unchanged
        assert state_1.last_tool is None
