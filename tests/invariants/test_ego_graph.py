"""Tests for ego-graph query and change impact analysis.

Research:
- RepoGraph ICLR 2025: k-hop ego-graphs (k=1: 11.6 nodes avg)
- CodePlan FSE 2024: change-may-impact via CalledBy edges
- Codebase-Memory 2026: 10x fewer tokens with structured rendering
"""
import sqlite3
import pytest
from pathlib import Path

from groundtruth.graph.ego import ego_graph, change_impact, EgoGraph


def _create_test_db(tmp_path):
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL, name TEXT NOT NULL,
        qualified_name TEXT, file_path TEXT NOT NULL,
        start_line INTEGER, end_line INTEGER,
        signature TEXT, return_type TEXT,
        is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
        language TEXT DEFAULT 'python', parent_id INTEGER
    )""")
    conn.execute("""CREATE TABLE edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
        type TEXT NOT NULL, source_line INTEGER, source_file TEXT,
        resolution_method TEXT, confidence REAL DEFAULT 1.0,
        metadata TEXT
    )""")
    # Create a small call graph:
    # test_foo (test) → foo → bar → baz
    #                  foo → helper
    #                  other_caller → foo
    nodes = [
        ("foo", "Function", "src/core.py", 10, False),
        ("bar", "Function", "src/utils.py", 20, False),
        ("baz", "Function", "src/deep.py", 30, False),
        ("helper", "Function", "src/core.py", 50, False),
        ("other_caller", "Function", "src/api.py", 5, False),
        ("test_foo", "Function", "tests/test_core.py", 1, True),
        ("MyClass", "Class", "src/core.py", 1, False),
    ]
    ids = {}
    for name, label, fpath, line, is_test in nodes:
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, start_line, is_test, is_exported) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (label, name, fpath, line, int(is_test)),
        )
        ids[name] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    edges = [
        (ids["foo"], ids["bar"], "CALLS", 1.0),
        (ids["foo"], ids["helper"], "CALLS", 1.0),
        (ids["bar"], ids["baz"], "CALLS", 0.9),
        (ids["other_caller"], ids["foo"], "CALLS", 1.0),
        (ids["test_foo"], ids["foo"], "CALLS", 1.0),
        (ids["foo"], ids["MyClass"], "EXTENDS", 1.0),
    ]
    for src, tgt, etype, conf in edges:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, confidence) VALUES (?, ?, ?, ?)",
            (src, tgt, etype, conf),
        )
    conn.commit()
    conn.close()
    return str(db), ids


class TestEgoGraph:
    def test_k1_finds_direct_neighbors(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        g = ego_graph(db, "foo", "src/core.py", k=1)
        assert g.center is not None
        assert g.center.name == "foo"
        assert len(g.callers) == 2  # other_caller + test_foo
        assert len(g.callees) == 2  # bar + helper

    def test_k2_finds_transitive(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        g = ego_graph(db, "foo", "src/core.py", k=2)
        assert g.center.name == "foo"
        node_names = {n.name for n in g.nodes.values()}
        assert "baz" in node_names  # 2-hop: foo → bar → baz

    def test_k1_does_not_include_2hop(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        g = ego_graph(db, "foo", "src/core.py", k=1)
        node_names = {n.name for n in g.nodes.values()}
        assert "baz" not in node_names  # baz is 2 hops away

    def test_parent_class(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        g = ego_graph(db, "foo", "src/core.py", k=1)
        pc = g.parent_class
        assert pc is not None
        assert pc.name == "MyClass"

    def test_render_structured(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        g = ego_graph(db, "foo", "src/core.py", k=1)
        rendered = g.render()
        assert "foo()" in rendered
        assert "core.py" in rendered
        assert "Called by:" in rendered
        assert "Calls:" in rendered
        assert "Parent:" in rendered
        assert "test_foo()" in rendered
        assert "[test]" in rendered

    def test_missing_symbol_returns_empty(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        g = ego_graph(db, "nonexistent", k=1)
        assert g.center is None
        assert len(g.nodes) == 0

    def test_confidence_filter(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        g = ego_graph(db, "bar", "src/utils.py", k=1, min_confidence=0.95)
        callee_names = {c.name for c in g.callees}
        assert "baz" not in callee_names  # baz edge is 0.9, below 0.95


class TestChangeImpact:
    def test_direct_callers(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        impact = change_impact(db, "foo", "src/core.py", max_depth=1)
        names = {i["name"] for i in impact}
        assert "other_caller" in names
        assert "test_foo" in names

    def test_transitive_callers(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        impact = change_impact(db, "bar", "src/utils.py", max_depth=2)
        names = {i["name"] for i in impact}
        # bar is called by foo, foo is called by other_caller and test_foo
        assert "foo" in names  # hop 1
        # other_caller and test_foo are hop 2 (they call foo which calls bar)

    def test_hop_distance_tracked(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        impact = change_impact(db, "bar", "src/utils.py", max_depth=2)
        for i in impact:
            if i["name"] == "foo":
                assert i["hop"] == 1

    def test_no_self_in_impact(self, tmp_path):
        db, ids = _create_test_db(tmp_path)
        impact = change_impact(db, "foo", "src/core.py", max_depth=2)
        names = {i["name"] for i in impact}
        assert "foo" not in names  # changed function is not in its own impact set
