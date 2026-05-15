"""Tests for L5 unverified patch detection (Change 1).

Covers: VerificationTarget enum, classify_verification_targeting(),
state.has_unverified_patch(), hook_unverified_patch(), hook_unsafe_finish
with unverified patch, and governor integration.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from groundtruth.trajectory.classifier import (
    VerificationTarget,
    classify_verification_targeting,
)
from groundtruth.trajectory.governor import L5Governor
from groundtruth.trajectory.hooks import (
    hook_unverified_patch,
    hook_unsafe_finish,
)
from groundtruth.trajectory.state import L5TrajectoryState


# ── classify_verification_targeting ────────────────────────────────────


class TestClassifyVerificationTargeting:

    def test_broad_pytest_bare(self):
        assert classify_verification_targeting(
            "pytest", ["src/auth.py"]
        ) == VerificationTarget.BROAD_PROJECT_VERIFICATION

    def test_broad_pytest_tests_dir(self):
        assert classify_verification_targeting(
            "pytest tests/", ["src/auth.py"]
        ) == VerificationTarget.BROAD_PROJECT_VERIFICATION

    def test_broad_npm_test(self):
        assert classify_verification_targeting(
            "npm test", ["src/index.ts"]
        ) == VerificationTarget.BROAD_PROJECT_VERIFICATION

    def test_broad_go_test_all(self):
        assert classify_verification_targeting(
            "go test ./...", ["pkg/server.go"]
        ) == VerificationTarget.BROAD_PROJECT_VERIFICATION

    def test_broad_cargo_test(self):
        assert classify_verification_targeting(
            "cargo test", ["src/lib.rs"]
        ) == VerificationTarget.BROAD_PROJECT_VERIFICATION

    def test_targeted_to_edited_file(self):
        result = classify_verification_targeting(
            "pytest tests/test_auth.py", ["src/auth.py"]
        )
        assert result == VerificationTarget.TARGETED_TO_EDITED_FILE

    def test_targeted_to_edited_file_with_cd_prefix(self):
        result = classify_verification_targeting(
            "cd /workspace/repo && pytest tests/test_auth.py -v", ["src/auth.py"]
        )
        assert result == VerificationTarget.TARGETED_TO_EDITED_FILE

    def test_targeted_to_edited_symbol_k_flag(self):
        result = classify_verification_targeting(
            "pytest -k auth", ["src/auth.py"]
        )
        assert result == VerificationTarget.TARGETED_TO_EDITED_SYMBOL

    def test_targeted_to_related_test(self):
        result = classify_verification_targeting(
            "pytest tests/test_config.py",
            ["src/auth.py"],
            related_test_files=["tests/test_config.py"],
        )
        assert result == VerificationTarget.TARGETED_TO_RELATED_TEST

    def test_irrelevant_specific_test(self):
        result = classify_verification_targeting(
            "pytest tests/test_unrelated.py", ["src/auth.py"]
        )
        assert result == VerificationTarget.IRRELEVANT_VERIFICATION

    def test_unknown_non_verification(self):
        result = classify_verification_targeting(
            "ls -la", ["src/auth.py"]
        )
        assert result == VerificationTarget.UNKNOWN

    def test_is_targeted_true_for_targeted(self):
        assert VerificationTarget.TARGETED_TO_EDITED_FILE.is_targeted()
        assert VerificationTarget.TARGETED_TO_EDITED_SYMBOL.is_targeted()
        assert VerificationTarget.TARGETED_TO_RELATED_TEST.is_targeted()

    def test_is_targeted_false_for_broad(self):
        assert not VerificationTarget.BROAD_PROJECT_VERIFICATION.is_targeted()
        assert not VerificationTarget.IRRELEVANT_VERIFICATION.is_targeted()
        assert not VerificationTarget.UNKNOWN.is_targeted()

    def test_broad_python_m_pytest(self):
        assert classify_verification_targeting(
            "python -m pytest", ["src/auth.py"]
        ) == VerificationTarget.BROAD_PROJECT_VERIFICATION

    def test_broad_tox(self):
        assert classify_verification_targeting(
            "tox", ["src/auth.py"]
        ) == VerificationTarget.BROAD_PROJECT_VERIFICATION

    def test_targeted_module_in_command(self):
        result = classify_verification_targeting(
            "python -m pytest test/unit/module/config/test_config_mixin.py -v",
            ["src/cfnlint/runner.py"],
        )
        # "runner" not in the test path, but the test file is specific
        assert result == VerificationTarget.IRRELEVANT_VERIFICATION


# ── state.has_unverified_patch ─────────────────────────────────────────


class TestHasUnverifiedPatch:

    def test_edit_then_broad_pass(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_source_edit("src/auth.py")
        state.record_verification(True, target_level="broad_project_verification")
        assert state.has_unverified_patch()

    def test_edit_then_targeted_pass(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_source_edit("src/auth.py")
        state.record_verification(True, target_level="targeted_to_edited_file")
        assert not state.has_unverified_patch()

    def test_no_edit(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_verification(True, target_level="broad_project_verification")
        assert not state.has_unverified_patch()

    def test_edit_then_broad_then_targeted(self):
        state = L5TrajectoryState(instance_id="test")
        state.current_iter = 10
        state.record_source_edit("src/auth.py")
        state.current_iter = 11
        state.record_verification(True, target_level="broad_project_verification")
        assert state.has_unverified_patch()
        state.current_iter = 12
        state.record_verification(True, target_level="targeted_to_edited_file")
        assert not state.has_unverified_patch()

    def test_broad_resets_after_targeted(self):
        state = L5TrajectoryState(instance_id="test")
        state.current_iter = 10
        state.record_source_edit("src/auth.py")
        state.current_iter = 11
        state.record_verification(True, target_level="targeted_to_edited_file")
        assert state.broad_pass_after_edit_count == 0

    def test_verification_targeting_history(self):
        state = L5TrajectoryState(instance_id="test")
        state.current_iter = 10
        state.record_verification(True, target_level="broad_project_verification")
        assert len(state.verification_targeting_history) == 1
        assert state.verification_targeting_history[0]["target_level"] == "broad_project_verification"


# ── hook_unverified_patch ─────────────────────────────────────────────


class TestHookUnverifiedPatch:

    def test_fires_when_unverified(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_source_edit("src/auth.py")
        state.record_verification(True, target_level="broad_project_verification")
        msg = hook_unverified_patch(state)
        assert msg is not None
        assert "Unverified Patch" in msg
        assert "auth.py" in msg

    def test_does_not_fire_when_verified(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_source_edit("src/auth.py")
        state.record_verification(True, target_level="targeted_to_edited_file")
        msg = hook_unverified_patch(state)
        assert msg is None

    def test_debounce(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_source_edit("src/auth.py")
        state.record_verification(True, target_level="broad_project_verification")
        msg1 = hook_unverified_patch(state)
        assert msg1 is not None
        state.record_l5_emission("unverified_patch")
        msg2 = hook_unverified_patch(state)
        assert msg2 is None

    def test_debounce_resets_after_new_edit(self):
        state = L5TrajectoryState(instance_id="test")
        state.current_iter = 10
        state.record_source_edit("src/auth.py")
        state.current_iter = 11
        state.record_verification(True, target_level="broad_project_verification")
        state.record_l5_emission("unverified_patch")
        state.last_l5_iter = 11

        state.current_iter = 15
        state.record_source_edit("src/auth.py")
        state.current_iter = 16
        state.record_verification(True, target_level="broad_project_verification")
        msg = hook_unverified_patch(state)
        assert msg is not None

    def test_includes_suggestions(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_source_edit("src/auth.py")
        state.record_verification(True, target_level="broad_project_verification")
        msg = hook_unverified_patch(
            state, test_file_suggestions=["tests/test_auth.py"]
        )
        assert msg is not None
        assert "tests/test_auth.py" in msg

    def test_not_suppressed_in_finalization(self):
        state = L5TrajectoryState(instance_id="test", max_iter=100)
        state.current_iter = 90
        state.update_iter(90, 100)
        state.record_source_edit("src/auth.py")
        state.record_verification(True, target_level="broad_project_verification")
        msg = hook_unverified_patch(state)
        assert msg is not None


# ── hook_unsafe_finish catches unverified patch ────────────────────────


class TestUnsafeFinishCatchesUnverified:

    def test_fires_on_unverified_patch_at_finish(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_source_edit("src/auth.py")
        state.record_verification(True, target_level="broad_project_verification")
        msg = hook_unsafe_finish(state)
        assert msg is not None
        assert "Unsafe Finish" in msg
        assert "no targeted" in msg.lower() or "broad" in msg.lower()

    def test_no_fire_after_targeted_verification(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_source_edit("src/auth.py")
        state.record_verification(True, target_level="targeted_to_edited_file")
        msg = hook_unsafe_finish(state)
        assert msg is None

    def test_fires_on_no_verification(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_source_edit("src/auth.py")
        msg = hook_unsafe_finish(state)
        assert msg is not None
        assert "no verification" in msg.lower()

    def test_fires_on_unresolved_failure(self):
        from groundtruth.trajectory.state import FailureSnapshot
        state = L5TrajectoryState(instance_id="test")
        state.current_iter = 10
        state.record_source_edit("src/auth.py")
        state.current_iter = 11
        snapshot = FailureSnapshot(
            failing_unit="test_auth", assertion_or_error="assert failed"
        )
        snapshot.compute_hash()
        state.record_verification(False, snapshot)
        msg = hook_unsafe_finish(state)
        assert msg is not None
        assert "unresolved" in msg.lower()


# ── Governor integration ───────────────────────────────────────────────


def _make_cmd(command: str) -> MagicMock:
    action = MagicMock()
    type(action).__name__ = "CmdRunAction"
    action.command = command
    action.content = command
    action.thought = ""
    action.path = ""
    return action


def _make_edit(path: str) -> MagicMock:
    action = MagicMock()
    type(action).__name__ = "FileEditAction"
    action.path = path
    action.command = ""
    action.content = ""
    action.thought = ""
    return action


def _make_obs(content: str) -> MagicMock:
    obs = MagicMock()
    obs.content = content
    obs.stdout = content
    return obs


def _make_finish() -> MagicMock:
    action = MagicMock()
    type(action).__name__ = "AgentFinishAction"
    action.command = ""
    action.content = "finish"
    action.thought = ""
    action.path = ""
    return action


class TestGovernorUnverifiedPatch:

    def test_edit_then_broad_pass_fires(self, monkeypatch):
        monkeypatch.setenv("GT_REBUILD_L5", "1")
        gov = L5Governor(instance_id="test-unverified", max_iter=100)

        gov.after_interaction(
            _make_edit("src/auth.py"), _make_obs("ok"),
            action_count=10, max_iter=100,
        )

        result = gov.after_interaction(
            _make_cmd("pytest tests/"), _make_obs("5 passed\nexit code: 0\n"),
            action_count=11, max_iter=100,
        )
        assert result is not None
        assert "Unverified Patch" in result

    def test_edit_then_targeted_pass_no_fire(self, monkeypatch):
        monkeypatch.setenv("GT_REBUILD_L5", "1")
        gov = L5Governor(instance_id="test-targeted-ok", max_iter=100)

        gov.after_interaction(
            _make_edit("src/auth.py"), _make_obs("ok"),
            action_count=10, max_iter=100,
        )

        result = gov.after_interaction(
            _make_cmd("pytest tests/test_auth.py"), _make_obs("1 passed\nexit code: 0\n"),
            action_count=11, max_iter=100,
        )
        assert result is None

    def test_flag_off_no_fire(self, monkeypatch):
        monkeypatch.setenv("GT_REBUILD_L5", "0")
        gov = L5Governor(instance_id="test-flag-off", max_iter=100)

        gov.after_interaction(
            _make_edit("src/auth.py"), _make_obs("ok"),
            action_count=10, max_iter=100,
        )

        result = gov.after_interaction(
            _make_cmd("pytest tests/"), _make_obs("5 passed\nexit code: 0\n"),
            action_count=11, max_iter=100,
        )
        assert result is None

    def test_finish_with_unverified_patch(self, monkeypatch):
        monkeypatch.setenv("GT_REBUILD_L5", "1")
        gov = L5Governor(instance_id="test-finish-unverified", max_iter=100)

        gov.after_interaction(
            _make_edit("src/auth.py"), _make_obs("ok"),
            action_count=10, max_iter=100,
        )
        gov.after_interaction(
            _make_cmd("pytest tests/"), _make_obs("5 passed\nexit code: 0\n"),
            action_count=11, max_iter=100,
        )

        result = gov.after_interaction(
            _make_finish(), _make_obs(""),
            action_count=12, max_iter=100,
        )
        assert result is not None
        assert "Unsafe Finish" in result

    def test_existing_hypothesis_falsified_still_works(self, monkeypatch):
        monkeypatch.setenv("GT_REBUILD_L5", "1")
        gov = L5Governor(instance_id="test-hyp-still", max_iter=100)

        gov.after_interaction(
            _make_edit("src/auth.py"), _make_obs("ok"),
            action_count=10, max_iter=100,
        )

        result = gov.after_interaction(
            _make_cmd("pytest tests/test_auth.py -x"),
            _make_obs(
                "FAILED tests/test_auth.py::test_login - AssertionError\n"
                "exit code: 1\n"
            ),
            action_count=11, max_iter=100,
        )
        assert result is not None
        assert "Hypothesis Falsified" in result
