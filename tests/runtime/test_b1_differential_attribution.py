"""B1 green->base->red DIFFERENTIAL attribution — Stage-1 deterministic tests.

Closes B1's assertion-muteness: frame-attribution only fires on a CRASH (the
deepest agent-source traceback frame lands in an edited file). An ASSERTION
failure (wrong value, no crash) leaves NO agent frame, so ``is_edit_attributed``
returns False and B1 stays silent on the MAJORITY of real covering failures.

The differential path runs the SAME covering files against the pre-edit BASE tree
(``git worktree add HEAD``): current-RED + base-GREEN => the working-tree edit is
the delta => attributed (deliver). current-RED + base-RED => pre-existing failure
=> NOT attributed (quiet — this also kills the false-block vector). No git / base
unavailable => quiet (correct-or-quiet is the law: never a false red).

Two RED-first behavior tests (assertion-delivery, pre-existing-quiet) drive the
build end-to-end on a real git repo + real pytest. Executor-injected unit tests
pin the truth table without git/subprocess.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from groundtruth.runtime import covering_runner as cr
from groundtruth.runtime import native_render as nr

_HAVE_GIT = shutil.which("git") is not None
_HAVE_PYTEST = shutil.which("pytest") is not None
_needs_git_pytest = pytest.mark.skipif(
    not (_HAVE_GIT and _HAVE_PYTEST), reason="git and pytest both required"
)


def _git_repo(root: Path, files_at_head: dict[str, str]) -> None:
    """Init a git repo at root and commit files_at_head as HEAD. Raises on failure
    (a broken fixture must be loud, unlike the engine which is correct-or-quiet)."""
    def g(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True, capture_output=True, text=True,
        )
    g("init", "-q")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    g("config", "commit.gpgsign", "false")
    for rel, content in files_at_head.items():
        (root / rel).write_text(content)
    g("add", "-A")
    g("commit", "-q", "-m", "base")


# ---------------------------------------------------------------------------
# RED-first #1: assertion failure, base GREEN -> DELIVER (frames alone are mute)
# ---------------------------------------------------------------------------
@_needs_git_pytest
def test_differential_delivers_on_assertion_when_base_green(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_repo(repo, {
        "mod.py": "def foo():\n    return 2\n",  # HEAD: correct -> base GREEN
        "test_mod.py": textwrap.dedent("""
            from mod import foo
            def test_foo():
                assert foo() == 2
        """),
    })
    # working-tree edit turns foo() wrong -> a VALUE failure (no crash, no frame)
    (repo / "mod.py").write_text("def foo():\n    return 1\n")

    cres = cr.run_covering_tests(str(repo), ["test_mod.py"])
    assert cres["verdict"] == "fail", cres

    # muteness documented: frame attribution CANNOT see an assertion failure.
    assert nr.is_edit_attributed(cres, {"mod.py"}, test_files=["test_mod.py"]) is False

    # differential rescues it: current-RED + base-GREEN => attributed.
    assert cr.is_red_attributable(
        cres, {"mod.py"}, test_files=["test_mod.py"],
        repo_root=str(repo), covering_files=["test_mod.py"],
    ) is True

    block = nr.render_covering_failure_native(
        cres, edited_symbol="foo", test_files=["test_mod.py"])
    assert block, "attributed assertion RED must deliver a Format-D block"
    assert "1 == 2" in block                          # value delta kept
    # leak invariants hold on the delivered surface.
    assert not nr.contains_gt_tag(block)
    assert not nr.contains_test_identity(block, ["test_mod.py"])
    assert "test_foo" not in block and "test_mod" not in block and "::" not in block


# ---------------------------------------------------------------------------
# RED-first #2 (contract UPDATED by W2a, e2abece00): covering test red on BOTH
# base and current, failing the SAME named test -> the UNRESOLVED-TARGET shape
# (a locally-green-but-incomplete fix) -> ATTRIBUTED via method
# "unresolved_covering". The pre-W2a expectation (quiet on any base-red) let
# incomplete fixes submit unchallenged (ipython-14798 / beets-5457 class); this
# test was landed stale and caught by the full-suite run on 2026-07-17.
# Quiet is still the verdict when the base-red is a DIFFERENT named failure
# (no overlap -> not the covered target) — pinned below.
# ---------------------------------------------------------------------------
@_needs_git_pytest
def test_preexisting_same_name_failure_is_unresolved_covering(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_repo(repo, {
        "mod.py": "def foo():\n    return 1\n",  # HEAD already wrong -> base RED
        "test_mod.py": textwrap.dedent("""
            from mod import foo
            def test_foo():
                assert foo() == 2
        """),
    })
    # working-tree edit is still wrong -> current RED too, SAME failing test name
    (repo / "mod.py").write_text("def foo():\n    return 3\n")

    cres = cr.run_covering_tests(str(repo), ["test_mod.py"])
    assert cres["verdict"] == "fail", cres

    att = cr.attribute_covering_red(
        cres, {"mod.py"}, test_files=["test_mod.py"],
        repo_root=str(repo), covering_files=["test_mod.py"],
    )
    assert att.attributed is True
    assert att.method == "unresolved_covering"
    assert att.base_verdict == "fail"


@_needs_git_pytest
def test_preexisting_different_name_failure_stays_quiet(tmp_path: Path):
    # Base-red on a DIFFERENT named test than the current red -> no overlap ->
    # NOT the covered target -> unattributed (fail toward quiet, W2a invariant ②).
    repo = tmp_path / "r"
    repo.mkdir()
    _git_repo(repo, {
        "mod.py": "def foo():\n    return 1\ndef bar():\n    return 1\n",
        "test_mod.py": textwrap.dedent("""
            from mod import foo, bar
            def test_bar():
                assert bar() == 2
        """),
    })
    # The edit rewrites the test file's target set: current red is test_foo only.
    (repo / "test_mod.py").write_text(textwrap.dedent("""
        from mod import foo, bar
        def test_foo():
            assert foo() == 2
    """))
    cres = cr.run_covering_tests(str(repo), ["test_mod.py"])
    assert cres["verdict"] == "fail", cres

    att = cr.attribute_covering_red(
        cres, {"mod.py"}, test_files=["test_mod.py"],
        repo_root=str(repo), covering_files=["test_mod.py"],
    )
    assert att.attributed is False


# ---------------------------------------------------------------------------
# Executor-injected truth table (no git / no subprocess): base pass/fail/unavail.
# ---------------------------------------------------------------------------
def _fake_base(monkeypatch, tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    monkeypatch.setattr(
        cr, "_make_base_worktree",
        lambda root, executor=None: (str(tmp_path / "base"), str(parent)))
    monkeypatch.setattr(
        cr, "_cleanup_base_worktree",
        lambda root, wt, parent, executor=None: None)


def test_differential_truth_table_via_executor(tmp_path: Path, monkeypatch):
    _fake_base(monkeypatch, tmp_path)
    fail = {"verdict": "fail", "stdout_tail": "1 failed", "exit_code": 1}
    files = ["test_mod.py"]

    base_green = lambda cmd, cwd, timeout: (0, "1 passed", "")          # noqa: E731
    assert cr.differential_attribution(str(tmp_path), files, fail, executor=base_green) is True

    base_red = lambda cmd, cwd, timeout: (1, "1 failed", "")            # noqa: E731
    assert cr.differential_attribution(str(tmp_path), files, fail, executor=base_red) is False

    base_unavail = lambda cmd, cwd, timeout: (5, "no tests ran", "")    # noqa: E731 (exit5/0-pass)
    assert cr.differential_attribution(str(tmp_path), files, fail, executor=base_unavail) is False


def test_differential_requires_established_current_red(tmp_path: Path, monkeypatch):
    # Fail-closed gate: attribution is a CONJUNCTION (current-RED AND base-GREEN).
    # A stray call with NO established current verdict (None / {} / missing or None
    # verdict key) must return False even when the base WOULD be green — base-green
    # alone is zero evidence the edit broke anything (correct-or-quiet).
    _fake_base(monkeypatch, tmp_path)
    base_green = lambda cmd, cwd, timeout: (0, "1 passed", "")          # noqa: E731
    files = ["test_mod.py"]
    assert cr.differential_attribution(str(tmp_path), files, None, executor=base_green) is False
    assert cr.differential_attribution(str(tmp_path), files, {}, executor=base_green) is False
    assert cr.differential_attribution(
        str(tmp_path), files, {"verdict": None}, executor=base_green) is False
    assert cr.differential_attribution(
        str(tmp_path), files, {"exit_code": 1}, executor=base_green) is False


def test_differential_executor_spawn_error_is_quiet(tmp_path: Path, monkeypatch):
    _fake_base(monkeypatch, tmp_path)
    fail = {"verdict": "fail"}

    def boom_exec(cmd, cwd, timeout):
        raise OSError("no runner")
    # an executor that throws -> base unavailable -> quiet (never a crash, never True).
    assert cr.differential_attribution(str(tmp_path), ["test_mod.py"], fail, executor=boom_exec) is False


# ---------------------------------------------------------------------------
# No git -> quiet. No repo_root -> frames only.
# ---------------------------------------------------------------------------
def test_no_git_is_quiet(tmp_path: Path):
    fail = {"verdict": "fail"}
    d = tmp_path / "nogit"
    d.mkdir()
    assert cr.differential_attribution(str(d), ["test_mod.py"], fail) is False
    assert cr.is_red_attributable(
        fail, {"mod.py"}, test_files=["test_mod.py"],
        repo_root=str(d), covering_files=["test_mod.py"]) is False


def test_is_red_attributable_frames_only_when_no_repo():
    # assertion red, no agent frame, no repo_root/covering_files -> frames-only -> quiet.
    r = {"stdout_tail": "E   assert 1 == 2", "stderr_tail": ""}
    assert cr.is_red_attributable(r, {"mod.py"}, test_files=["test_mod.py"]) is False


# ---------------------------------------------------------------------------
# Frames FIRST: when a crash frame attributes, differential is never run (cost).
# ---------------------------------------------------------------------------
def test_frames_first_short_circuits_no_base_run(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("differential must not run when frames already attribute")
    monkeypatch.setattr(cr, "differential_attribution", boom)
    crash = {
        "stdout_tail": (
            "tests/test_pay.py:12: in test_refund\n"
            "src/mymodule.py:88: in transform\n"
            "E   TypeError: unsupported operand type(s)"
        ),
        "stderr_tail": "",
    }
    assert cr.is_red_attributable(
        crash, ["src/mymodule.py"], test_files=["tests/test_pay.py"],
        repo_root="/anything", covering_files=["tests/test_pay.py"]) is True


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
