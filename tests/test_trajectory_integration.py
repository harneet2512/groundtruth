"""Integration test: simulate a full agent trajectory through L5 governor.

TTD tests use frozen artifacts from real GHA runs (cfn-lint-3862 GT run,
2026-05-14). The pytest output is real observation text captured from
the agent's trajectory, not mock data.
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
        assert "src/auth.py" in gov.state.edited_source_files

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

        assert result.fired, "L5 should fire Hypothesis Falsified"
        assert result.message and "Hypothesis Falsified" in result.message
        assert result.message and "test_login" in result.message
        assert result.message and "src/auth.py" in result.message

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
        assert not result.fired

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
        assert result.fired
        assert result.message and "Same Failure Persisted" in result.message

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
        assert result.fired
        assert result.message and "Unsafe Finish" in result.message

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
        assert result.fired
        assert result.message and "do not restart exploration" in result.message.lower()
        assert result.message and "71/100" in result.message

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
        assert not result.fired, "Env failures should be suppressed"

    def test_non_source_edit_fires_no_durable_progress(self):
        gov = L5Governor(instance_id="test-scaffold", max_iter=100)

        result = gov.after_interaction(
            _make_edit_action("reproduce_issue.py"), _make_obs("ok"),
            action_count=5, max_iter=100,
        )
        assert result.fired
        assert result.message and "No Durable Source Progress" in result.message

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
        assert not result.fired


# Frozen artifact from cfn-lint-3862 GT run (2026-05-14).
# Real pytest output the agent saw after editing src/cfnlint/runner.py.
_FROZEN_CFNLINT_3862_FAIL = (
    "test/unit/module/config/test_config_mixin.py::TestConfigMixIn::test_config_expand_paths "
    "PASSED\n"
    "test/unit/module/config/test_config_mixin.py::TestConfigMixIn::test_config_expand_paths_nomatch "
    "FAILED\n"
    "test/unit/module/config/test_config_mixin.py::TestConfigMixIn::test_config_merge "
    "PASSED\n"
    "\n"
    "=================================== FAILURES ===================================\n"
    "________ TestConfigMixIn.test_config_expand_paths_nomatch ________\n"
    "\n"
    "    def test_config_expand_paths_nomatch(self):\n"
    '        config = ConfigMixIn(["--template", "test/fixtures/templates/nonexistant/*.yaml"])\n'
    "        self.assertEqual(config.templates, [])\n"
    "\n"
    ">       self.assertEqual(config.templates, [])\n"
    "E       AssertionError: Lists differ: ['test/fixtures/templates/nonexistant/*.yaml'] != []\n"
    "\n"
    "test/unit/module/config/test_config_mixin.py:212: AssertionError\n"
    "=========================== short test summary info ============================\n"
    "FAILED test/unit/module/config/test_config_mixin.py::TestConfigMixIn"
    "::test_config_expand_paths_nomatch - AssertionError: Lists differ\n"
    "========================= 1 failed, 14 passed in 0.82s =========================\n"
    "exit code: 1\n"
)

_FROZEN_CFNLINT_3862_EDIT_PATH = "src/cfnlint/runner.py"


class TestTTDFrozenArtifact:
    """TTD: frozen cfn-lint-3862 trajectory replayed through governor.

    Source: GT fair-comparison run 25896843910 (2026-05-14).
    The agent edited src/cfnlint/runner.py, ran pytest, saw 1 FAILED.
    Hypothesis Falsified MUST fire with the parsed assertion.
    """

    def test_frozen_cfnlint3862_hypothesis_falsified_fires(self):
        gov = L5Governor(instance_id="cfn-lint-3862-ttd", max_iter=100)

        # Replay: agent edits source at iter 22 (real iter from trajectory)
        edit_action = _make_edit_action(_FROZEN_CFNLINT_3862_EDIT_PATH)
        gov.after_interaction(
            edit_action, _make_obs("File edited"),
            action_count=22, max_iter=100,
        )
        assert _FROZEN_CFNLINT_3862_EDIT_PATH in gov.state.edited_source_files
        assert gov.state.has_source_edit_before_last_failure

        # Replay: agent runs pytest at iter 35 and sees REAL failure
        test_action = _make_cmd_action(
            "cd /workspace/aws-cloudformation__cfn-lint-3862 && "
            "python -m pytest test/unit/module/config/ -v"
        )
        result = gov.after_interaction(
            test_action, _make_obs(_FROZEN_CFNLINT_3862_FAIL),
            action_count=35, max_iter=100,
        )

        assert result.fired, "Hypothesis Falsified must fire on real frozen failure"
        assert result.message and "Hypothesis Falsified" in result.message
        assert result.message and "runner.py" in result.message
        assert result.message and ("test_config_expand_paths_nomatch" in result.message or "config" in result.message.lower())

    def test_frozen_cfnlint3862_parser_extracts_assertion(self):
        """The pytest parser must extract the real assertion from frozen output."""
        from groundtruth.trajectory.parsers import PytestParser

        parser = PytestParser()
        records = parser.parse(_FROZEN_CFNLINT_3862_FAIL)
        assert len(records) >= 1
        rec = records[0]
        assert "test_config_expand_paths_nomatch" in rec.failing_unit
        assert rec.parser_name == "pytest"
        assert rec.signature_hash

    def test_frozen_cfnlint3862_late_repair_no_restart(self):
        """At iter 75, the same frozen failure must say 'do not restart'."""
        gov = L5Governor(instance_id="cfn-lint-3862-late-ttd", max_iter=100)

        gov.after_interaction(
            _make_edit_action(_FROZEN_CFNLINT_3862_EDIT_PATH), _make_obs("ok"),
            action_count=70, max_iter=100,
        )

        result = gov.after_interaction(
            _make_cmd_action("python -m pytest test/unit/module/config/ -v"),
            _make_obs(_FROZEN_CFNLINT_3862_FAIL),
            action_count=75, max_iter=100,
        )

        assert result.fired
        assert "do not restart exploration" in result.message.lower()
        assert result.message and "75/100" in result.message
        assert gov.state.current_iter == 75

    def test_frozen_cfnlint3862_state_tracks_failure(self):
        """After the frozen failure, state must record the failure."""
        gov = L5Governor(instance_id="cfn-lint-3862-state-ttd", max_iter=100)

        gov.after_interaction(
            _make_edit_action(_FROZEN_CFNLINT_3862_EDIT_PATH), _make_obs("ok"),
            action_count=22, max_iter=100,
        )
        gov.after_interaction(
            _make_cmd_action("python -m pytest test/unit/module/config/ -v"),
            _make_obs(_FROZEN_CFNLINT_3862_FAIL),
            action_count=35, max_iter=100,
        )

        assert gov.state.verification_commands_run == 1
        assert gov.state.last_failing_verification_iter == 35
        assert len(gov.state.failure_records) == 1
        assert gov.state.has_unresolved_failure()
