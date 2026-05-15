"""Tests for L5b safety checker."""
from __future__ import annotations

from groundtruth.trajectory.hooks import L5bSafetyChecker


class TestL5bSafetyChecker:

    def test_restart_language_detected(self):
        for phrase in L5bSafetyChecker.RESTART_PHRASES:
            text = f"You should {phrase} the implementation"
            is_safe, reason = L5bSafetyChecker.validate(text, 0.3)
            assert not is_safe, f"Should catch restart phrase: {phrase}"
            assert "restart_language" in reason

    def test_late_broad_exploration_detected(self):
        for phrase in L5bSafetyChecker.BROAD_EXPLORATION_PHRASES:
            text = f"Try to {phrase}"
            is_safe, reason = L5bSafetyChecker.validate(text, 0.7)
            assert not is_safe, f"Should catch late exploration: {phrase}"
            assert "late_broad_exploration" in reason

    def test_early_broad_exploration_allowed(self):
        text = "Explore the codebase to find the issue"
        is_safe, _ = L5bSafetyChecker.validate(text, 0.2)
        assert is_safe

    def test_token_cap_enforced(self):
        text = "x " * 400
        is_safe, reason = L5bSafetyChecker.validate(text, 0.3)
        assert not is_safe
        assert "exceeds_token_cap" in reason

    def test_clean_message_passes(self):
        text = (
            "[GT L5: Unverified Patch]\n"
            "Evidence: broad test suite passed after editing src/auth.py.\n"
            "Next action: run a test that specifically exercises the changed function."
        )
        is_safe, reason = L5bSafetyChecker.validate(text, 0.3)
        assert is_safe
        assert reason is None

    def test_under_token_cap(self):
        text = "Short message."
        is_safe, _ = L5bSafetyChecker.validate(text, 0.5)
        assert is_safe

    def test_at_60pct_boundary_broad_blocked(self):
        text = "Explore the codebase for related files"
        is_safe, _ = L5bSafetyChecker.validate(text, 0.60)
        assert not is_safe

    def test_at_59pct_broad_allowed(self):
        text = "Explore the codebase for related files"
        is_safe, _ = L5bSafetyChecker.validate(text, 0.59)
        assert is_safe
