"""Tests for Layer 2.2 — L3 post-edit categorical filter + G7 Contract fallback.

Verifies:
- _categorical_edge_filter_clause() returns valid SQL with the categorical
  combination (resolution_method / candidate_count / trust_tier).
- _edge_filter_for_db() picks categorical clause when post-merge columns
  are present; falls back to numeric on older schemas.
- The filter correctly admits CERTIFIED edges, excludes SUPPRESSED.
- Strong resolution methods (same_file, import, verified_unique, type_flow,
  import_type, lsp_verified) admit edges regardless of confidence number.
"""
import os
import sqlite3
import tempfile

import pytest

from groundtruth.hooks.post_edit import (
    _categorical_edge_filter_clause,
    _legacy_confidence_filter_clause,
    _edge_filter_for_db,
    _STRONG_RESOLUTION_METHODS,
    _STRONG_TRUST_TIERS,
    _SUPPRESSED_TRUST_TIER,
)


def _make_db(with_categorical_cols: bool) -> str:
    """Create a temp graph.db with edges populated for filter tests."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            language TEXT NOT NULL,
            is_test INTEGER DEFAULT 0
        )
    """)
    if with_categorical_cols:
        conn.execute("""
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                source_line INTEGER,
                resolution_method TEXT,
                confidence REAL DEFAULT 0.0,
                trust_tier TEXT DEFAULT 'SPECULATIVE',
                candidate_count INTEGER DEFAULT 1
            )
        """)
    else:
        conn.execute("""
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                source_line INTEGER,
                confidence REAL DEFAULT 0.0
            )
        """)
    conn.commit()
    conn.close()
    return path


def test_categorical_clause_admits_strong_resolution_methods():
    """All 6 strong resolution methods should appear in the clause."""
    clause = _categorical_edge_filter_clause()
    for method in _STRONG_RESOLUTION_METHODS:
        assert f"'{method}'" in clause


def test_categorical_clause_admits_strong_trust_tiers():
    clause = _categorical_edge_filter_clause()
    for tier in _STRONG_TRUST_TIERS:
        assert f"'{tier}'" in clause


def test_categorical_clause_excludes_suppressed_tier():
    clause = _categorical_edge_filter_clause()
    assert _SUPPRESSED_TRUST_TIER in clause
    assert "!=" in clause  # explicit exclusion


def test_categorical_clause_uses_candidate_count_disambiguation():
    """name_match with candidate_count <= 1 should be admitted."""
    clause = _categorical_edge_filter_clause()
    assert "name_match" in clause
    assert "candidate_count" in clause


def test_legacy_clause_uses_numeric_threshold():
    clause = _legacy_confidence_filter_clause(min_conf=0.6)
    assert ">= 0.6" in clause
    assert "confidence" in clause


def test_edge_filter_picks_categorical_on_post_merge_schema():
    path = _make_db(with_categorical_cols=True)
    try:
        clause = _edge_filter_for_db(path)
        # Should be the categorical version
        assert "resolution_method" in clause
        assert "trust_tier" in clause
    finally:
        os.unlink(path)


def test_edge_filter_falls_back_to_numeric_on_legacy_schema():
    path = _make_db(with_categorical_cols=False)
    try:
        clause = _edge_filter_for_db(path)
        # Should be the legacy version
        assert "confidence" in clause
        assert "0.6" in clause
        assert "trust_tier" not in clause
    finally:
        os.unlink(path)


def test_edge_filter_falls_back_on_missing_db():
    clause = _edge_filter_for_db("/nonexistent/path.db")
    assert "confidence" in clause
    assert "0.6" in clause


def test_categorical_clause_runs_in_sqlite():
    """The clause must be valid SQL that SQLite can execute."""
    path = _make_db(with_categorical_cols=True)
    try:
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, "
            "confidence, trust_tier, candidate_count) VALUES "
            "(1, 2, 'CALLS', 'same_file', 1.0, 'CERTIFIED', 1)"
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, "
            "confidence, trust_tier, candidate_count) VALUES "
            "(3, 4, 'CALLS', 'name_match', 0.4, 'SUPPRESSED', 5)"
        )
        conn.commit()
        clause = _categorical_edge_filter_clause()
        rows = conn.execute(
            f"SELECT id FROM edges e WHERE {clause}"
        ).fetchall()
        ids = {r[0] for r in rows}
        assert 1 in ids  # CERTIFIED + same_file admitted
        assert 2 not in ids  # SUPPRESSED excluded
        conn.close()
    finally:
        os.unlink(path)


def test_filter_admits_verified_unique_high_confidence():
    """verified_unique with confidence 0.95 should be admitted."""
    path = _make_db(with_categorical_cols=True)
    try:
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, "
            "confidence, trust_tier, candidate_count) VALUES "
            "(1, 2, 'CALLS', 'verified_unique', 0.95, 'CERTIFIED', 1)"
        )
        conn.commit()
        clause = _categorical_edge_filter_clause()
        rows = conn.execute(
            f"SELECT id FROM edges e WHERE {clause}"
        ).fetchall()
        assert len(rows) == 1
        conn.close()
    finally:
        os.unlink(path)


def test_filter_admits_unique_name_match():
    """name_match with candidate_count=1 should be admitted."""
    path = _make_db(with_categorical_cols=True)
    try:
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, "
            "confidence, trust_tier, candidate_count) VALUES "
            "(1, 2, 'CALLS', 'name_match', 0.9, 'CANDIDATE', 1)"
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, "
            "confidence, trust_tier, candidate_count) VALUES "
            "(3, 4, 'CALLS', 'name_match', 0.4, 'SPECULATIVE', 5)"
        )
        conn.commit()
        clause = _categorical_edge_filter_clause()
        rows = conn.execute(
            f"SELECT id FROM edges e WHERE {clause}"
        ).fetchall()
        ids = {r[0] for r in rows}
        assert 1 in ids  # unique name_match + CANDIDATE — admitted
        assert 2 not in ids  # ambiguous + SPECULATIVE — excluded
        conn.close()
    finally:
        os.unlink(path)
