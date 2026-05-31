"""Tests for Layer 2.3 — L3b post-view Contract pillar + categorical filter.

Verifies:
- _contract_pillar() always returns signature/return evidence from nodes
  table regardless of caller count (CLAUDE.md:86 always-fire).
- Issue-relevant functions are prioritized.
- _edge_filter() reuses the L3 categorical helper.
- graph_navigation() delivers Contract even when function has 0 callers.
"""
import os
import sqlite3
import tempfile

import pytest

from groundtruth.hooks.post_view import (
    _contract_pillar,
    _edge_filter,
    graph_navigation,
)


def _make_db(*, with_callers: bool = False, categorical: bool = True) -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            start_line INTEGER DEFAULT 0,
            signature TEXT,
            return_type TEXT,
            is_test INTEGER DEFAULT 0,
            language TEXT DEFAULT 'python'
        )
    """)
    edge_cols = (
        "id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, target_id INTEGER, "
        "type TEXT, source_line INTEGER DEFAULT 0, confidence REAL DEFAULT 0.0"
    )
    if categorical:
        edge_cols += ", resolution_method TEXT, trust_tier TEXT, candidate_count INTEGER DEFAULT 1"
    conn.execute(f"CREATE TABLE edges ({edge_cols})")

    # Isolated function (0 callers) — the case where Contract matters most.
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, signature, return_type) "
        "VALUES ('Function', 'expanded_capacity', 'src/stats.py', 10, "
        "'def expanded_capacity(comps, groupby=False)', 'DataFrame')"
    )
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, signature, return_type) "
        "VALUES ('Function', '_filter_active_assets', 'src/stats.py', 50, "
        "'def _filter_active_assets(idx, mask)', 'Index')"
    )
    if with_callers:
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, signature) "
            "VALUES ('Function', 'caller', 'src/other.py', 'def caller()')"
        )
        if categorical:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, type, source_line, confidence, "
                "resolution_method, trust_tier, candidate_count) "
                "VALUES (3, 1, 'CALLS', 5, 1.0, 'same_file', 'CERTIFIED', 1)"
            )
        else:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, type, source_line, confidence) "
                "VALUES (3, 1, 'CALLS', 5, 1.0)"
            )
    conn.commit()
    conn.close()
    return path


def test_contract_pillar_returns_signature():
    path = _make_db()
    try:
        conn = sqlite3.connect(path)
        lines = _contract_pillar(conn, "src/stats.py")
        conn.close()
        assert any("expanded_capacity" in l for l in lines)
        assert all(l.startswith("[CONTRACT]") for l in lines)
    finally:
        os.unlink(path)


def test_contract_pillar_includes_return_type():
    path = _make_db()
    try:
        conn = sqlite3.connect(path)
        lines = _contract_pillar(conn, "src/stats.py")
        conn.close()
        # expanded_capacity sig has no '->', so return type appended
        cap_line = next(l for l in lines if "expanded_capacity" in l)
        assert "-> DataFrame" in cap_line
    finally:
        os.unlink(path)


def test_contract_pillar_prioritizes_issue_relevant():
    path = _make_db()
    try:
        conn = sqlite3.connect(path)
        # issue mentions "filter active" → _filter_active_assets should rank first
        lines = _contract_pillar(conn, "src/stats.py", issue_terms={"filter", "active", "assets"})
        conn.close()
        assert lines
        assert "_filter_active_assets" in lines[0]
    finally:
        os.unlink(path)


def test_contract_pillar_empty_on_no_functions():
    path = _make_db()
    try:
        conn = sqlite3.connect(path)
        lines = _contract_pillar(conn, "nonexistent/file.py")
        conn.close()
        assert lines == []
    finally:
        os.unlink(path)


def test_edge_filter_returns_categorical_on_post_merge():
    path = _make_db(categorical=True)
    try:
        clause = _edge_filter(path)
        assert "resolution_method" in clause or "confidence" in clause
    finally:
        os.unlink(path)


def test_edge_filter_falls_back_on_legacy():
    path = _make_db(categorical=False)
    try:
        clause = _edge_filter(path)
        assert "confidence" in clause
    finally:
        os.unlink(path)


def test_graph_navigation_delivers_contract_on_isolated_function():
    """The constitutional fix: Contract pillar fires even with 0 callers."""
    path = _make_db(with_callers=False)
    try:
        out, total_callers = graph_navigation("src/stats.py", path)
        assert total_callers == 0  # isolated
        # Contract pillar must still appear despite zero callers
        assert any("[CONTRACT]" in line for line in out)
        assert any("expanded_capacity" in line for line in out)
    finally:
        os.unlink(path)


def test_graph_navigation_contract_leads_output():
    """Contract should be prepended (U-shaped salience)."""
    path = _make_db(with_callers=True)
    try:
        out, _ = graph_navigation("src/stats.py", path)
        assert out
        # First line should be a Contract line
        assert out[0].startswith("[CONTRACT]")
    finally:
        os.unlink(path)


def test_graph_navigation_corrupt_db_returns_gracefully():
    """Verifier-found: a corrupt db must return [], 0 not crash."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    with open(path, "w") as f:
        f.write("this is not a sqlite database")
    try:
        # Must not raise
        out, total = graph_navigation("src/stats.py", path)
        assert out == []
        assert total == 0
    finally:
        os.unlink(path)


def test_contract_pillar_keeps_real_symbol_run_as_anchor(monkeypatch):
    """Regression (Step 2/site 1): a real issue symbol named 'run' (an old static
    {setup,teardown,main,run,wrapper} blocklist entry) must drive the contract, not
    be dropped. Only DUNDERS are generic anchors now; hub/homonym filtering happens
    upstream in extract_issue_anchors (is_seed_pollutant)."""
    import groundtruth.hooks.post_view as pv

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, "
        "name TEXT, file_path TEXT, start_line INTEGER DEFAULT 0, signature TEXT, "
        "return_type TEXT, is_test INTEGER DEFAULT 0, language TEXT DEFAULT 'python')"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, "
        "target_id INTEGER, type TEXT, source_line INTEGER DEFAULT 0, confidence REAL DEFAULT 0.0)"
    )
    conn.execute(
        "INSERT INTO nodes (label,name,file_path,start_line,signature) "
        "VALUES ('Function','aaa_other','src/x.py',10,'def aaa_other(x)')"
    )
    conn.execute(
        "INSERT INTO nodes (label,name,file_path,start_line,signature) "
        "VALUES ('Function','run','src/x.py',50,'def run(self, cfg)')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        pv, "_load_issue_anchors",
        lambda: {"symbols": ["run"], "paths": [], "test_names": []},
    )
    try:
        c = sqlite3.connect(path)
        lines = pv._contract_pillar(c, "src/x.py")
        c.close()
        assert lines, "contract suppressed despite a matching anchor 'run'"
        # 'run' is the anchor -> relevance 100 -> leads; it must NOT be dropped generic.
        assert "run(" in lines[0], lines
    finally:
        os.unlink(path)


def test_contract_pillar_caps_at_three():
    """Contract pillar must cap at 3 lines even with many functions."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, name TEXT,
            file_path TEXT, start_line INTEGER DEFAULT 0, signature TEXT,
            return_type TEXT, is_test INTEGER DEFAULT 0, language TEXT DEFAULT 'python'
        )
    """)
    for i in range(8):
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, start_line, signature) "
            "VALUES ('Function', ?, 'src/many.py', ?, ?)",
            (f"func_{i}", i, f"def func_{i}(x)"),
        )
    conn.commit()
    conn.close()
    try:
        c = sqlite3.connect(path)
        lines = _contract_pillar(c, "src/many.py")
        c.close()
        assert len(lines) <= 3
    finally:
        os.unlink(path)
