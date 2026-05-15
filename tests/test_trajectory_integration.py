"""Integration test: simulate a full agent trajectory through L5 governor.

Verifies that the governor correctly fires hooks at the right moments
in a realistic sequence: edit source → run tests → tests fail → L5 fires.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from groundtruth.trajectory.governor import L5Governor


def _make_cmd_action(command: str) -> MagicMock:
    action = MagicMock()
    action.__class__.__name__ = "CmdRunAction"
    type(action).__name__ = "CmdRunAction"
    action.command = command
    action.content = command
    action.thought = ""
    action.path = ""
    return action


def _make_edit_action(path: str) -> MagicMock:
    action = MagicMock()
    action.__class__.__name__ = "FileEditAction"
    type(action).__name__ = "FileEditAction"
    action.path = path
    action.command = ""
    action.content = ""
    action.thought = ""
    return action


def _make_finish_action() -> MagicMock:
    action = MagicMock()
    action.__class__.__name__ = "AgentFinishAction"
    type(action).__name__ = "AgentFinishAction"
    action.command = ""
    action.content = "finish"
    action.thought = ""
    action.path = ""
    return action


def _make_obs(content: str) -> MagicMock:
    obs = MagicMock()
    obs.content = content
    obs.stdout = content
    return obs


class TestFullTrajectorySimulation:
    """Simulate: edit → pytest fail → L5 fires Hypothesis Falsified."""

    def test_edit_then_test_fail_fires_hypothesis_falsified(self):
        gov = L5Governor(instance_id="test-task", max_iter=100)

        # Step 1: Agent edits a source file (iter 10)
        edit_action = _make_edit_action("src/auth.py")
        edit_obs = _make_obs("File edited successfully")
        result = gov.after_interaction(
            edit_action, edit_obs, action_count=10, max_iter=100,
        )
        assert gov.state.edited_source_files == ["src/auth.py"]
        assert gov.state.has_source_edit_before_last_failure

        # Step 2: Agent runs pytest and it FAILS (iter 11)
        test_action = _make_cmd_action("pytest tests/test_auth.py -x")
        test_obs = _make_obs(
            "============================= FAILURES =============================\n"
            "________ test_login ________\n"
            "    def test_login():\n"
            ">       assert result == 'success'\n"
            "E       AssertionError: assert 'failure' == 'success'\n"
            "\n"
            "tests/test_auth.py:42: AssertionError\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_auth.py::test_login - AssertionError\n"
            "exit code: 1\n"
        )
        result = gov.after_interaction(
            test_action, test_obs, action_count=11, max_iter=100,
        )

        assert result is not None, "L5 should fire Hypothesis Falsified"
        assert "Hypothesis Falsified" in result
        assert "test_login" in result
        assert "src/auth.py" in result

    def test_edit_then_test_pass_no_fire(self):
        gov = L5Governor(instance_id="test-task-pass", max_iter=100)

        edit_action = _make_edit_action("src/auth.py")
        edit_obs = _make_obs("File edited successfully")
        gov.after_interaction(edit_action, edit_obs, action_count=10, max_iter=100)

        test_action = _make_cmd_action("pytest tests/test_auth.py")
        test_obs = _make_obs("1 passed\nexit code: 0\n")
        result = gov.after_interaction(
            test_action, test_obs, action_count=11, max_iter=100,
        )
        assert result is None

    def test_repeated_failure_fires_same_failure_persisted(self):
        gov = L5Governor(instance_id="test-repeat", max_iter=100)

        fail_output = (
            "FAILED tests/test_auth.py::test_login - AssertionError\n"
            "E       assert 'failure' == 'success'\n"
            "exit code: 1\n"
        )

        # Edit
        gov.after_interaction(
            _make_edit_action("src/auth.py"), _make_obs("ok"),
            action_count=10, max_iter=100,
        )

        # First failure
        gov.after_interaction(
            _make_cmd_action("pytest tests/"), _make_obs(fail_output),
            action_count=11, max_iter=100,
        )

        # Edit again
        gov.after_interaction(
            _make_edit_action("src/auth.py"), _make_obs("ok"),
            action_count=12, max_iter=100,
        )

        # Same failure again
        result = gov.after_interaction(
            _make_cmd_action("pytest tests/"), _make_obs(fail_output),
            action_count=13, max_iter=100,
        )
        assert result is not None
        assert "Same Failure Persisted" in result

    def test_unsafe_finish_with_unresolved_failure(self):
        gov = L5Governor(instance_id="test-finish", max_iter=100)

        # Edit
        gov.after_interaction(
            _make_edit_action("src/auth.py"), _make_obs("ok"),
            action_count=10, max_iter=100,
        )

        # Test fails
        gov.after_interaction(
            _make_cmd_action("pytest tests/"), _make_obs(
                "FAILED tests/test_auth.py::test_login\nexit code: 1\n"
            ),
            action_count=11, max_iter=100,
        )

        # Agent tries to finish
        result = gov.after_interaction(
            _make_finish_action(), _make_obs(""),
            action_count=12, max_iter=100,
        )
        assert result is not None
        assert "Unsafe Finish" in result

    def test_late_repair_includes_no_restart(self):
        gov = L5Governor(instance_id="test-late", max_iter=100)

        # Edit at iter 70
        gov.after_interaction(
            _make_edit_action("src/auth.py"), _make_obs("ok"),
            action_count=70, max_iter=100,
        )

        # Test fails at iter 71
        result = gov.after_interaction(
            _make_cmd_action("pytest tests/"), _make_obs(
                "FAILED tests/test_auth.py::test_login - AssertionError\n"
                "exit code: 1\n"
            ),
            action_count=71, max_iter=100,
        )
        assert result is not None
        assert "do not restart exploration" in result.lower()
        assert "71/100" in result

    def test_env_failure_suppressed(self):
        gov = L5Governor(instance_id="test-env", max_iter=100)

        gov.after_interaction(
            _make_edit_action("src/auth.py"), _make_obs("ok"),
            action_count=10, max_iter=100,
        )

        result = gov.after_interaction(
            _make_cmd_action("pytest tests/"), _make_obs(
                "ModuleNotFoundError: No module named 'foo'. pip install foo\n"
                "exit code: 1\n"
            ),
            action_count=11, max_iter=100,
        )
        assert result is None, "Env failures should be suppressed"

    def test_non_source_edit_fires_no_durable_progress(self):
        gov = L5Governor(instance_id="test-scaffold", max_iter=100)

        result = gov.after_interaction(
            _make_edit_action("reproduce_issue.py"), _make_obs("ok"),
            action_count=5, max_iter=100,
        )
        assert result is not None
        assert "No Durable Source Progress" in result

    def test_reset_detector_disables_injection(self):
        gov = L5Governor(instance_id="test-reset", max_iter=100)

        gov.after_interaction(
            _make_edit_action("src/auth.py"), _make_obs("ok"),
            action_count=50, max_iter=100,
        )

        # Simulate reset: iter goes backwards
        gov.after_interaction(
            _make_edit_action("src/auth.py"), _make_obs("ok"),
            action_count=10, max_iter=100,
        )
        assert gov.state._injection_disabled

        # Should not fire anything after reset
        result = gov.after_interaction(
            _make_cmd_action("pytest tests/"), _make_obs(
                "FAILED tests/test_auth.py::test_login\nexit code: 1\n"
            ),
            action_count=11, max_iter=100,
        )
        assert result is None
