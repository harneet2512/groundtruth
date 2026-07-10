"""B1 green->base->red DIFFERENTIAL — REMOTE-container (executor) routing tests.

Wave-1 built the differential, but its worktree git ops (``_git`` /
``_make_base_worktree`` / ``_cleanup_base_worktree``) ran on the LOCAL host via
``subprocess``. On the mini-swe-agent DeepSWE path the repo lives in a REMOTE
Docker/Singularity container, so a host-side ``git worktree add`` operates on the
host fs, not the task tree -> the base run is ``unavailable`` -> the differential
silently never fires -> B1 goes MUTE on assertion failures again.

These tests pin the fix: when an ``executor`` is provided the worktree add / base
run / worktree remove ALL route through it (the container's namespace), the base
tmp path is DERIVED from ``repo_root`` (never a host ``tempfile`` dir), and every
fail-safe outcome (git-add non-zero, executor raises, base-red) degrades to
``False`` (quiet) — never a false attribution, never a crash.

No real git / no real subprocess: a recording fake-executor supplies canned git +
covering-run outputs, and ``subprocess.run`` / ``tempfile.mkdtemp`` are monkeypatched
to RAISE so any accidental local-host fallback is caught red-handed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from groundtruth.runtime import covering_runner as cr


class RecordingExecutor:
    """Records every ``(cmd, cwd, timeout)`` and returns canned outputs.

    git ``rev-parse``/``worktree add``/``worktree remove``/``worktree prune``/``rm``
    -> rc 0. Any non-git command is the BASE covering run -> returns ``base_run``
    (default a green ``1 passed``)."""

    def __init__(self, base_run: tuple[int | None, str, str] = (0, "1 passed", "")):
        self.calls: list[dict] = []
        self._base_run = base_run

    def __call__(self, cmd, cwd, timeout):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "timeout": timeout})
        if cmd and cmd[0] == "git":
            return (0, "abc1234def" if "rev-parse" in cmd else "", "")
        if cmd and cmd[0] == "rm":
            return (0, "", "")
        return self._base_run  # the base covering-test run

    # --- convenience accessors over the recording -------------------------------
    def _git_calls(self, *tokens: str) -> list[list[str]]:
        return [
            c["cmd"] for c in self.calls
            if c["cmd"] and c["cmd"][0] == "git" and all(t in c["cmd"] for t in tokens)
        ]

    def worktree_add_target(self) -> str | None:
        for cmd in self._git_calls("worktree", "add"):
            # git -C <repo> worktree add --detach <wt> HEAD
            return cmd[cmd.index("--detach") + 1]
        return None


def _no_local_subprocess(monkeypatch):
    """Make ANY accidental local-host fallback loud: local subprocess or a host
    tempfile dir on the executor path must never happen."""
    def _boom_run(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("local subprocess.run must NOT run on the executor path")

    def _boom_mkdtemp(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("host tempfile.mkdtemp must NOT run on the executor path")

    monkeypatch.setattr(cr.subprocess, "run", _boom_run)
    monkeypatch.setattr(cr.tempfile, "mkdtemp", _boom_mkdtemp)


# ---------------------------------------------------------------------------
# RED-first: worktree ops ROUTE through the executor; base-green -> attributed.
#   (a) routed through the executor, never a local subprocess (monkeypatched raise)
#   (b) attribution True on base-green / current-red
#   (c) container tmp path DERIVED from repo_root, not a host tempfile dir
#   + cleanup (worktree remove) also routed through the executor  [finally-mutation]
# ---------------------------------------------------------------------------
def test_worktree_ops_route_through_executor_and_attribute(monkeypatch, tmp_path: Path):
    _no_local_subprocess(monkeypatch)
    repo_root = "/workspace/proj"  # a CONTAINER path, unrelated to the host tmp_path
    ex = RecordingExecutor(base_run=(0, "1 passed", ""))  # base GREEN
    current_red = {"verdict": "fail", "stdout_tail": "E   assert 1 == 2", "exit_code": 1}

    # full thread: is_red_attributable -> differential_attribution -> worktree ops.
    got = cr.is_red_attributable(
        current_red, {"mod.py"}, test_files=["test_mod.py"],
        repo_root=repo_root, covering_files=["test_mod.py"], executor=ex,
    )

    # (b) base GREEN + current RED => attributed => deliver.
    assert got is True, ex.calls

    # (a) the worktree ADD went through the executor with the exact git command.
    adds = ex._git_calls("worktree", "add")
    assert adds, f"worktree add never routed through the executor: {ex.calls}"
    wt = ex.worktree_add_target()
    assert adds[0] == ["git", "-C", repo_root, "worktree", "add", "--detach", wt, "HEAD"]

    # (a) the BASE covering run also went through the executor, cwd = the worktree.
    base_runs = [c for c in ex.calls if c["cmd"] and c["cmd"][0] not in ("git", "rm")]
    assert base_runs and base_runs[0]["cwd"] == wt, base_runs

    # (c) container tmp path DERIVED from repo_root (sibling of the repo), NOT a
    #     host tempfile dir. tempfile.mkdtemp is monkeypatched to raise, so simply
    #     reaching here already proves no host tmp was used; assert the shape too.
    assert wt.startswith("/workspace/.gt_base_"), wt
    assert str(tmp_path) not in wt  # nothing derived from a host tempdir

    # cleanup routed through the executor (finally on the executor path). This is
    # the assertion the "drop finally-cleanup" mutation must bite.
    removes = ex._git_calls("worktree", "remove")
    assert removes, f"worktree remove never routed through the executor: {ex.calls}"
    assert removes[0][:4] == ["git", "-C", repo_root, "worktree"]


def test_direct_differential_true_on_base_green_via_executor(monkeypatch, tmp_path: Path):
    _no_local_subprocess(monkeypatch)
    ex = RecordingExecutor(base_run=(0, "2 passed", ""))
    fail = {"verdict": "fail", "exit_code": 1}
    assert cr.differential_attribution(
        "/testbed", ["test_mod.py"], fail, executor=ex) is True
    # sibling of /testbed is "/": path derived from repo_root, hidden dotdir.
    assert ex.worktree_add_target().startswith("/.gt_base_")


# ---------------------------------------------------------------------------
# Fail-safe truth table on the executor path -> always quiet (False), never crash.
# ---------------------------------------------------------------------------
def test_base_red_via_executor_returns_false(monkeypatch, tmp_path: Path):
    _no_local_subprocess(monkeypatch)
    ex = RecordingExecutor(base_run=(1, "1 failed", ""))  # base ALSO red
    fail = {"verdict": "fail", "exit_code": 1}
    assert cr.differential_attribution(
        "/testbed", ["test_mod.py"], fail, executor=ex) is False
    # worktree still created + cleaned even though attribution is False (no leak).
    assert ex._git_calls("worktree", "add")
    assert ex._git_calls("worktree", "remove")


def test_git_add_nonzero_is_quiet(monkeypatch, tmp_path: Path):
    _no_local_subprocess(monkeypatch)

    class AddFails(RecordingExecutor):
        def __call__(self, cmd, cwd, timeout):
            self.calls.append({"cmd": list(cmd), "cwd": cwd, "timeout": timeout})
            if cmd and cmd[0] == "git" and "add" in cmd:
                return (128, "", "fatal: worktree add failed")  # non-zero
            if cmd and cmd[0] == "git":
                return (0, "abc1234" if "rev-parse" in cmd else "", "")
            return (0, "1 passed", "")

    ex = AddFails()
    fail = {"verdict": "fail", "exit_code": 1}
    # worktree add non-zero -> base unavailable -> quiet, and NO base run happened.
    assert cr.differential_attribution(
        "/testbed", ["test_mod.py"], fail, executor=ex) is False
    base_runs = [c for c in ex.calls if c["cmd"] and c["cmd"][0] not in ("git", "rm")]
    assert not base_runs, f"base run must not fire after a failed worktree add: {base_runs}"


def test_executor_raises_on_base_run_is_quiet(monkeypatch, tmp_path: Path):
    _no_local_subprocess(monkeypatch)

    class RaiseOnRun(RecordingExecutor):
        def __call__(self, cmd, cwd, timeout):
            self.calls.append({"cmd": list(cmd), "cwd": cwd, "timeout": timeout})
            if cmd and cmd[0] in ("git", "rm"):
                return (0, "abc1234" if "rev-parse" in cmd else "", "")
            raise OSError("no runner in this container")  # the base covering run

    ex = RaiseOnRun()
    fail = {"verdict": "fail", "exit_code": 1}
    # executor raises on the base run -> base unavailable -> quiet (never a crash).
    assert cr.differential_attribution(
        "/testbed", ["test_mod.py"], fail, executor=ex) is False
    # cleanup still ran (worktree remove routed through the executor) -> no leak.
    assert ex._git_calls("worktree", "remove")


def test_executor_raises_on_git_is_quiet(monkeypatch, tmp_path: Path):
    _no_local_subprocess(monkeypatch)

    def boom(cmd, cwd, timeout):
        raise OSError("git missing in container")

    fail = {"verdict": "fail", "exit_code": 1}
    # even the HEAD verify / worktree add throwing -> None -> quiet, never a crash.
    assert cr.differential_attribution(
        "/testbed", ["test_mod.py"], fail, executor=boom) is False


def test_container_base_path_is_posix_sibling_derived_from_repo():
    # Pure derivation: container-side, sibling of the repo, no host tempfile,
    # POSIX even if a Windows host injected backslashes into repo_root.
    p = cr._container_base_path("/workspace/proj")
    assert p.startswith("/workspace/.gt_base_") and "\\" not in p
    p2 = cr._container_base_path("C:\\\\host\\\\repo".replace("\\\\", "\\"))
    assert "\\" not in p2  # backslashes normalised to POSIX
    # two derivations never collide (random token) -> concurrent hooks are safe.
    assert cr._container_base_path("/r") != cr._container_base_path("/r")


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
