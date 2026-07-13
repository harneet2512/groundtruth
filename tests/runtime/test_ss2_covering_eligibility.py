"""SS-2 feature 1/2 — covering SELECTION is RUNNER-ELIGIBLE (no phantom claim).

Stage-1 determinism on a REAL sqlite graph.db + a REAL working tree. The 29-task causal
audit (run 29236533134) found the "a graph-linked covering test covers them" advisory shipped
on fonttools-3682/cfn-lint-3749 with NO runnable covering test in the working tree: the graph
carried a test NODE whose FILE was absent from the agent's checkout (a stale/renamed/other-tree
index artifact). ``select_covering_tests(..., repo_root=…)`` now drops that phantom at the
selection surface, so the advisory + executed + obligation paths share ONE runner-eligibility
definition (``os.path.exists(join(root, file))`` — the exact gate the executed seam applies).

Invariants proven:
  * repo_root=None  -> BYTE-IDENTICAL to the pre-SS-2 selection (every legacy caller unchanged).
  * repo_root given -> a covering FILE absent from the tree is DROPPED; a present one survives.
  * a phantom ranked ABOVE a real covering file does NOT starve the real one (scan-then-cap).
  * the FACT gate is untouched (name_match / conf<0.7 still never selected — no fabrication).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.runtime.covering_runner import (
    runner_eligible_files,
    select_covering_tests,
)

_NODES_SCHEMA = (
    "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
    "qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, "
    "signature TEXT, return_type TEXT, is_exported INT, is_test INT, "
    "language TEXT, parent_id INT)"
)
_EDGES_SCHEMA = (
    "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
    "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, "
    "confidence REAL, metadata TEXT)"
)


def _make_graph(path, nodes, edges) -> None:
    con = sqlite3.connect(str(path))
    con.execute(_NODES_SCHEMA)
    con.execute(_EDGES_SCHEMA)
    for nid, name, fpath, is_test in nodes:
        con.execute(
            "INSERT INTO nodes (id, label, name, file_path, is_test, language) "
            "VALUES (?,?,?,?,?,?)", (nid, "Function", name, fpath, is_test, "python"))
    for src, tgt, method, conf in edges:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
            "VALUES (?,?,'CALLS',?,?)", (src, tgt, method, conf))
    con.commit()
    con.close()


def _graph_two_tests(tmp_path):
    """impl `foo` covered by TWO FACT-tier test files: a REAL one and a PHANTOM one
    (higher confidence, so it ranks first — proving the scan-then-cap fill)."""
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "foo", "src/impl.py", 0),
               (2, "test_foo_phantom", "tests/test_phantom.py", 1),
               (3, "test_foo_real", "tests/test_foo.py", 1)],
        # phantom edge conf 1.0 (ranks FIRST); real edge conf 0.8 (ranks second).
        edges=[(2, 1, "import", 1.0), (3, 1, "import", 0.8)],
    )
    return db


def test_repo_root_none_is_byte_identical(tmp_path):
    db = _graph_two_tests(tmp_path)
    got = select_covering_tests(db, {"foo"}, limit=2)
    files = sorted(r["file"] for r in got)
    # No disk check: BOTH graph test files are returned (the legacy behaviour).
    assert files == ["tests/test_foo.py", "tests/test_phantom.py"]


def test_phantom_dropped_when_repo_root_given(tmp_path):
    db = _graph_two_tests(tmp_path)
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n", encoding="utf-8")
    # tests/test_phantom.py is NOT written -> it is a phantom -> must be dropped.
    got = select_covering_tests(db, {"foo"}, limit=2, repo_root=str(root))
    files = [r["file"] for r in got]
    assert files == ["tests/test_foo.py"], files  # phantom gone; the REAL one survives despite ranking 2nd


def test_all_phantom_yields_empty(tmp_path):
    db = _graph_two_tests(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    # neither test file exists on disk -> no runner-eligible covering -> [] (correct-or-quiet).
    got = select_covering_tests(db, {"foo"}, limit=2, repo_root=str(root))
    assert got == []


def test_present_file_survives(tmp_path):
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "foo", "src/impl.py", 0), (2, "test_foo", "tests/test_foo.py", 1)],
        edges=[(2, 1, "import", 1.0)],
    )
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_foo.py").write_text("x\n", encoding="utf-8")
    got = select_covering_tests(db, {"foo"}, limit=2, repo_root=str(root))
    assert [r["file"] for r in got] == ["tests/test_foo.py"]


def test_fact_gate_untouched_no_fabrication(tmp_path):
    """A name_match / low-conf test edge is NEVER selected — the eligibility filter must
    not loosen the FACT gate (admitting a receiver-unproven guess would fabricate a claim)."""
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "foo", "src/impl.py", 0), (2, "test_foo", "tests/test_foo.py", 1)],
        edges=[(2, 1, "name_match", 1.0), (2, 1, "impl_method", 1.0)],  # both non-FACT
    )
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_foo.py").write_text("x\n", encoding="utf-8")
    # even with the file on disk, a name_match/impl_method edge is a guess -> not selected.
    assert select_covering_tests(db, {"foo"}, limit=2, repo_root=str(root)) == []
    assert select_covering_tests(db, {"foo"}, limit=2) == []


def test_runner_eligible_files_helper(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "present.py").write_text("x\n", encoding="utf-8")
    files = ["tests/present.py", "tests/absent.py"]
    # None root -> unchanged (byte-identical).
    assert runner_eligible_files(files, None) == files
    assert runner_eligible_files(files, "") == files
    # real root -> only the present file.
    assert runner_eligible_files(files, str(root)) == ["tests/present.py"]
    assert runner_eligible_files([], str(root)) == []
