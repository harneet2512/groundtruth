"""Offline preflight tests for L5 Goku event-driven governor.

Decision 34: All 12 cases use mocked agent trajectories. No model calls.
No benchmark runs. No external repos required.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from groundtruth.trajectory.state import L5TrajectoryState, IterationBand
from groundtruth.trajectory.governor import L5Governor, L5Decision
from groundtruth.trajectory import hooks
from groundtruth.trajectory.event_classifier import (
    classify_file_kind,
    classify_check_kind,
    classify_event_bucket,
    classify_verification_strength,
)


@pytest.fixture(autouse=True)
def _enable_goku_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GT_L5_GOKU_EVENTS", "1")
    monkeypatch.setenv("GT_L5B_SAFETY_REQUIRED", "1")


@pytest.fixture
def state() -> L5TrajectoryState:
    s = L5TrajectoryState(instance_id="preflight_test", max_iter=100)
    s._initialized = True
    s._prev_iter = 0
    return s


@pytest.fixture
def governor(tmp_path: object) -> L5Governor:
    g = L5Governor.__new__(L5Governor)
    g.state = L5TrajectoryState(instance_id="preflight_test", max_iter=100)
    g.state._initialized = True
    g.state._prev_iter = 0
    g._log_entries = []
    return g


class _FakeAction:
    def __init__(self, cls_name: str = "CmdRunAction", command: str = "", path: str = ""):
        self._cls_name = cls_name
        self.command = command
        self.path = path
        self.content = command

    @property
    def __class__(self):
        class _Fake:
            __name__ = self._cls_name
        return _Fake()


class _FakeObs:
    def __init__(self, content: str = "", exit_code: int = 0):
        self.content = content
        self.stdout = content


# --- CASE 3: L3 next_action ignored -> L5 STRUCTURAL_WITNESS_IGNORED ---

class TestCase3StructuralWitnessIgnored:
    def test_fires_after_3_unrelated_actions(self, state: L5TrajectoryState) -> None:
        state.record_gt_next_action("READ_CALLER_CONTRACT", "src/callers.py", 10)
        state.record_action_after_gt("src/unrelated1.py")
        state.record_action_after_gt("src/unrelated2.py")
        state.record_action_after_gt("src/unrelated3.py")

        assert state.actions_since_gt_next_action == 3
        assert not state.structural_witness_followed

        msg = hooks.hook_structural_witness_ignored(
            state, witness_file="src/callers.py",
        )
        assert msg is not None
        # Diagnostic form (SWE-PRM NeurIPS 2025): no prescriptive directive.
        assert "Unexamined structural signal" in msg
        assert "Next action:" not in msg
        assert "src/callers.py" in msg

    def test_does_not_fire_if_followed(self, state: L5TrajectoryState) -> None:
        state.record_gt_next_action("READ_CALLER_CONTRACT", "src/callers.py", 10)
        state.record_action_after_gt("src/callers.py")

        assert state.structural_witness_followed
        msg = hooks.hook_structural_witness_ignored(state)
        assert msg is None

    def test_does_not_fire_before_3_actions(self, state: L5TrajectoryState) -> None:
        state.record_gt_next_action("READ_CALLER_CONTRACT", "src/callers.py", 10)
        state.record_action_after_gt("src/other.py")
        state.record_action_after_gt("src/other2.py")

        assert state.actions_since_gt_next_action == 2
        msg = hooks.hook_structural_witness_ignored(state)
        assert msg is None


# --- CASE 4: Weak verification after edit ---

class TestCase4WeakVerificationAfterEdit:
    def test_fires_on_broad_only(self, state: L5TrajectoryState) -> None:
        state.record_source_edit("src/auth.py")
        state.last_edit_iter = 5
        state.record_verification(True, target_level="broad_project_verification")

        assert state.has_unverified_patch()
        msg = hooks.hook_weak_verification_after_edit(state)
        assert msg is not None
        assert "Weak Verification" in msg
        assert "src/auth.py" in msg

    def test_does_not_fire_with_targeted(self, state: L5TrajectoryState) -> None:
        state.record_source_edit("src/auth.py")
        state.last_edit_iter = 5
        state.record_verification(True, target_level="targeted_to_edited_file")

        assert not state.has_unverified_patch()
        msg = hooks.hook_weak_verification_after_edit(state)
        assert msg is None


# --- CASE 5: Finish with unverified edit ---

class TestCase5FinishWithoutStructuralWitness:
    def test_fires_on_finish_no_witness(self, state: L5TrajectoryState) -> None:
        state.current_iter = 10
        state.record_source_edit("src/auth.py")
        state.structural_witness_followed = False

        msg = hooks.hook_finish_without_structural_witness(state)
        assert msg is not None
        # Diagnostic verify-before-finish form (no content prescription).
        assert "Finish without verification" in msg
        assert "Next action: inspect" not in msg

    def test_does_not_fire_if_witness_followed(self, state: L5TrajectoryState) -> None:
        state.record_source_edit("src/auth.py")
        state.structural_witness_followed = True

        msg = hooks.hook_finish_without_structural_witness(state)
        assert msg is None

    def test_does_not_fire_with_targeted_verification(self, state: L5TrajectoryState) -> None:
        state.current_iter = 5
        state.record_source_edit("src/auth.py")
        state.current_iter = 8
        state.record_verification(True, target_level="targeted_to_edited_file")

        msg = hooks.hook_finish_without_structural_witness(state)
        assert msg is None


# --- CASE 6: Patch collapsed ---

class TestCase6PatchCollapsed:
    def test_fires_on_diff_collapse(self, state: L5TrajectoryState) -> None:
        state.record_diff_snapshot(150)
        assert state.patch_nonzero_seen
        assert not state.patch_collapsed

        state.record_diff_snapshot(0)
        assert state.patch_collapsed
        assert state.durable_edit_lost

        msg = hooks.hook_patch_collapsed_or_lost(state)
        assert msg is not None
        assert "Patch Collapsed" in msg

    def test_does_not_fire_without_prior_nonzero(self, state: L5TrajectoryState) -> None:
        state.record_diff_snapshot(0)
        assert not state.patch_collapsed
        msg = hooks.hook_patch_collapsed_or_lost(state)
        assert msg is None


# --- CASE 7: No durable progress ---

class TestCase7NoDurableProgress:
    def test_fires_in_late_band(self, state: L5TrajectoryState) -> None:
        state.band = IterationBand.LATE_REPAIR
        state.current_iter = 70
        state.max_iter = 100

        msg = hooks.hook_no_durable_progress_goku(state)
        assert msg is not None
        assert "No Durable Progress" in msg

    def test_does_not_fire_in_early_band(self, state: L5TrajectoryState) -> None:
        state.band = IterationBand.EARLY_EXPLORATION
        msg = hooks.hook_no_durable_progress_goku(state)
        assert msg is None

    def test_does_not_fire_with_source_edit(self, state: L5TrajectoryState) -> None:
        state.band = IterationBand.LATE_REPAIR
        state.record_source_edit("src/fix.py")
        msg = hooks.hook_no_durable_progress_goku(state)
        assert msg is None


# --- CASE 9: Strong verification ---

class TestCase9StrongVerification:
    def test_targeted_pass_clears_unverified(self, state: L5TrajectoryState) -> None:
        state.current_iter = 5
        state.record_source_edit("src/auth.py")
        state.current_iter = 8
        state.record_verification(True, target_level="targeted_to_edited_file")

        assert not state.has_unverified_patch()
        msg = hooks.hook_weak_verification_after_edit(state)
        assert msg is None
        msg2 = hooks.hook_finish_without_structural_witness(state)
        assert msg2 is None


# --- CASE 10: Step 75 no restart ---

class TestCase10NoRestart:
    def test_safety_checker_blocks_restart(self) -> None:
        is_safe, reason = hooks.L5bSafetyChecker.validate(
            "Start over from scratch and explore the codebase", 0.75,
        )
        assert not is_safe
        assert reason is not None
        assert "restart_language" in reason

    def test_safety_checker_allows_do_not_restart(self) -> None:
        is_safe, _ = hooks.L5bSafetyChecker.validate(
            "Do not restart. Focus on the current edit.", 0.75,
        )
        assert is_safe

    def test_safety_checker_blocks_late_exploration(self) -> None:
        is_safe, reason = hooks.L5bSafetyChecker.validate(
            "Explore the codebase to find more clues.", 0.75,
        )
        assert not is_safe
        assert reason is not None
        assert "late_broad_exploration" in reason

    def test_safety_checker_blocks_long_messages(self) -> None:
        long_msg = "x" * 800
        is_safe, reason = hooks.L5bSafetyChecker.validate(long_msg, 0.5)
        assert not is_safe
        assert reason is not None
        assert "token_cap" in reason


# --- CASE 11: Low-confidence drift suppressed ---

class TestCase11LowConfidenceSuppressed:
    def test_goku_suppresses_medium_in_early_band(self, governor: L5Governor) -> None:
        governor.state.band = IterationBand.EARLY_EXPLORATION
        decision = governor._try_goku_emit(
            "WEAK_VERIFICATION_AFTER_EDIT", "MEDIUM",
            "[GT L5] Test message",
            trigger_reason="test",
        )
        assert decision.fired
        assert decision.suppressed
        assert "structured_only" in (decision.suppression_reason or "")

    def test_goku_suppresses_low_always(self, governor: L5Governor) -> None:
        governor.state.band = IterationBand.FINALIZATION
        decision = governor._try_goku_emit(
            "STALE_CONTEXT_PATH", "LOW",
            "[GT L5] Test message",
            trigger_reason="test",
        )
        assert decision.fired
        assert decision.suppressed
        assert "structured_only" in (decision.suppression_reason or "")


# --- Debounce + max emissions ---

class TestDebounceAndCap:
    def test_debounce_blocks_same_type_within_3_iters(self, state: L5TrajectoryState) -> None:
        state.current_iter = 10
        state.record_l5_goku_emission("STRUCTURAL_WITNESS_IGNORED")
        state.current_iter = 12

        allowed, reason = state.can_emit_l5("STRUCTURAL_WITNESS_IGNORED")
        assert not allowed
        assert "debounce" in reason

    def test_debounce_allows_different_type(self, state: L5TrajectoryState) -> None:
        state.current_iter = 10
        state.record_l5_goku_emission("STRUCTURAL_WITNESS_IGNORED")
        state.current_iter = 11

        allowed, _ = state.can_emit_l5("PATCH_COLLAPSED_OR_LOST")
        assert allowed

    def test_max_injections_cap(self, state: L5TrajectoryState) -> None:
        state.current_iter = 0
        for i in range(2):
            state.current_iter = i * 10
            state.record_l5_goku_emission(f"TYPE_{i}")

        allowed, reason = state.can_emit_l5("NEW_TYPE")
        assert not allowed
        assert "max_emissions" in reason or "max_injections" in reason


# --- Event classifier ---

class TestEventClassifier:
    def test_file_kind_source(self) -> None:
        assert classify_file_kind("src/auth.py") == "DURABLE_PRODUCT_FILE"

    def test_file_kind_test(self) -> None:
        assert classify_file_kind("tests/test_auth.py") == "VALIDATION_FILE"

    def test_file_kind_scaffold(self) -> None:
        assert classify_file_kind("reproduce_issue.py") == "SCAFFOLD_FILE"

    def test_file_kind_config(self) -> None:
        assert classify_file_kind("pyproject.toml") == "CONFIG_FILE"

    def test_file_kind_generated(self) -> None:
        assert classify_file_kind("generated/proto.py") == "GENERATED_FILE"

    def test_check_kind_targeted(self) -> None:
        result = classify_check_kind(
            "pytest tests/test_auth.py -k test_login",
            edited_files=["src/auth.py"],
        )
        assert result == "TARGETED_CHECK"

    def test_check_kind_broad(self) -> None:
        result = classify_check_kind("pytest", edited_files=["src/auth.py"])
        assert result == "BROAD_CHECK"

    def test_check_kind_static(self) -> None:
        result = classify_check_kind("mypy src/auth.py")
        assert result == "STATIC_SANITY"

    def test_check_kind_install(self) -> None:
        result = classify_check_kind("pip install -e .")
        assert result == "SETUP_OR_INSTALL"

    def test_event_bucket_edit(self) -> None:
        assert classify_event_bucket("edit_file") == "EDIT_COMMITMENT"

    def test_event_bucket_read(self) -> None:
        assert classify_event_bucket("read_file") == "OPEN_INSPECT"

    def test_event_bucket_search(self) -> None:
        assert classify_event_bucket("run_command", command="grep -r validate src/") == "SEARCH"

    def test_event_bucket_verification(self) -> None:
        assert classify_event_bucket("run_command", command="pytest tests/") == "VERIFICATION_CHECK"

    def test_event_bucket_finish(self) -> None:
        assert classify_event_bucket("finish", is_finish=True) == "FINISH_TERMINAL"

    def test_verification_strength_targeted(self) -> None:
        assert classify_verification_strength("TARGETED_CHECK") == "STRONG"

    def test_verification_strength_broad(self) -> None:
        assert classify_verification_strength("BROAD_CHECK") == "WEAK"

    def test_verification_strength_with_witness(self) -> None:
        assert classify_verification_strength("BROAD_CHECK", structural_witness_followed=True) == "STRONG"

    def test_no_framework_names_in_event_types(self) -> None:
        """L5 event types must never contain framework-specific names."""
        from groundtruth.telemetry.constants import VALID_L5_EVENT_TYPES
        forbidden = {"pytest", "jest", "cargo", "go_test", "npm_test", "rspec", "tox"}
        for et in VALID_L5_EVENT_TYPES:
            for fw in forbidden:
                assert fw not in et.lower(), f"L5 event type {et} contains framework name {fw}"
