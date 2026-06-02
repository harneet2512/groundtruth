"""Red->green regression tests for post_edit.py defects (2026-06-01).

P0-3: `Impact:` block re-admits name_match callers (the change_impact NUMERIC
      min_confidence=0.9 gate accepts a name_match-with-1-candidate edge at
      conf 0.9, the exact os.walk->account.walk laundering the categorical
      FACT filter prevents). After the fix, a name_match caller that is dropped
      from [CONTRACT] must ALSO be absent from Impact:.

P1-5: `[PATTERN] sibling` rendered the positionally-first same-file top-level
      function even at relevance 0. After the fix: a same-class twin still
      fires; two unrelated top-level functions (no anchor/term overlap) -> no
      [PATTERN] sibling.

P3:   G7 isolated INFO claimed "isolated" even on a resolution MISS. After the
      fix: an unresolved node yields a "could not resolve" note, not "isolated".
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from groundtruth.hooks import post_edit
from groundtruth.hooks.post_edit import (
    g7_filter_isolated,
    generate_improved_evidence,
)


# --------------------------------------------------------------------------
# Schema helpers (mirrors the real graph.db; includes categorical columns so
# the FACT filter takes the categorical path).
# --------------------------------------------------------------------------
def _make_graph(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
        file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
        signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
        is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
    )""")
    conn.execute("""CREATE TABLE edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER, target_id INTEGER, type TEXT,
        source_line INTEGER, source_file TEXT, resolution_method TEXT,
        confidence REAL DEFAULT 0.0, trust_tier TEXT, candidate_count INTEGER,
        metadata TEXT
    )""")
    return conn


# ==========================================================================
# P0-3: name_match must NOT surface in Impact:
# ==========================================================================
def _build_namematch_caller_graph(db_path: str) -> None:
    """`walk` is edited; its only cross-file caller resolves via name_match
    (1 candidate -> conf 0.9). The categorical FACT filter drops it from
    [CONTRACT]; the buggy Impact: block (numeric >=0.9) re-admits it."""
    conn = _make_graph(db_path)
    # Edited function (target of the change)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "signature, is_exported, is_test, language) VALUES "
        "(1, 'Function', 'walk', 'pkg/account.py', 10, 30, "
        "'def walk(self):', 1, 0, 'python')"
    )
    # Cross-file caller, resolved only by name_match (NOT a deterministic fact).
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "is_exported, is_test, language) VALUES "
        "(2, 'Function', 'caller_fn', 'pkg/other.py', 5, 20, 1, 0, 'python')"
    )
    # name_match edge with confidence 0.9 + SPECULATIVE tier (categorical filter
    # excludes name_match; numeric >=0.9 admits it -> the laundering).
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, source_line, "
        "resolution_method, confidence, trust_tier, candidate_count) VALUES "
        "(2, 1, 'CALLS', 12, 'name_match', 0.9, 'SPECULATIVE', 1)"
    )
    conn.commit()
    conn.close()


def test_p0_3_namematch_caller_dropped_from_contract():
    """RED baseline assertion: the FACT filter drops the name_match caller from
    the caller-evidence path (it must never appear as a confident [CONTRACT]/
    [CALLERS] caller)."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = os.path.join(tmp, "graph.db")
        _build_namematch_caller_graph(db)
        out = generate_improved_evidence(
            "pkg/account.py", ["walk"], db, tmp,
        )
        # caller_fn must NOT be rendered as a confident caller anywhere.
        assert "caller_fn" not in out, (
            "name_match caller leaked into caller evidence:\n" + out
        )


def test_p0_3_namematch_caller_absent_from_impact():
    """The core P0-3 fix: name_match caller must NOT appear in Impact:."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = os.path.join(tmp, "graph.db")
        _build_namematch_caller_graph(db)
        out = generate_improved_evidence(
            "pkg/account.py", ["walk"], db, tmp,
        )
        # Before fix: change_impact (numeric >=0.9) re-admitted caller_fn into
        # an "Impact:" block. After fix: the categorical FACT filter drops it,
        # and with no surviving impacted caller the whole Impact block is gone.
        assert "Impact:" not in out, (
            "Impact: block surfaced a laundered name_match caller:\n" + out
        )
        assert "caller_fn" not in out


def test_p0_3_verified_caller_still_surfaces_in_impact():
    """Do-no-harm: a genuinely verified (import) caller still appears in Impact:."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = os.path.join(tmp, "graph.db")
        conn = _make_graph(db)
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
            "signature, is_exported, is_test, language) VALUES "
            "(1, 'Function', 'walk', 'pkg/account.py', 10, 30, "
            "'def walk(self):', 1, 0, 'python')"
        )
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
            "is_exported, is_test, language) VALUES "
            "(2, 'Function', 'verified_caller', 'pkg/other.py', 5, 20, 1, 0, 'python')"
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, "
            "resolution_method, confidence, trust_tier, candidate_count) VALUES "
            "(2, 1, 'CALLS', 12, 'import', 1.0, 'CERTIFIED', 1)"
        )
        conn.commit()
        conn.close()
        out = generate_improved_evidence("pkg/account.py", ["walk"], db, tmp)
        assert "Impact:" in out, "verified caller wrongly suppressed:\n" + out
        assert "verified_caller" in out


# ==========================================================================
# P1-5: sibling [PATTERN] relevance floor
# ==========================================================================
@pytest.fixture(autouse=True)
def _clear_issue_terms(monkeypatch):
    """Ensure no stray /tmp issue-terms file biases relevance gates."""
    monkeypatch.setattr(post_edit, "_ISSUE_TERMS_PATH",
                        "/tmp/gt_issue_terms_nonexistent_test.txt")
    monkeypatch.setattr(post_edit, "_ISSUE_ANCHORS_PATH",
                        "/tmp/gt_issue_anchors_nonexistent_test.json")
    yield


def _write_source(repo_root: str, rel: str, lines: list[str]) -> None:
    full = os.path.join(repo_root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def test_p1_5_same_class_sibling_still_fires():
    """(a) A genuine same-class sibling still renders [PATTERN] sibling."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = os.path.join(tmp, "graph.db")
        conn = _make_graph(db)
        # Class parent
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
            "language) VALUES (1, 'Class', 'Importer', 'pkg/imp.py', 1, 80, 'python')"
        )
        # Edited method + a sibling method, same class (parent_id=1)
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
            "signature, language, parent_id) VALUES "
            "(2, 'Method', 'set_fields', 'pkg/imp.py', 10, 20, "
            "'def set_fields(self):', 'python', 1)"
        )
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
            "signature, language, parent_id) VALUES "
            "(3, 'Method', 'set_tags', 'pkg/imp.py', 30, 45, "
            "'def set_tags(self):', 'python', 1)"
        )
        conn.commit()
        conn.close()
        _write_source(tmp, "pkg/imp.py", [
            "class Importer:",
            *["    pad" for _ in range(8)],
            "    def set_fields(self):",
            *["        x = 1" for _ in range(9)],
            *["    pad2" for _ in range(9)],
            "    def set_tags(self):",
            "        self.write_tag()",
            "        return True",
        ] + ["    pad3" for _ in range(15)])
        out = generate_improved_evidence("pkg/imp.py", ["set_fields"], db, tmp)
        assert "[PATTERN] sibling set_tags" in out, (
            "same-class sibling must still fire:\n" + out
        )


def test_p1_5_unrelated_toplevel_peer_suppressed():
    """(b) Two unrelated top-level functions, edit one, no anchor/term overlap
    -> NO [PATTERN] sibling (positionally-first noise removed)."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = os.path.join(tmp, "graph.db")
        conn = _make_graph(db)
        # Two top-level functions, no parent class, unrelated names.
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
            "signature, language, parent_id) VALUES "
            "(1, 'Function', 'compute_alpha', 'pkg/util.py', 5, 12, "
            "'def compute_alpha():', 'python', 0)"
        )
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
            "signature, language, parent_id) VALUES "
            "(2, 'Function', 'render_widget', 'pkg/util.py', 20, 30, "
            "'def render_widget():', 'python', 0)"
        )
        conn.commit()
        conn.close()
        _write_source(tmp, "pkg/util.py", [
            "def compute_alpha():",
            "    return 1",
            *["pad" for _ in range(16)],
            "def render_widget():",
            "    draw()",
            "    return None",
        ] + ["pad2" for _ in range(15)])
        # Edit compute_alpha; render_widget shares no tokens with it and no
        # issue terms exist -> must NOT render as a [PATTERN] sibling.
        out = generate_improved_evidence("pkg/util.py", ["compute_alpha"], db, tmp)
        assert "[PATTERN] sibling" not in out, (
            "unrelated top-level peer leaked as sibling noise:\n" + out
        )


# ==========================================================================
# P3: G7 isolated INFO distinguishes resolution miss from true isolate
# ==========================================================================
def test_p3_resolved_isolate_says_isolated():
    """When node WAS resolved and genuinely has no neighbors -> 'isolated'."""
    kept = g7_filter_isolated([], sig="", resolved=True)
    assert kept and "appears isolated" in kept[0]


def test_p3_unresolved_node_says_could_not_resolve():
    """When node was NOT resolved (anon/arrow/not-indexed) -> resolution-miss
    note, NOT a misleading 'isolated' claim."""
    kept = g7_filter_isolated([], sig="", resolved=False)
    assert kept
    assert "appears isolated" not in kept[0], (
        "resolution miss wrongly labeled 'isolated': " + kept[0]
    )
    assert "Could not resolve" in kept[0]


def test_p3_signature_still_preferred_when_present():
    """Do-no-harm: a stored signature still wins over either INFO note."""
    kept = g7_filter_isolated([], sig="def f() -> int:", resolved=False)
    assert kept == ["[SIGNATURE] def f() -> int:"]
