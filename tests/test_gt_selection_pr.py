"""Deterministic tests for scripts/gt_selection_pr.py — the covering-selection P/R harness.

Covers the pure P/R math, the filename-anchored test-file detector, the word-boundary
grep-reference, and determinism. Uses a tiny synthetic repo (no graph.db needed for the
reference-side tests; the engine import is exercised separately by the live testset run).
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PR = os.path.join(os.path.dirname(_HERE), "scripts", "gt_selection_pr.py")


def _load():
    spec = importlib.util.spec_from_file_location("gt_selection_pr", _PR)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pr():
    return _load()


def test_pr_math(pr):
    # pred {a,b}, ref {b,c} -> inter=1 ; precision=1/2 ; recall=1/2
    r = pr._pr_for_symbol({"a", "b"}, {"b", "c"})
    assert r["n_intersection"] == 1
    assert r["precision"] == 0.5
    assert r["recall"] == 0.5
    # empty prediction -> precision None (undefined), recall 0 over nonempty ref
    r2 = pr._pr_for_symbol(set(), {"b"})
    assert r2["precision"] is None and r2["recall"] == 0.0
    # perfect selection
    r3 = pr._pr_for_symbol({"x"}, {"x"})
    assert r3["precision"] == 1.0 and r3["recall"] == 1.0


def test_testfile_detector_and_grep_reference(pr, tmp_path):
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "tests").mkdir()
    # product file (NOT a test file even though it says conftest-ish)
    (repo / "pkg" / "mod.py").write_text("def target_func():\n    return 1\n")
    (repo / "conftest.py").write_text("import pytest\n")  # infra, not a test-case file
    # a real test file referencing target_func as a whole word
    (repo / "tests" / "test_mod.py").write_text(
        "from pkg.mod import target_func\n\ndef test_it():\n    assert target_func()\n")
    # a test file that mentions a CONFUSABLE longer token but not target_func
    (repo / "tests" / "test_other.py").write_text(
        "def test_x():\n    target_func_helper_xyz = 1\n    assert target_func_helper_xyz\n")

    tfs = pr._discover_test_files(str(repo))
    # tests/ dir files are test files; conftest.py at root is NOT; product mod.py is NOT
    assert "tests/test_mod.py" in tfs
    assert "tests/test_other.py" in tfs
    assert "pkg/mod.py" not in tfs
    assert "conftest.py" not in tfs

    ref = pr._grep_reference(str(repo), "target_func", tfs)
    # whole-word match: test_mod.py hits; test_other.py's target_func_helper_xyz does NOT
    assert ref == ["tests/test_mod.py"], ref


def test_grep_reference_min_length(pr, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("id = 1\n")
    tfs = pr._discover_test_files(str(tmp_path))
    # a 2-char symbol is unmatchable (FP guard) -> empty reference
    assert pr._grep_reference(str(tmp_path), "id", tfs) == []


def test_determinism(pr, tmp_path):
    import json
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test(): foo()\n")
    a = json.dumps(pr._discover_test_files(str(tmp_path)))
    b = json.dumps(pr._discover_test_files(str(tmp_path)))
    assert a == b
