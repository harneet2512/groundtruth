"""Layer 2.4 — L4a auto-query uses the categorical edge filter (verified-only).

L4a's unique value is verified cross-file callers the agent can't grep —
not name_match noise. This test confirms the shared categorical filter
helper (also used by L3/L3b) produces a verified-only clause on the
post-merge schema and a numeric fallback otherwise. The in-container query
string interpolates this clause for both the symbol-ranking COUNT and the
caller subquery.
"""
import os
import sqlite3
import tempfile

from groundtruth.hooks.post_edit import _edge_filter_for_db


def _make_db(categorical: bool) -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    cols = "id INTEGER PRIMARY KEY, source_id INT, target_id INT, type TEXT, confidence REAL"
    if categorical:
        cols += ", resolution_method TEXT, trust_tier TEXT, candidate_count INT"
    conn.execute(f"CREATE TABLE edges ({cols})")
    conn.commit()
    conn.close()
    return path


def test_l4a_filter_categorical_on_post_merge():
    path = _make_db(categorical=True)
    try:
        clause = _edge_filter_for_db(path)
        assert "resolution_method" in clause
        assert "trust_tier" in clause
        # SUPPRESSED hard-excluded
        assert "SUPPRESSED" in clause
    finally:
        os.unlink(path)


def test_l4a_filter_numeric_fallback_on_legacy():
    path = _make_db(categorical=False)
    try:
        clause = _edge_filter_for_db(path)
        assert "confidence" in clause
        assert "trust_tier" not in clause
    finally:
        os.unlink(path)


def test_l4a_filter_clause_valid_sql():
    """The clause must be valid SQL with alias 'e' (both L4a queries use e)."""
    path = _make_db(categorical=True)
    try:
        clause = _edge_filter_for_db(path)
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, confidence, "
            "resolution_method, trust_tier, candidate_count) "
            "VALUES (1, 2, 'CALLS', 1.0, 'same_file', 'CERTIFIED', 1)"
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, confidence, "
            "resolution_method, trust_tier, candidate_count) "
            "VALUES (3, 4, 'CALLS', 0.4, 'name_match', 'SUPPRESSED', 5)"
        )
        conn.commit()
        rows = conn.execute(f"SELECT id FROM edges e WHERE e.type='CALLS' AND {clause}").fetchall()
        ids = {r[0] for r in rows}
        assert 1 in ids       # verified same_file admitted
        assert 2 not in ids   # SUPPRESSED name_match excluded
        conn.close()
    finally:
        os.unlink(path)
