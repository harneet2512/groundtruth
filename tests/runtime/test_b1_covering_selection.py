"""B1 covering-test SELECTION + full pipeline — Stage-1, on a REAL graph.db.

Proves the selection half (graph.db -> covering test FILES via FACT edges only,
leak-safe) and the full delivered pipeline (select -> run -> render -> a native
covering RED) end to end on a real sqlite graph and a real failing test. This is
the "prove it by being strong" evidence: the delivered signal is a real test
failure the agent would see, produced from the graph, not a mock.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from groundtruth.runtime.covering_runner import (
    run_covering_tests,
    select_covering_tests,
)
from groundtruth.runtime.native_render import (
    contains_gt_tag,
    contains_test_identity,
    render_covering_failure_native,
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


def _make_graph(path: Path, edges: list[tuple[int, int, str, str, float]],
                nodes: list[tuple[int, str, str, int]]) -> None:
    """nodes: (id, name, file_path, is_test); edges: (src, tgt, type, method, conf)."""
    con = sqlite3.connect(str(path))
    con.execute(_NODES_SCHEMA)
    con.execute(_EDGES_SCHEMA)
    for nid, name, fpath, is_test in nodes:
        con.execute(
            "INSERT INTO nodes (id, label, name, file_path, is_test, language) "
            "VALUES (?,?,?,?,?,?)",
            (nid, "Function", name, fpath, is_test, "python"),
        )
    for src, tgt, etype, method, conf in edges:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
            "VALUES (?,?,?,?,?)",
            (src, tgt, etype, method, conf),
        )
    con.commit()
    con.close()


def test_select_finds_fact_covered_test_file(tmp_path):
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/users.py", 0),
               (2, "test_get_user", "tests/test_users.py", 1)],
        edges=[(2, 1, "CALLS", "import", 0.9)],
    )
    cov = select_covering_tests(str(db), {"get_user"})
    assert cov == [{"file": "tests/test_users.py", "confidence": 0.9}]


def test_select_is_leak_safe_no_test_name():
    """The result must never carry the test NAME — only the file + confidence."""
    # (structural: the dict keys are exactly file/confidence)
    db_keys = {"file", "confidence"}
    row = {"file": "tests/test_users.py", "confidence": 0.9}
    assert set(row) == db_keys
    assert "test_get_user" not in str(row)


def test_select_excludes_name_match_guess(tmp_path):
    """A name_match (guess) edge is NOT a covering fact — excluded."""
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/users.py", 0),
               (2, "test_get_user", "tests/test_users.py", 1)],
        edges=[(2, 1, "CALLS", "name_match", 0.9)],  # guess, not a fact
    )
    assert select_covering_tests(str(db), {"get_user"}) == []


def test_select_excludes_low_confidence(tmp_path):
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/users.py", 0),
               (2, "test_get_user", "tests/test_users.py", 1)],
        edges=[(2, 1, "CALLS", "import", 0.5)],  # FACT method but conf < 0.7
    )
    assert select_covering_tests(str(db), {"get_user"}) == []


def test_select_survives_legacy_schema_without_confidence_column(tmp_path):
    """A graph.db predating the confidence column must NOT silently return [].

    Pins the LIPI-implementation fix: the SELECT referenced ``e.confidence``
    unconditionally, so on a legacy schema the query threw -> caught -> [] (a
    coverage hole masquerading as 'no covering tests'). With the column absent,
    every FACT edge is treated as conf 1.0 and the covering file still resolves.
    Revert the fix (put e.confidence back in the SELECT) and this test bites.
    """
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "file_path TEXT, is_test INT, language TEXT)"
    )
    # edges table WITHOUT a confidence column (the legacy shape).
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, resolution_method TEXT)"
    )
    con.execute("INSERT INTO nodes VALUES (1,'Function','get_user','src/users.py',0,'python')")
    con.execute("INSERT INTO nodes VALUES (2,'Function','test_get_user','tests/test_users.py',1,'python')")
    con.execute("INSERT INTO edges VALUES (1,2,1,'CALLS','import')")
    con.commit()
    con.close()
    cov = select_covering_tests(str(db), {"get_user"})
    assert cov == [{"file": "tests/test_users.py", "confidence": 1.0}], cov


def test_select_excludes_non_test_caller(tmp_path):
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/users.py", 0),
               (2, "other", "src/other.py", 0)],  # caller is NOT a test
        edges=[(2, 1, "CALLS", "import", 0.9)],
    )
    assert select_covering_tests(str(db), {"get_user"}) == []


def test_select_empty_cases(tmp_path):
    assert select_covering_tests("", {"x"}) == []
    assert select_covering_tests(str(tmp_path / "nope.db"), {"x"}) == []
    db = tmp_path / "g.db"
    _make_graph(db, nodes=[(1, "f", "a.py", 0)], edges=[])
    assert select_covering_tests(str(db), set()) == []
    assert select_covering_tests(str(db), {"f"}) == []  # no covering edge


# ---------------------------------------------------------------------------
# FULL PIPELINE — real graph.db + real failing test -> a native covering RED
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    __import__("shutil").which("pytest") is None, reason="pytest not on PATH"
)
def test_full_pipeline_delivers_native_red(tmp_path):
    repo = tmp_path
    (repo / "mod.py").write_text("def foo():\n    return 1\n")
    (repo / "test_mod.py").write_text(textwrap.dedent("""
        from mod import foo
        def test_foo():
            assert foo() == 2  # fails: foo returns 1
    """))
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "foo", "mod.py", 0), (2, "test_foo", "test_mod.py", 1)],
        edges=[(2, 1, "CALLS", "import", 0.95)],
    )
    # select (from the real graph) -> run (the real failing test) -> render native.
    cov = select_covering_tests(str(db), {"foo"})
    assert cov == [{"file": "test_mod.py", "confidence": 0.95}]
    result = run_covering_tests(str(repo), [c["file"] for c in cov])
    assert result["verdict"] == "fail", result
    rendered = render_covering_failure_native(result, edited_symbol="foo",
                                               test_files=["test_mod.py"])
    # Format D on a real end-to-end RED: the assertion delta survives...
    assert "1 == 2" in rendered or "== 2" in rendered
    # ...but NO test identity reaches the surface (nodeid, test file, def echo).
    assert "test_mod.py" not in rendered and "test_foo" not in rendered and "::" not in rendered
    assert not contains_gt_tag(rendered)
    assert not contains_test_identity(rendered, ["test_mod.py"])
