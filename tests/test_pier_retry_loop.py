"""Pier write->test->fix retry loop (red->green) — PIER_INTEGRATION.md Option 2.

The pier scaffolding audit found: pier's verifier runs AFTER the agent exits
and never feeds back (`trial.py:300-334`); `num_retries: 3` is litellm HTTP
retry, not task retry.  This suite proves the in-agent retry loop in
`GTMiniSweAgent._run_with_test_retry`:

  (a) after an attempt, the verifier command runs in the SAME container via
      `environment.exec` (edits persist across attempts);
  (b) pass/fail is parsed from the exit code + env-failure shapes;
  (c) a REAL failure is formatted as agent-consumable structured text
      (<test-feedback>, arm-neutral) and
  (d) prepended to the next attempt's instruction;
  (e) capped at 2 retries (3 total attempts) to bound cost.

CONFIRMED (the "confirm which" item): pier's Verifier is TRIAL-level — the
agent has no `self._task`, and the hidden tests/test.sh is uploaded to /tests
only at verify time (and would leak the hidden test patch if run mid-loop) —
so the retry calls the test command DIRECTLY (GT_RETRY_TEST_CMD or the
auto-detected repo-native runner), never pier's verifier.

RED before the loop: with GT_RETRY_ON_VERIFIER_FAIL set and a failing test
run, the agent still got exactly ONE attempt and no feedback text existed.
GREEN after: the failure output is injected and the agent re-enters the loop.

Regression: without the flag, behavior is identical to today — exactly one
super().run(), zero verifier execs.

Research: Reflexion (NeurIPS 2023, +11pp pass@1, ablating test execution
collapses the gain); Self-Debug (ICLR 2024); ORACLE-SWE (reproduction-test =
largest single signal).  gt_gt §15.6: the verification loop is the lever
context-only delivery cannot buy.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

pier = pytest.importorskip("pier", reason="pier is not installed in this environment")
from pier.environments.base import ExecResult  # noqa: E402

import artifact_deepswe.gt_agent as gt_agent  # noqa: E402
from artifact_deepswe.gt_agent import GTMiniSweAgent  # noqa: E402
from pier.agents.installed.mini_swe_agent import MiniSweAgent  # noqa: E402


class FakeEnv:
    """Stub BaseEnvironment: records exec calls, returns scripted results for
    the verifier command, rc=0 for everything else (archival mv/cp)."""

    def __init__(self, verifier_results: list[ExecResult]):
        self.exec_calls: list[str] = []
        self._verifier_results = list(verifier_results)

    async def exec(
        self, command: str, cwd=None, env=None, timeout_sec=None, user=None
    ) -> ExecResult:
        self.exec_calls.append(command)
        if "mini-swe-agent.trajectory" in command:  # attempt archival
            return ExecResult(stdout="", stderr="", return_code=0)
        if self._verifier_results:
            return self._verifier_results.pop(0)
        return ExecResult(stdout="", stderr="", return_code=0)

    def verifier_execs(self) -> list[str]:
        return [c for c in self.exec_calls if "mini-swe-agent.trajectory" not in c]


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    # clean arm/proof env so run() takes the plain non-proof path.
    for var in (
        "GT_PROOF_MODE",
        "GT_PORTABLE_SUBSTRATE",
        "GT_HOST_GRAPH_DB",
        "GT_CERT_DIR",
        "GT_BASELINE",
        "GT_RETRY_ON_VERIFIER_FAIL",
        "GT_RETRY_TEST_CMD",
        "GT_RETRY_TEST_TIMEOUT_SEC",
        "GT_GRAPH_DB",
        "GT_REPO_ROOT",
        "GT_HOST_SRC_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gt_agent, "_GT_BASELINE", False, raising=False)
    return GTMiniSweAgent(logs_dir=tmp_path)


@pytest.fixture()
def record_runs(monkeypatch):
    """Replace the parent MiniSweAgent.run with an instruction recorder."""
    runs: list[str] = []

    async def fake_run(self, instruction, environment, context):
        runs.append(instruction)

    monkeypatch.setattr(MiniSweAgent, "run", fake_run)
    return runs


def _fail(out: str = "FAIL: test_foo\n1 failed", rc: int = 1) -> ExecResult:
    return ExecResult(stdout=out, stderr="", return_code=rc)


def _pass() -> ExecResult:
    return ExecResult(stdout="2 passed", stderr="", return_code=0)


def _go(agent, env, instruction="fix the bug"):
    asyncio.run(agent.run(instruction, env, context=None))


# ---------------------------------------------------------------------------
# Regression: flag off (default) == today's behavior exactly.
# ---------------------------------------------------------------------------
def test_retry_off_single_attempt_no_verifier(agent, record_runs):
    env = FakeEnv([])
    _go(agent, env)
    assert len(record_runs) == 1
    assert env.exec_calls == [], "flag off must never exec a verifier command"
    assert "<test-feedback" not in record_runs[0]


# ---------------------------------------------------------------------------
# THE red->green: failure -> feedback injected -> second attempt.
# ---------------------------------------------------------------------------
def test_retry_fires_and_injects_feedback(agent, record_runs, monkeypatch):
    monkeypatch.setenv("GT_RETRY_ON_VERIFIER_FAIL", "2")
    env = FakeEnv([_fail(), _pass()])
    _go(agent, env, "fix the widget bug")
    assert len(record_runs) == 2, "a failing verifier must produce a retry attempt"
    first, second = record_runs
    assert "<test-feedback" not in first
    assert '<test-feedback attempt="2"' in second
    assert "Tests failed" in second
    assert "FAIL: test_foo" in second
    assert "Exit code: 1" in second
    # the ORIGINAL instruction is preserved after the feedback block.
    assert "fix the widget bug" in second
    assert second.index("<test-feedback") < second.index("fix the widget bug")
    # verifier ran twice (after attempt 1: fail; after attempt 2 not needed —
    # attempt 2 was the last allowed... no: retries=2 -> 3 attempts allowed;
    # attempt 2 passed -> loop stops. 2 verifier execs total.
    assert len(env.verifier_execs()) == 2
    # attempt-1 artifacts archived before the retry.
    assert any("mini-swe-agent.trajectory.attempt1" in c for c in env.exec_calls)


def test_retry_cap_three_total_attempts(agent, record_runs, monkeypatch):
    """Flag values above the cap are clamped: max 2 retries / 3 attempts."""
    monkeypatch.setenv("GT_RETRY_ON_VERIFIER_FAIL", "5")
    env = FakeEnv([_fail(), _fail(), _fail(), _fail()])
    _go(agent, env)
    assert len(record_runs) == 3, "cost bound: at most 3 total attempts"
    # no verifier exec after the final attempt (nothing left to retry into).
    assert len(env.verifier_execs()) == 2
    assert '<test-feedback attempt="2"' in record_runs[1]
    assert '<test-feedback attempt="3"' in record_runs[2]
    # feedback does NOT accumulate (one bounded block per attempt).
    assert record_runs[2].count("<test-feedback") == 1


def test_retry_pass_stops_immediately(agent, record_runs, monkeypatch):
    monkeypatch.setenv("GT_RETRY_ON_VERIFIER_FAIL", "2")
    env = FakeEnv([_pass()])
    _go(agent, env)
    assert len(record_runs) == 1
    assert len(env.verifier_execs()) == 1


# ---------------------------------------------------------------------------
# Correct-or-quiet: non-test failures never become feedback.
# ---------------------------------------------------------------------------
def test_env_failure_is_unverifiable_no_retry(agent, record_runs, monkeypatch):
    monkeypatch.setenv("GT_RETRY_ON_VERIFIER_FAIL", "2")
    env = FakeEnv([_fail("bash: go: command not found", rc=127)])
    _go(agent, env)
    assert len(record_runs) == 1, "an environment failure is not a test failure"


def test_no_runner_detected_is_unverifiable(agent, record_runs, monkeypatch):
    monkeypatch.setenv("GT_RETRY_ON_VERIFIER_FAIL", "2")
    env = FakeEnv([ExecResult(stdout="", stderr="", return_code=97)])
    _go(agent, env)
    assert len(record_runs) == 1


def test_exec_exception_is_unverifiable(agent, record_runs, monkeypatch):
    monkeypatch.setenv("GT_RETRY_ON_VERIFIER_FAIL", "1")

    class BoomEnv(FakeEnv):
        async def exec(self, command, **kw):
            self.exec_calls.append(command)
            raise RuntimeError("docker exec timeout")

    env = BoomEnv([])
    _go(agent, env)
    assert len(record_runs) == 1


# ---------------------------------------------------------------------------
# Command resolution + formatting.
# ---------------------------------------------------------------------------
def test_explicit_test_cmd_wins(agent, record_runs, monkeypatch):
    monkeypatch.setenv("GT_RETRY_ON_VERIFIER_FAIL", "1")
    monkeypatch.setenv("GT_RETRY_TEST_CMD", "bash /app/run_tests.sh")
    env = FakeEnv([_fail()])
    _go(agent, env)
    assert env.verifier_execs() == ["bash /app/run_tests.sh"]
    assert "Command: bash /app/run_tests.sh" in record_runs[1]


def test_autodetect_command_dispatches_languages(monkeypatch):
    """The default command is ONE language-dispatched surface (go/cargo/npm/
    pytest) rooted at the GT-detected repo root — no per-task logic."""
    monkeypatch.delenv("GT_RETRY_TEST_CMD", raising=False)
    cmd, display = gt_agent._retry_test_command()
    for marker in ("go test ./...", "cargo test", "npm test", "pytest", "gt_root.txt"):
        assert marker in cmd
    assert "auto-detected" in display


def test_feedback_tail_is_bounded(agent, record_runs, monkeypatch):
    monkeypatch.setenv("GT_RETRY_ON_VERIFIER_FAIL", "1")
    huge = "x" * 50_000 + "\nFAILED test_tail"
    env = FakeEnv([_fail(huge)])
    _go(agent, env)
    fb = record_runs[1]
    assert "…(truncated)…" in fb
    assert "FAILED test_tail" in fb  # the TAIL survives (most recent output)
    assert len(fb) < 10_000 + len(record_runs[0])


def test_garbage_flag_means_off(agent, record_runs, monkeypatch):
    monkeypatch.setenv("GT_RETRY_ON_VERIFIER_FAIL", "yes please")
    env = FakeEnv([_fail()])
    _go(agent, env)
    assert len(record_runs) == 1
    assert env.exec_calls == []


# ---------------------------------------------------------------------------
# Arm parity: the loop is harness mechanics — the baseline arm gets the SAME
# loop (paired measurement compares identical harnesses).
# ---------------------------------------------------------------------------
def test_baseline_arm_gets_the_same_loop(tmp_path, record_runs, monkeypatch):
    for var in ("GT_RETRY_TEST_CMD", "GT_PROOF_MODE", "GT_CERT_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gt_agent, "_GT_BASELINE", True, raising=False)
    monkeypatch.setenv("GT_RETRY_ON_VERIFIER_FAIL", "1")
    a = GTMiniSweAgent(logs_dir=tmp_path)
    env = FakeEnv([_fail()])
    asyncio.run(a.run("baseline instruction", env, context=None))
    assert len(record_runs) == 2
    assert "<test-feedback" in record_runs[1]
    # arm purity: no GT content in the baseline arm's instructions.
    assert "<gt-" not in record_runs[0] and "<gt-" not in record_runs[1]
    assert "GroundTruth" not in record_runs[0]
