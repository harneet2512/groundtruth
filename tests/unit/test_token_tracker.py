"""Tests for TokenTracker."""

from __future__ import annotations

from groundtruth.stats.token_tracker import TokenTracker


class TestEstimateTokens:
    def test_basic_estimation(self) -> None:
        assert TokenTracker.estimate_tokens("abcd") == 1
        assert TokenTracker.estimate_tokens("abcdefgh") == 2

    def test_empty_string(self) -> None:
        assert TokenTracker.estimate_tokens("") == 0


class TestTrack:
    def test_returns_estimated_tokens(self) -> None:
        tracker = TokenTracker()
        tokens = tracker.track("tool_a", "a" * 100)
        assert tokens == 25

    def test_accumulates_across_calls(self) -> None:
        tracker = TokenTracker()
        tracker.track("tool_a", "a" * 100)
        tracker.track("tool_b", "b" * 200)
        assert tracker.get_session_total() == 75  # 25 + 50


class TestBreakdown:
    def test_groups_by_tool(self) -> None:
        tracker = TokenTracker()
        tracker.track("tool_a", "a" * 100)
        tracker.track("tool_a", "a" * 100)
        tracker.track("tool_b", "b" * 200)
        breakdown = tracker.get_breakdown()
        assert breakdown["tool_a"] == 50
        assert breakdown["tool_b"] == 50


class TestFootprint:
    def test_footprint_structure(self) -> None:
        tracker = TokenTracker()
        tracker.track("tool_a", "a" * 100)
        footprint = tracker.get_footprint("tool_a", 25)
        assert footprint["this_call_tokens"] == 25
        assert footprint["session_total_tokens"] == 25
        assert isinstance(footprint["breakdown"], dict)
        assert footprint["breakdown"]["tool_a"] == 25
