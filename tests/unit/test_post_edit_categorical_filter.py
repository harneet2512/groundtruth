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
    g7_filter_isolated,
    _STRONG_RESOLUTION_METHODS,
    _STRONG_TRUST_TIERS,
    _SUPPRESSED_TRUST_TIER,
)


# ---------------------------------------------------------------------------
# G7 isolation gate (Contract pillar always-fire)
# ---------------------------------------------------------------------------

def test_g7_drops_caller_derived_markers():
    """Caller-derived markers dropped when function is isolated."""
    parts = [
        "[CALLERS] views.py:45 `bar()`",
        "[PROPAGATE] foo.py:12",
        "[IMPACT] direct: x()",
        "[MISMATCH] you removed q",
        "[REVIEW] PRESERVE: ...",
        "CALLERS: token usage",
        "[CONTRACT] 5 callers depend",
    ]
    kept = g7_filter_isolated(parts, sig="def foo(x) -> int")
    # All caller-derived dropped; falls back to signature.
    assert kept == ["[SIGNATURE] def foo(x) -> int"]


def test_g7_keeps_contract_consistency_completeness():
    """Pillar markers survive isolation per CLAUDE.md:59."""
    parts = [
        "[SIGNATURE] def foo(x: int) -> str",
        "[BEHAVIORAL CONTRACT]\nPRESERVE: if not x: raise",
        "[RAISES] ValueError when x is None",
        "[OVERRIDE] BaseService.foo()",
        "[TWIN] sibling pattern",
        "TWINS: helper.py shares 3 calls",
        "[TEST] test_foo asserts None",
        "[COMPLETENESS] 2 test groups",
        "[SCOPE] multi-file: a.py, b.py",
        "[CALLERS] should be dropped",
    ]
    kept = g7_filter_isolated(parts, sig="def foo(x: int) -> str")
    # All pillar markers kept, caller-derived dropped.
    assert "[CALLERS] should be dropped" not in kept
    assert any("TWINS:" in k for k in kept)
    assert any("[SCOPE]" in k for k in kept)
    assert any("[BEHAVIORAL CONTRACT]" in k for k in kept)
    assert any("[TEST]" in k for k in kept)
    assert len(kept) == 9  # 10 input minus 1 caller-derived


def test_g7_signature_fallback_when_only_caller_evidence():
    """When all evidence is caller-derived, fall back to [SIGNATURE]."""
    parts = ["[CALLERS] views.py:45", "[PROPAGATE] foo.py:12"]
    kept = g7_filter_isolated(parts, sig="def bar()")
    assert kept == ["[SIGNATURE] def bar()"]


def test_g7_honest_note_when_nothing_knowable():
    """When no pillar evidence and no signature, emit honest isolation note."""
    parts = ["[CALLERS] views.py:45"]
    kept = g7_filter_isolated(parts, sig="")
    assert len(kept) == 1
    assert "isolated" in kept[0].lower()
    assert kept[0].startswith("[INFO]")


def test_g7_empty_parts_with_signature():
    kept = g7_filter_isolated([], sig="def foo() -> None")
    assert kept == ["[SIGNATURE] def foo() -> None"]


def test_g7_empty_parts_no_signature():
    kept = g7_filter_isolated([], sig="")
    assert len(kept) == 1
    assert "isolated" in kept[0].lower()


def test_g7_keeps_l5_advisories():
    """L5 advisory markers (L<digit>) survive isolation."""
    parts = ["L5: scaffold advisory", "[CALLERS] drop me"]
    kept = g7_filter_isolated(parts, sig="def x()")
    assert any(k.startswith("L5") for k in kept)
    assert not any("[CALLERS]" in k for k in kept)


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
