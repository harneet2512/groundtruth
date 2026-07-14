"""B1 substrate-correct execution — injectable EXECUTOR + timeout group-kill.

Stage-1 deterministic proofs for the `execute_test_command(..., executor=...)`
injection point and the `make_env_executor` mini-swe-agent adapter. The frozen
executor contract (a parallel agent builds to the same one):

    executor(cmd: list[str], cwd: str, timeout: int) -> tuple[int, str, str]
                                                          (exit_code, stdout, stderr)

Invariants pinned here:
  - executor=None  -> exactly today's subprocess path (byte-identical; the
    existing tests/runtime + tests/unit suites are the byte-identity witness).
  - executor set   -> routed through it; NO subprocess is ever spawned; the SAME
    classification is applied to the (exit_code, stdout, stderr) it returns.
  - make_env_executor -> wraps a mini-swe env.execute; marker-based exit capture
    (`; echo __GT_EXIT_$?__`) is robust to test output containing similar strings
    and is STRIPPED from stdout before returning.
  - default-path timeout hardening (process-group / tree kill) is output-invisible
    (still `timed_out=True`, empty tails) and does not change any result shape.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from groundtruth.runtime import test_runner as tr
from groundtruth.runtime.test_runner import (
    _parse_failing_test_names,
    _parse_passing_test_names,
    _parse_requested_test_names,
    _parse_test_output,
    execute_test_command,
    make_env_executor,
)

# canned pytest-shaped failure output — one FAILED nodeid + a summary line.
_PYTEST_FAIL = (
    "collected 1 item\n\n"
    "tests/test_pay.py::test_refund FAILED                                     [100%]\n\n"
    "=================================== FAILURES ===================================\n"
    "E   AssertionError: assert 1 == 2\n"
    "=========================== 1 failed in 0.03s ============================\n"
)


# ---------------------------------------------------------------------------
# (a) FakeEnv executor — routes through the injected executor, spawns NOTHING,
#     and classifies identically to the sync path for the same output.
# ---------------------------------------------------------------------------
def _boom(*_a, **_k):  # any subprocess spawn during the executor path is a bug
    raise AssertionError("a subprocess was spawned on the executor path")


def test_executor_routes_and_never_spawns(monkeypatch):
    # if the executor path ever touches subprocess, these bite.
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    calls: list[tuple] = []

    def fake_executor(cmd, cwd, timeout):
        calls.append((list(cmd), cwd, timeout))
        return (1, _PYTEST_FAIL, "")

    res = execute_test_command(
        "/repo/task", ["pytest", "tests/test_pay.py"], timeout_seconds=77,
        executor=fake_executor,
    )

    # routed through the executor with the frozen (cmd, cwd, timeout) contract.
    assert calls == [(["pytest", "tests/test_pay.py"], "/repo/task", 77)]
    assert res["executed"] is True
    assert res["exit_code"] == 1
    assert res["timed_out"] is False

    # classification MATCHES the sync path for identical output (path-independent).
    text = _PYTEST_FAIL + "\n" + ""
    expected_counts = _parse_test_output(text, ["pytest", "tests/test_pay.py"])
    assert res["passed"] == expected_counts["passed"]
    assert res["failed"] == expected_counts["failed"] == 1
    assert res["errored"] == expected_counts["errored"]
    assert res["failing_test_names"] == _parse_failing_test_names(text)
    assert res["all_passed"] is (
        res["exit_code"] == 0 and res["failed"] == 0 and res["errored"] == 0
    )
    # tails carry the executor's stdout/stderr verbatim (bounded).
    assert res["stdout_tail"].endswith("1 failed in 0.03s ============================\n")
    assert res["stderr_tail"] == ""


def test_executor_none_still_uses_subprocess(monkeypatch):
    # the DEFAULT path must still spawn — assert subprocess IS reached.
    seen: list = []
    real_popen = subprocess.Popen

    def watch_popen(*a, **k):
        seen.append(a[0] if a else None)
        return real_popen(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", watch_popen)
    res = execute_test_command(
        os.getcwd(),
        [sys.executable, "-c", "print('1 passed in 0.01s')"],
        timeout_seconds=10,
    )
    assert seen, "executor=None must reach subprocess"
    assert res["executed"] is True and res["exit_code"] == 0


def test_executor_bad_shape_is_spawn_error_not_crash(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    def bad(cmd, cwd, timeout):
        return "not-a-tuple"  # violates the frozen 3-tuple contract

    res = execute_test_command("/repo", ["pytest", "x"], executor=bad)
    assert res["executed"] is False
    assert res["reason"] == "spawn_error"


def test_pytest_progress_failure_name_is_the_nodeid_not_progress_bracket():
    text = (
        "tests/test_cli.py::test_inspect_no_args FAILED                 [ 89%]\n"
        "=========================== 1 failed in 0.03s ===========================\n"
    )

    assert _parse_failing_test_names(text) == [
        "tests/test_cli.py::test_inspect_no_args"
    ]


def test_pytest_summary_and_progress_forms_share_one_failure_identity():
    progress = "tests/test_cli.py::test_inspect_no_args FAILED [100%]\n"
    summary = (
        "FAILED tests/test_cli.py::test_inspect_no_args - assert False\n"
    )

    assert _parse_failing_test_names(progress) == _parse_failing_test_names(summary)


def test_pytest_passing_and_requested_nodeids_are_canonicalized():
    nodeid = "tests/test_cli.py::test_inspect_no_args"
    assert _parse_passing_test_names(f"{nodeid} PASSED [100%]\n") == [nodeid]
    assert _parse_passing_test_names(f"PASSED {nodeid}\n") == [nodeid]
    assert _parse_requested_test_names(f"pytest {nodeid} -x") == [nodeid]
    assert _parse_requested_test_names(f'pytest "{nodeid}" -x') == [nodeid]


def test_executor_timeout_is_timed_out(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    def slow(cmd, cwd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    res = execute_test_command("/repo", ["pytest", "x"], executor=slow, timeout_seconds=3)
    assert res["executed"] is True
    assert res["timed_out"] is True
    assert res["reason"] == "timeout"
    assert res["exit_code"] is None


# ---------------------------------------------------------------------------
# (b) make_env_executor — marker-based exit capture + shape defensiveness
# ---------------------------------------------------------------------------
def test_marker_parses_real_code_and_strips_marker():
    # the env returns the compound command's output. returncode=0 is the ECHO's
    # exit (masking the real fail) -> the marker MUST override it.
    def env(cmd_str, cwd=None):
        assert cmd_str.endswith("; echo __GT_EXIT_$?__")
        return {"output": _PYTEST_FAIL + "__GT_EXIT_1__\n", "returncode": 0}

    ex = make_env_executor(env)  # use_exit_marker default True
    code, out, err = ex(["pytest", "tests/test_pay.py"], "/repo/task", 60)
    assert code == 1                       # from the marker, NOT the env's 0
    assert "__GT_EXIT_" not in out         # marker stripped
    assert "1 failed in 0.03s" in out      # signal preserved
    assert err == ""


def test_marker_is_robust_to_similar_strings_in_output():
    # test output itself prints a look-alike token mid-stream; the TRUE marker is
    # the last full-line occurrence (the appended echo). Last-line wins.
    body = (
        "log: fake token __GT_EXIT_99__ inside a line should be ignored\n"
        "__GT_EXIT_77__\n"                 # an earlier full-line look-alike
        "1 failed\n"
        "__GT_EXIT_2__\n"                  # the real appended marker (last)
    )

    def env(cmd_str, cwd=None):
        return {"output": body, "returncode": 0}

    ex = make_env_executor(env)
    code, out, _ = ex(["pytest", "x"], "/repo", 60)
    assert code == 2                        # last full-line marker, not 99 / 77
    # only the real (last) marker line is stripped; the mid-line look-alike stays.
    assert "__GT_EXIT_99__ inside a line" in out
    assert out.count("__GT_EXIT_2__") == 0


def test_marker_absent_raises_valueerror():
    def env(cmd_str, cwd=None):
        return {"output": "no marker here", "returncode": 0}

    ex = make_env_executor(env)
    with pytest.raises(ValueError):
        ex(["pytest", "x"], "/repo", 60)


def test_cwd_kwarg_passed_when_accepted():
    seen = {}

    def env(cmd_str, cwd=None):
        seen["cwd"] = cwd
        return {"output": "ok\n__GT_EXIT_0__\n", "returncode": 0}

    ex = make_env_executor(env)
    code, _, _ = ex(["pytest", "x"], "/repo/task", 60)
    assert code == 0
    assert seen["cwd"] == "/repo/task"


def test_cwd_folded_into_cd_when_kwarg_rejected():
    received: list[str] = []

    def env(cmd_str):  # no cwd kwarg -> executor must fold cd into the command
        received.append(cmd_str)
        return {"output": "ok\n__GT_EXIT_0__\n", "returncode": 0}

    ex = make_env_executor(env)
    code, _, _ = ex(["pytest", "x"], "/repo/task", 60)
    assert code == 0
    assert received and received[0].startswith("cd -- ")
    assert "/repo/task" in received[0]


@pytest.mark.parametrize("ret,expect_code", [
    ({"output": "ok", "returncode": 3}, 3),
    ({"stdout": "ok", "exit_code": 4}, 4),
    ({"output": "ok", "code": "5"}, 5),          # numeric string code
    ((7, "ok"), 7),                              # (code, stdout)
    ((7, "ok", "e"), 7),                         # (code, stdout, stderr)
    (("ok", "e", 9), 9),                         # (stdout, stderr, code)
])
def test_no_marker_extracts_code_from_return_shapes(ret, expect_code):
    def env(cmd_str, cwd=None):
        # marker OFF -> the command must NOT carry the echo marker.
        assert "__GT_EXIT_" not in cmd_str and "; echo" not in cmd_str
        return ret

    ex = make_env_executor(env, use_exit_marker=False)
    code, out, _ = ex(["pytest", "x"], "/repo", 60)
    assert code == expect_code


def test_no_marker_object_like_return():
    class R:
        output = "ok"
        returncode = 6

    ex = make_env_executor(lambda c, cwd=None: R(), use_exit_marker=False)
    code, _, _ = ex(["pytest", "x"], "/repo", 60)
    assert code == 6


def test_no_marker_missing_code_raises():
    ex = make_env_executor(lambda c, cwd=None: "bare text, no code",
                           use_exit_marker=False)
    with pytest.raises(ValueError):
        ex(["pytest", "x"], "/repo", 60)


# ---------------------------------------------------------------------------
# timeout hardening — process-group / tree kill selection (deterministic)
# ---------------------------------------------------------------------------
class _FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid
        self.killed = False

    def kill(self):
        self.killed = True


def test_kill_tree_posix_uses_process_group(monkeypatch):
    killed_groups: list[tuple[int, int]] = []
    monkeypatch.setattr(tr.os, "name", "posix")
    monkeypatch.setattr(tr.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(tr.os, "killpg",
                        lambda pgid, sig: killed_groups.append((pgid, sig)),
                        raising=False)
    proc = _FakeProc(pid=4321)
    tr._kill_process_tree(proc)
    assert killed_groups == [(4321, tr._SIGKILL)]
    assert proc.killed is False  # group-kill, not a direct-child fallback


def test_kill_tree_posix_falls_back_to_direct_kill(monkeypatch):
    monkeypatch.setattr(tr.os, "name", "posix")
    monkeypatch.setattr(tr.os, "getpgid", lambda pid: pid, raising=False)

    def boom(pgid, sig):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(tr.os, "killpg", boom, raising=False)
    proc = _FakeProc()
    tr._kill_process_tree(proc)
    assert proc.killed is True  # degraded to prior direct-child behavior


def test_kill_tree_windows_uses_taskkill(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(tr.os, "name", "nt")

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    proc = _FakeProc(pid=999)
    tr._kill_process_tree(proc)
    assert calls and calls[0][:2] == ["taskkill", "/F"]
    assert "999" in calls[0]
    assert proc.killed is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group kill only")
def test_real_group_kill_reaps_grandchild(tmp_path):
    """POSIX-only: a hung test whose GRANDCHILD outlives the parent is killed too.

    Deterministic-ish: the shell backgrounds a grandchild that writes its PID and
    sleeps 60s, then the parent sleeps 60s. _run_subprocess times out at 1s and
    SIGKILLs the whole group. After a short grace the grandchild PID is dead.
    """
    import shutil
    import time

    if shutil.which("bash") is None and shutil.which("sh") is None:
        pytest.skip("no POSIX shell")
    pidfile = tmp_path / "gc.pid"
    script = (
        f"( echo $$ > {pidfile}; sleep 60 ) & "  # grandchild in the same group
        "sleep 60"                                # parent hangs
    )
    with pytest.raises(subprocess.TimeoutExpired):
        tr._run_subprocess(["sh", "-c", script], str(tmp_path), 1)
    time.sleep(0.5)
    gc_pid = int(pidfile.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(gc_pid, 0)  # grandchild is gone -> group-kill worked


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
