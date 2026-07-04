"""RC-14 unit tests — subprocess lifecycle, hard_cap math, signal forwarder.

Covers the closed findings A-009 / A-010 / A-020 / E-020 / E-021 / E-022.

These tests do NOT spawn a real SWE-agent batch. They exercise the helpers
in isolation against fakes so they can run inside `pytest -x` on any
developer box without docker / GPU / network.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "swebench" / "swe_agent_smoke_runner.py"


@pytest.fixture(scope="module")
def smoke_runner():
    """Load swe_agent_smoke_runner.py as a module without running main()."""
    if not RUNNER_PATH.is_file():
        pytest.skip(f"smoke runner not found at {RUNNER_PATH}")
    # The runner imports `image_name_resolver` at top-level via a sibling
    # script directory; add that dir to sys.path or stub the import.
    runner_dir = str(RUNNER_PATH.parent)
    if runner_dir not in sys.path:
        sys.path.insert(0, runner_dir)
    try:
        import image_name_resolver  # noqa: F401
    except ImportError:
        # Stub it so the runner module loads cleanly.
        stub = type(sys)("image_name_resolver")
        stub.resolve_image_name = lambda *a, **kw: ""  # type: ignore[attr-defined]
        sys.modules["image_name_resolver"] = stub

    spec = importlib.util.spec_from_file_location(
        "smoke_runner_under_test", RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["smoke_runner_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---- A-020 / E-022: hard_cap math is math.ceil-correct --------------------


class TestComputeHardCapSeconds:
    def test_under_saturated_5_4_is_4500_not_2250(self, smoke_runner):
        """5 tasks / 4 workers: longest chain is 2 (one worker gets 2),
        so cap = 1800 * 1.25 * 2 = 4500. Old integer-floor gave 2250.
        """
        result = smoke_runner._compute_hard_cap_seconds(
            cap_seconds=1800, task_count=5, workers=4
        )
        assert result == 4500, (
            f"expected 4500 (math.ceil(5/4)=2), got {result}"
        )

    def test_saturated_30_4_chain_is_8(self, smoke_runner):
        """30 tasks / 4 workers: math.ceil(30/4) = 8."""
        result = smoke_runner._compute_hard_cap_seconds(
            cap_seconds=1800, task_count=30, workers=4
        )
        assert result == int(1800 * 1.25 * 8) == 18000

    def test_exactly_divisible_keeps_existing_behavior(self, smoke_runner):
        """4 tasks / 4 workers: math.ceil(4/4) = 1. Same as old code."""
        result = smoke_runner._compute_hard_cap_seconds(
            cap_seconds=1800, task_count=4, workers=4
        )
        assert result == 2250

    def test_single_task_chain_is_1(self, smoke_runner):
        result = smoke_runner._compute_hard_cap_seconds(
            cap_seconds=1800, task_count=1, workers=4
        )
        assert result == 2250

    def test_more_workers_than_tasks_chain_floor_is_1(self, smoke_runner):
        """3 tasks / 4 workers — chain is 1 (math.ceil(3/4)=1)."""
        result = smoke_runner._compute_hard_cap_seconds(
            cap_seconds=1800, task_count=3, workers=4
        )
        assert result == 2250

    def test_zero_workers_treated_as_one(self, smoke_runner):
        """Defensive: workers=0 must not divide-by-zero; falls back to 1."""
        result = smoke_runner._compute_hard_cap_seconds(
            cap_seconds=1800, task_count=5, workers=0
        )
        # safe_workers=max(1,0)=1, ceil(5/1)=5, 1800*1.25*5 = 11250
        assert result == 11250

    def test_zero_cap_returns_none(self, smoke_runner):
        assert smoke_runner._compute_hard_cap_seconds(
            cap_seconds=0, task_count=5, workers=4
        ) is None

    def test_none_cap_returns_none(self, smoke_runner):
        assert smoke_runner._compute_hard_cap_seconds(
            cap_seconds=None, task_count=5, workers=4
        ) is None

    def test_zero_tasks_does_not_crash(self, smoke_runner):
        """Edge case: empty task list. Should still return a finite cap
        because main() validates task_count > 0 upstream and we want
        defensive behavior here."""
        result = smoke_runner._compute_hard_cap_seconds(
            cap_seconds=1800, task_count=0, workers=4
        )
        # safe_count=max(1,0)=1, ceil(1/4)=1, 1800*1.25*1=2250
        assert result == 2250


# ---- A-009 / A-010 / E-020 / E-021: signal forwarder ----------------------


class _FakeProc:
    """Minimal stand-in for subprocess.Popen used by the signal forwarder."""

    def __init__(self, pid: int = 12345):
        self.pid = pid
        self.terminate_called = 0
        self.kill_called = 0
        self._returncode: int | None = None

    def terminate(self) -> None:
        self.terminate_called += 1

    def kill(self) -> None:
        self.kill_called += 1
        self._returncode = -9

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return self._returncode if self._returncode is not None else 0

    @property
    def returncode(self) -> int | None:
        return self._returncode


def _is_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


class TestSignalForwarder:
    def test_install_and_restore_does_not_raise(self, smoke_runner):
        """Forwarder installs handlers, returns state, restores cleanly."""
        if not _is_main_thread():
            pytest.skip("signal.signal requires main thread")
        if os.name == "nt":
            # Windows doesn't deliver SIGTERM to processes the same way
            # POSIX does. The handler installer should still no-op safely.
            pass
        proc = _FakeProc()
        state = smoke_runner._install_sigterm_forwarder(proc)
        assert state["fired"] is False
        # _prev_handlers must be a dict the restore helper can iterate.
        assert isinstance(state["_prev_handlers"], dict)
        smoke_runner._restore_signal_handlers(state)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX signal delivery semantics not portable to Windows",
    )
    def test_sigterm_forwards_to_child_and_sets_fired(self, smoke_runner):
        """SIGTERM to self must call proc.terminate() exactly once and
        flip state['fired'] to True. Second SIGTERM is a no-op (single-fire)."""
        if not _is_main_thread():
            pytest.skip("signal.signal requires main thread")
        proc = _FakeProc()
        state = smoke_runner._install_sigterm_forwarder(proc)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            # Give Python a moment to deliver the signal.
            time.sleep(0.1)
            assert state["fired"] is True, "handler did not fire"
            assert state["signum"] == signal.SIGTERM
            assert proc.terminate_called == 1, (
                f"expected 1 terminate, got {proc.terminate_called}"
            )

            # Second SIGTERM should NOT re-fire (single-fire policy
            # documented in addendum ADD-RC-14-2).
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.1)
            assert proc.terminate_called == 1, (
                "single-fire policy violated — second SIGTERM re-fired"
            )
        finally:
            smoke_runner._restore_signal_handlers(state)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX signal delivery semantics not portable to Windows",
    )
    def test_sigint_also_forwards(self, smoke_runner):
        """The forwarder must hook BOTH SIGTERM and SIGINT — operator
        Ctrl-C must also drive the SWE-agent flush path."""
        if not _is_main_thread():
            pytest.skip("signal.signal requires main thread")
        proc = _FakeProc()
        state = smoke_runner._install_sigterm_forwarder(proc)
        try:
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.1)
            assert state["fired"] is True
            assert state["signum"] == signal.SIGINT
            assert proc.terminate_called == 1
        finally:
            smoke_runner._restore_signal_handlers(state)


# ---- _wait_loop teardown timing (A-010) -----------------------------------


class _SlowDeathProc:
    """Popen-shaped fake that survives `terminate` (simulates a wedged
    SWE-agent batch) so we can assert the new 60s post-SIGTERM wait
    eventually escalates to .kill()."""

    def __init__(self):
        self.pid = 99999
        self._terminated_at: float | None = None
        self._killed = False
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._terminated_at = time.monotonic()

    def kill(self) -> None:
        self._killed = True
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self._killed:
            return -9
        # Simulate a child that does not respond to SIGTERM.
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0.0)

    @property
    def returncode(self) -> int | None:
        return self._returncode


class TestWaitLoopTeardown:
    def test_wait_loop_post_sigterm_timeout_is_60s_not_30s(
        self, smoke_runner, monkeypatch
    ):
        """The post-SIGTERM wait must be 60s. We monkeypatch proc.wait to
        record the timeout it was called with."""
        proc = _SlowDeathProc()
        recorded: list[float | None] = []

        original_wait = proc.wait

        def _spy_wait(timeout: float | None = None) -> int:
            recorded.append(timeout)
            return original_wait(timeout=timeout)

        proc.wait = _spy_wait  # type: ignore[assignment]

        # hard_wall_clock_s=0 forces immediate cap-exceeded path.
        smoke_runner._wait_loop(
            proc=proc,  # type: ignore[arg-type]
            output_dir=Path("."),
            expected_ids=[],
            poll_s=0.001,
            hard_wall_clock_s=0,
        )

        # First wait call (post-SIGTERM) must use timeout=60.
        assert recorded, "wait was never called"
        assert recorded[0] == 60, (
            f"post-SIGTERM wait must be 60s (RC-14 fix), got {recorded[0]}"
        )
        # And after timeout, kill must have been called.
        assert proc._killed is True, (
            "wait_loop must escalate to kill() after SIGTERM timeout"
        )
