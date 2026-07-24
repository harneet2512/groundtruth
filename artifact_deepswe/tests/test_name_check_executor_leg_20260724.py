"""AUDIT 2026-07-24 — the PRODUCTION (executor) leg of the undefined-name check.

TWO REAL BUGS, both found by pre-build audit, both invisible to every existing test:

1. DEAD BY CONSTRUCTION. `_apply_name_check` compared `status == "ok"`, but `_execute` returns
   "ran" | "timeout" | "spawn_error" and NEVER "ok" (edit_check.py:560-561, 578, 593). The branch
   could not execute. The in-process leg (executor is None) was unaffected — which is exactly why
   local tests passed while GT_EDIT_CHECK_NAMES came back UNPROVEN from the live run: production
   injects an executor so it only ever took the dead branch.

2. ABSOLUTE PATH IN MODEL BYTES. The subprocess probe echoes the path it is handed (abs_path), so
   the diagnostic read `/testbed/pkg/x.py:2: NameError: ...`, violating the module's own L-1b
   invariant (check_edit_syntax: name the file REPO-RELATIVE so the model needn't guess it back)
   and disagreeing with the in-process leg's format.

These tests drive the REAL executor contract — a 3-tuple (rc, stdout, stderr), the frozen shape
`_execute` validates — so a regression in either the status token or the path rewrite fails here.
"""
from __future__ import annotations
import os
import subprocess
import sys

import pytest

from groundtruth.runtime import edit_check as ec


def _executor(cmd, cwd, timeout):
    """The frozen executor contract: returns exactly (rc, stdout, stderr)."""
    r = subprocess.run([sys.executable] + list(cmd)[1:], capture_output=True, text=True)
    return (r.returncode, r.stdout, r.stderr)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_EDIT_CHECK_NAMES", "1")
    (tmp_path / "pkg").mkdir()
    return tmp_path


def test_executor_leg_detects_the_undefined_name(repo):
    """BUG 1: this is the branch that could never run in production."""
    p = repo / "pkg" / "u.py"
    p.write_text("def f():\n    return nope_undefined\n")
    res = ec.check_edit_syntax(str(p), str(repo), executor=_executor)
    assert res["verdict"] == "name_error", (
        f"production (executor) name-check is DEAD — got {res['verdict']!r}. "
        "Check the _execute status token: it is 'ran', never 'ok'."
    )
    assert res["reason"] == "undefined_name"
    assert "pyflakes" in res["checker"]


def test_diagnostic_is_repo_relative_not_absolute(repo):
    """BUG 2: L-1b — model-facing bytes must never carry the container path."""
    p = repo / "pkg" / "u.py"
    p.write_text("def f():\n    return nope_undefined\n")
    diag = ec.check_edit_syntax(str(p), str(repo), executor=_executor)["diagnostic"]
    assert "pkg/u.py" in diag, f"path not rewritten repo-relative: {diag!r}"
    assert str(repo) not in diag and str(repo).replace("\\", "/") not in diag, \
        f"absolute path leaked into model-facing bytes: {diag!r}"


def test_clean_file_is_not_upgraded_but_records_provenance(repo):
    """Correct-or-quiet + observability: a clean file stays ok and proves the leg RAN."""
    p = repo / "pkg" / "ok.py"
    p.write_text("def f():\n    x = 1\n    return x\n")
    res = ec.check_edit_syntax(str(p), str(repo), executor=_executor)
    assert res["verdict"] == "ok"
    assert "pyflakes:clean" in res["checker"], \
        "a clean file must be distinguishable from 'the name check never executed'"


def test_syntax_error_short_circuits_the_name_check(repo):
    """A parse failure is the syntax path; the name leg must not overwrite it."""
    p = repo / "pkg" / "bad.py"
    p.write_text("def f(:\n")
    res = ec.check_edit_syntax(str(p), str(repo), executor=_executor)
    assert res["verdict"] == "syntax_error"
    assert "pyflakes" not in res["checker"]


def test_flag_off_never_upgrades(repo, monkeypatch):
    monkeypatch.setenv("GT_EDIT_CHECK_NAMES", "0")
    p = repo / "pkg" / "u.py"
    p.write_text("def f():\n    return nope_undefined\n")
    res = ec.check_edit_syntax(str(p), str(repo), executor=_executor)
    assert res["verdict"] == "ok"
    assert "pyflakes" not in str(res["checker"])


def test_broken_executor_degrades_quietly(repo):
    """A buggy executor must never crash the run or fabricate a verdict."""
    p = repo / "pkg" / "u.py"
    p.write_text("def f():\n    return nope_undefined\n")

    def _bad(cmd, cwd, timeout):
        raise RuntimeError("executor exploded")

    assert ec.check_edit_syntax(str(p), str(repo), executor=_bad)["verdict"] in ("ok", "unavailable")
