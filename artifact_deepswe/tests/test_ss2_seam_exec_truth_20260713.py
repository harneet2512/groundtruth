"""SS-2 feature 1/2 (GT_SS_EXEC_TRUTH) at the mini seam — NO UNEXECUTED ASSURANCE.

Causal-audit context (run 29236533134): the verify.horizon advisory + the spec.obligation
covering line claimed "a graph-linked covering test covers them" on fonttools-3682 /
cfn-lint-3749 with NO runnable covering test in the working tree — the graph held a test NODE
whose FILE was absent from the agent's checkout. The seam's ``_covering_tests_for_symbols`` is
the SINGLE surface every covering consumer (advisory / executed / obligation) reads; under
GT_SS_EXEC_TRUTH it now drops a phantom (not-on-disk) test file, so the "graph-linked covering
test" CLAIM is made only when the runner could actually run it. Off -> byte-identical.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.verification_horizon import render_verify_emission  # noqa: E402

_NODES = (
    "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,"
    " file_path TEXT, start_line INT, end_line INT, signature TEXT, return_type TEXT,"
    " is_exported INT, is_test INT, language TEXT, parent_id INT)")
_EDGES = (
    "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, type TEXT,"
    " source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL, metadata TEXT)")


def _graph(path, nodes, edges):
    con = sqlite3.connect(str(path))
    con.execute(_NODES)
    con.execute(_EDGES)
    for nid, name, fp, is_test in nodes:
        con.execute("INSERT INTO nodes (id,label,name,file_path,is_test,language)"
                    " VALUES (?,?,?,?,?,?)", (nid, "Function", name, fp, is_test, "python"))
    for s, t, m, c in edges:
        con.execute("INSERT INTO edges (source_id,target_id,type,resolution_method,confidence)"
                    " VALUES (?,?,'CALLS',?,?)", (s, t, m, c))
    con.commit()
    con.close()


def _setup(monkeypatch, tmp_path, *, write_real: bool, write_phantom: bool):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.delenv("GT_SS_EXEC_TRUTH", raising=False)
    db = tmp_path / "graph.db"
    _graph(db,
           nodes=[(1, "foo", "src/impl.py", 0),
                  (2, "test_foo_phantom", "tests/test_phantom.py", 1),
                  (3, "test_foo_real", "tests/test_foo.py", 1)],
           edges=[(2, 1, "import", 1.0), (3, 1, "import", 0.8)])  # phantom ranks first
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    if write_real:
        (root / "tests" / "test_foo.py").write_text("x\n", encoding="utf-8")
    if write_phantom:
        (root / "tests" / "test_phantom.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(g, "_db_path", lambda: str(db))
    monkeypatch.setattr(g, "_root", lambda: str(root))
    g._reset_oracle_state()


def test_flag_off_is_byte_identical(monkeypatch, tmp_path):
    # only the REAL file on disk, but flag OFF -> NO disk filter -> BOTH graph nodes returned.
    _setup(monkeypatch, tmp_path, write_real=True, write_phantom=False)
    got = g._covering_tests_for_symbols({"foo"})
    assert sorted(r["file"] for r in got) == ["tests/test_foo.py", "tests/test_phantom.py"]


def test_flag_on_drops_phantom(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, write_real=True, write_phantom=False)
    monkeypatch.setenv("GT_SS_EXEC_TRUTH", "1")
    got = g._covering_tests_for_symbols({"foo"})
    assert [r["file"] for r in got] == ["tests/test_foo.py"]  # phantom gone; real survives


def test_flag_on_all_phantom_is_empty_kills_false_claim(monkeypatch, tmp_path):
    # NEITHER file on disk -> [] -> the "graph-linked covering test" claim is NOT made.
    _setup(monkeypatch, tmp_path, write_real=False, write_phantom=False)
    monkeypatch.setenv("GT_SS_EXEC_TRUTH", "1")
    covering = g._covering_tests_for_symbols({"foo"})
    assert covering == []
    # end-to-end: the advisory falls back to the GENERIC (non-graph-linked) reminder.
    body = render_verify_emission("advisory", 10, 100, {"src/impl.py"}, covering_tests=covering)
    assert "a graph-linked covering test" not in body          # false assurance KILLED
    assert "the relevant tests cover them" in body             # honest generic reminder


def test_flag_off_all_phantom_still_makes_the_false_claim(monkeypatch, tmp_path):
    """Proof the fix is load-bearing: with the flag OFF, the phantom still yields a
    covering result -> render_verify_emission STILL makes the graph-linked claim. This is
    the pre-SS-2 (regression) behaviour, preserved byte-identically off."""
    _setup(monkeypatch, tmp_path, write_real=False, write_phantom=False)
    covering = g._covering_tests_for_symbols({"foo"})  # flag off
    assert covering != []                                        # phantom NOT filtered
    body = render_verify_emission("advisory", 10, 100, {"src/impl.py"}, covering_tests=covering)
    assert "a graph-linked covering test" in body               # the pre-fix false claim
