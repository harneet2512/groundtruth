"""Tests for L5 trajectory governor, classifier, parsers, and hooks."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from groundtruth.trajectory.state import (
    L5TrajectoryState,
    IterationBand,
    AgentPhase,
    FailureSnapshot,
    compute_band,
)
from groundtruth.trajectory.classifier import (
    classify_command,
    classify_observation,
    is_verification_command,
    is_env_failure,
    CommandKind,
)
from groundtruth.trajectory.parsers import (
    FailureRecord,
    PytestParser,
    GenericTracebackParser,
    TscParser,
    MypyParser,
    parse_failures,
)
from groundtruth.trajectory.hooks import (
    hook_no_durable_source_progress,
    hook_premature_commitment,
    hook_patch_hypothesis,
    hook_hypothesis_falsified,
    hook_same_failure_persisted,
    hook_unsafe_finish,
)


class TestIterationBands:
    def test_early(self):
        assert compute_band(5, 100) == IterationBand.EARLY_EXPLORATION

    def test_mid(self):
        assert compute_band(40, 100) == IterationBand.MID_COMMITMENT

    def test_late(self):
        assert compute_band(70, 100) == IterationBand.LATE_REPAIR

    def test_finalization(self):
        assert compute_band(90, 100) == IterationBand.FINALIZATION

    def test_zero_max(self):
        assert compute_band(0, 0) == IterationBand.EARLY_EXPLORATION

    def test_boundary_25(self):
        assert compute_band(25, 100) == IterationBand.MID_COMMITMENT

    def test_boundary_60(self):
        assert compute_band(60, 100) == IterationBand.LATE_REPAIR

    def test_boundary_85(self):
        assert compute_band(85, 100) == IterationBand.FINALIZATION


class TestL5State:
    def test_init_once(self):
        state = L5TrajectoryState(instance_id="test", max_iter=100)
        assert state.current_iter == 0
        assert state.band == IterationBand.EARLY_EXPLORATION

    def test_update_iter_monotonic(self):
        state = L5TrajectoryState(instance_id="test", max_iter=100)
        state._prev_iter = 0
        state.update_iter(10, 100)
        assert state.current_iter == 10
        assert not state._injection_disabled

    def test_reset_detector(self):
        state = L5TrajectoryState(instance_id="test", max_iter=100)
        state._prev_iter = 50
        state.current_iter = 50
        state.update_iter(10, 100)
        assert state._injection_disabled
        assert "iter_decreased" in state._disable_reason

    def test_record_source_edit(self):
        state = L5TrajectoryState(instance_id="test")
        state.record_source_edit("src/auth.py")
        assert "src/auth.py" in state.edited_source_files
        assert state.phase == AgentPhase.EDITING

    def test_record_verification_pass(self):
        state = L5TrajectoryState(instance_id="test")
        state._prev_iter = 0
        state.update_iter(10, 100)
        state.record_verification(True)
        assert state.last_passing_verification_iter == 10
        assert not state.has_unresolved_failure()

    def test_record_verification_fail(self):
        state = L5TrajectoryState(instance_id="test")
        state._prev_iter = 0
        state.update_iter(10, 100)
        snap = FailureSnapshot(failing_unit="test_auth", assertion_or_error="assert x == 1")
        state.record_verification(False, snap)
        assert state.last_failing_verification_iter == 10
        assert state.has_unresolved_failure()

    def test_repeated_failure(self):
        state = L5TrajectoryState(instance_id="test")
        state._prev_iter = 0
        snap1 = FailureSnapshot(failing_unit="test_auth", assertion_or_error="assert x == 1")
        state.update_iter(10, 100)
        state.record_verification(False, snap1)
        assert state.repeated_failure_count == 0

        snap2 = FailureSnapshot(failing_unit="test_auth", assertion_or_error="assert x == 1")
        state.update_iter(20, 100)
        state.record_verification(False, snap2)
        assert state.repeated_failure_count == 1


class TestClassifier:
    def test_pytest(self):
        assert classify_command("pytest tests/") == CommandKind.TEST

    def test_python_m_pytest(self):
        assert classify_command("python -m pytest tests/test_auth.py") == CommandKind.TEST

    def test_npm_test(self):
        assert classify_command("npm test") == CommandKind.TEST

    def test_go_test(self):
        assert classify_command("go test ./...") == CommandKind.TEST

    def test_cargo_test(self):
        assert classify_command("cargo test") == CommandKind.TEST

    def test_tsc(self):
        assert classify_command("tsc --noEmit") == CommandKind.TYPECHECK

    def test_mypy(self):
        assert classify_command("mypy src/") == CommandKind.TYPECHECK

    def test_eslint(self):
        assert classify_command("eslint src/") == CommandKind.LINT

    def test_ruff(self):
        assert classify_command("ruff check src/") == CommandKind.LINT

    def test_npm_build(self):
        assert classify_command("npm run build") == CommandKind.BUILD

    def test_pip_install(self):
        assert classify_command("pip install pytest") == CommandKind.INSTALL

    def test_is_verification(self):
        assert is_verification_command("pytest tests/")
        assert is_verification_command("mypy src/")
        assert not is_verification_command("pip install x")

    def test_env_failure(self):
        assert is_env_failure("ModuleNotFoundError: No module named 'foo'. pip install foo")
        assert is_env_failure("bash: command not found")
        assert not is_env_failure("FAILED tests/test_auth.py::test_login - AssertionError")

    def test_classify_observation_test_fail(self):
        result = classify_observation(
            "pytest tests/",
            "FAILED tests/test_auth.py - assert 1 == 2\nexit code: 1",
        )
        assert result.command_kind == CommandKind.TEST
        assert result.is_verification
        assert result.is_failure
        assert not result.is_env_failure

    def test_classify_observation_test_pass(self):
        result = classify_observation(
            "pytest tests/",
            "all passed\nexit code: 0",
        )
        assert not result.is_failure


class TestPytestParser:
    def test_basic_assertion(self):
        output = """
============================= FAILURES =============================
________ test_auth ________
    def test_auth():
>       assert result == "expected"
E       AssertionError: assert 'actual' == 'expected'

tests/test_auth.py:42: AssertionError
=========================== short test summary info ============================
FAILED tests/test_auth.py::test_auth - AssertionError
"""
        parser = PytestParser()
        records = parser.parse(output)
        assert len(records) >= 1
        rec = records[0]
        assert rec.failing_unit == "tests/test_auth.py::test_auth"
        assert rec.parser_name == "pytest"
        assert rec.signature_hash

    def test_multiple_failures(self):
        output = """
FAILED tests/test_a.py::test_one - assert False
FAILED tests/test_b.py::test_two - TypeError
FAILED tests/test_c.py::test_three - ValueError
"""
        parser = PytestParser()
        records = parser.parse(output)
        assert len(records) == 3

    def test_render_compact(self):
        rec = FailureRecord(
            failing_unit="test_auth",
            assertion_or_error="assert x == 1",
            expected="1",
            actual="2",
        )
        text = rec.render_compact()
        assert "FAILED: test_auth" in text
        assert "expected: 1" in text
        assert "actual:   2" in text
        assert len(text) <= 300


class TestTscParser:
    def test_basic(self):
        output = "src/auth.ts(42,10): error TS2345: Argument of type 'string' is not assignable."
        parser = TscParser()
        records = parser.parse(output)
        assert len(records) == 1
        assert records[0].file == "src/auth.ts"
        assert records[0].line == 42


class TestMypyParser:
    def test_basic(self):
        output = "src/auth.py:42: error: Incompatible return value type [return-value]"
        parser = MypyParser()
        records = parser.parse(output)
        assert len(records) == 1
        assert records[0].file == "src/auth.py"


class TestGenericTraceback:
    def test_basic(self):
        output = """
Traceback (most recent call last):
  File "/site-packages/pytest.py", line 100, in run
    result = func()
  File "src/auth.py", line 42, in validate
    return token.verify()
TypeError: cannot verify NoneType
"""
        parser = GenericTracebackParser()
        records = parser.parse(output)
        assert len(records) == 1
        assert records[0].file == "src/auth.py"
        assert records[0].exception_type == "TypeError"


class TestHooks:
    def _make_state(self, current_iter: int = 10, max_iter: int = 100) -> L5TrajectoryState:
        state = L5TrajectoryState(instance_id="test", max_iter=max_iter)
        state._prev_iter = 0
        state.update_iter(current_iter, max_iter)
        return state

    def test_no_durable_source_progress(self):
        state = self._make_state()
        msg = hook_no_durable_source_progress(state, "reproduce.py")
        assert msg is not None
        assert "No Durable Source Progress" in msg

    def test_no_durable_suppressed_when_source_exists(self):
        state = self._make_state()
        state.edited_source_files = ["src/auth.py"]
        msg = hook_no_durable_source_progress(state, "reproduce.py")
        assert msg is None

    def test_premature_commitment(self):
        state = self._make_state()
        msg = hook_premature_commitment(state, "src/auth.py", 0)
        assert msg is not None
        assert "Premature Commitment" in msg

    def test_premature_commitment_suppressed_late(self):
        state = self._make_state(75, 100)
        msg = hook_premature_commitment(state, "src/auth.py", 0)
        assert msg is None

    def test_hypothesis_falsified(self):
        state = self._make_state(30, 100)
        state.has_source_edit_before_last_failure = True
        state.edited_source_files = ["src/auth.py"]
        failure = FailureRecord(
            failing_unit="test_auth",
            assertion_or_error="assert x == 1",
            expected="1",
            actual="2",
        )
        msg = hook_hypothesis_falsified(state, failure)
        assert msg is not None
        assert "Hypothesis Falsified" in msg
        assert "test_auth" in msg

    def test_hypothesis_falsified_no_source_edit(self):
        state = self._make_state()
        state.has_source_edit_before_last_failure = False
        failure = FailureRecord(failing_unit="test_auth")
        msg = hook_hypothesis_falsified(state, failure)
        assert msg is None

    def test_hypothesis_falsified_late_includes_no_restart(self):
        state = self._make_state(75, 100)
        state.has_source_edit_before_last_failure = True
        state.edited_source_files = ["src/auth.py"]
        failure = FailureRecord(failing_unit="test_auth", assertion_or_error="assert x == 1")
        msg = hook_hypothesis_falsified(state, failure)
        assert msg is not None
        assert "do not restart exploration" in msg.lower()

    def test_same_failure_persisted(self):
        state = self._make_state()
        state.repeated_failure_count = 1
        failure = FailureRecord(failing_unit="test_auth")
        msg = hook_same_failure_persisted(state, failure)
        assert msg is not None
        assert "Same Failure Persisted" in msg

    def test_same_failure_not_repeated(self):
        state = self._make_state()
        state.repeated_failure_count = 0
        failure = FailureRecord(failing_unit="test_auth")
        msg = hook_same_failure_persisted(state, failure)
        assert msg is None

    def test_unsafe_finish_with_failure(self):
        state = self._make_state(90, 100)
        state.edited_source_files = ["src/auth.py"]
        state.last_failing_verification_iter = 85
        state.last_passing_verification_iter = 0
        state.failure_records = [{"failing_unit": "test_auth"}]
        msg = hook_unsafe_finish(state)
        assert msg is not None
        assert "Unsafe Finish" in msg

    def test_unsafe_finish_no_verification(self):
        state = self._make_state(90, 100)
        state.edited_source_files = ["src/auth.py"]
        state.verification_commands_run = 0
        msg = hook_unsafe_finish(state)
        assert msg is not None
        assert "no verification" in msg.lower()

    def test_unsafe_finish_all_green(self):
        state = self._make_state(90, 100)
        state.edited_source_files = ["src/auth.py"]
        state.verification_commands_run = 1
        state.last_passing_verification_iter = 85
        state.last_failing_verification_iter = 0
        msg = hook_unsafe_finish(state)
        assert msg is None


class TestStep75NoReset:
    """Critical integration test: step 75 must not reset or restart."""

    def test_step_75_appends_only(self):
        state = L5TrajectoryState(instance_id="test", max_iter=100)
        state._prev_iter = 74
        state.update_iter(75, 100)
        assert state.current_iter == 75
        assert state.band == IterationBand.LATE_REPAIR
        assert not state._injection_disabled

        state.record_source_edit("src/auth.py")
        state.has_source_edit_before_last_failure = True

        failure = FailureRecord(
            failing_unit="test_auth",
            assertion_or_error="assert validate(token) == True",
            expected="True",
            actual="False",
        )
        msg = hook_hypothesis_falsified(state, failure)
        assert msg is not None
        assert "do not restart exploration" in msg.lower()
        assert "75/100" in msg
        assert state.current_iter == 75
