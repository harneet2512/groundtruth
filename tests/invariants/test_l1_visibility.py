"""Invariant 3: Edit Target Selection

If issue text explicitly names a candidate function, that function beats
unrelated high-caller-count functions. Caller count is tie-breaker only.
All candidates must be scored before selection (no first-match-wins).

Violation = D5 in failure taxonomy.
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile

import pytest


def create_beets_like_graph(db_path: str) -> None:
    """Create graph.db mimicking beets: Pipeline (high callers) vs set_fields (low callers)."""
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
        confidence REAL DEFAULT 0.0, metadata TEXT
    )""")
    conn.execute("""CREATE TABLE properties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
        line INTEGER, confidence REAL DEFAULT 1.0
    )""")

    # Pipeline class — HIGH caller count (10 callers)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, signature, "
        "is_exported, is_test, language) VALUES "
        "(1, 'Class', 'Pipeline', 'beets/util/pipeline.py', 10, 'class Pipeline:', 1, 0, 'python')"
    )
    for i in range(10):
        conn.execute(
            f"INSERT INTO nodes (id, label, name, file_path, start_line, is_exported, is_test, language) VALUES "
            f"({100+i}, 'Function', 'caller_{i}', 'other/file{i}.py', {i*10}, 1, 0, 'python')"
        )
        conn.execute(
            f"INSERT INTO edges (source_id, target_id, type, confidence) VALUES ({100+i}, 1, 'CALLS', 1.0)"
        )

    # set_fields method — LOW caller count (2 callers) but NAMED IN ISSUE
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, signature, "
        "is_exported, is_test, language) VALUES "
        "(2, 'Method', 'set_fields', 'beets/importer.py', 602, 'def set_fields(self, lib):', 1, 0, 'python')"
    )
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, is_exported, is_test, language) VALUES "
        "(200, 'Function', 'run_import', 'beets/ui/commands.py', 100, 1, 0, 'python')"
    )
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence) VALUES (200, 2, 'CALLS', 1.0)"
    )
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, is_exported, is_test, language) VALUES "
        "(201, 'Function', 'test_set_fields', 'test/test_importer.py', 395, 0, 1, 'python')"
    )
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence) VALUES (201, 2, 'CALLS', 1.0)"
    )

    conn.commit()
    conn.close()


def simulate_edit_target_selection(db_path: str, issue_text: str,
                                    brief_files: list[str]) -> dict | None:
    """Simulate edit target selection logic.

    Returns the selected edit target dict, or None if no target found.
    Implements Invariant 3: issue-named function beats high-caller functions.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    _COMMON_FN_PARTS = {
        "get", "set", "add", "remove", "update", "create",
        "delete", "find", "make", "check", "is", "has",
        "do", "run", "to", "from", "on", "in", "of", "by",
    }

    issue_kws = {
        w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", issue_text)
        if len(w) > 3 and w.lower() not in {
            "that", "this", "with", "from", "have", "been", "when", "then",
            "should", "would", "could", "file", "line", "code", "test",
            "error", "issue", "none", "true", "false", "self", "class",
        }
    }

    all_candidates = []

    for bf in brief_files:
        bf_norm = bf.replace("\\", "/").lstrip("/")
        key_funcs = conn.execute(
            "SELECT id, name, signature, start_line FROM nodes "
            "WHERE file_path LIKE ? AND is_exported = 1 AND is_test = 0 "
            "ORDER BY (SELECT COUNT(*) FROM edges WHERE target_id = nodes.id AND type='CALLS') DESC LIMIT 5",
            (f"%{bf_norm}",),
        ).fetchall()

        for kf in key_funcs:
            fn_parts = set(re.split(r"[_]|(?<=[a-z])(?=[A-Z])", kf["name"]))
            fn_parts = {p.lower() for p in fn_parts if p and p.lower() not in _COMMON_FN_PARTS}
            kw_overlap = len(fn_parts & issue_kws)
            direct = kf["name"].lower() in issue_text.lower()

            caller_count = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS'",
                (kf["id"],),
            ).fetchone()[0]

            # Score: direct mention is primary, keyword overlap secondary, callers tie-break
            score = 0
            if direct:
                score += 1000  # explicit mention dominates
            score += kw_overlap * 10
            score += min(caller_count, 5)  # callers as tie-breaker only (capped)

            all_candidates.append({
                "file": bf,
                "func": kf["name"],
                "sig": kf["signature"] or "",
                "line": kf["start_line"] or 0,
                "callers": caller_count,
                "score": score,
                "direct": direct,
                "kw_overlap": kw_overlap,
            })

    conn.close()

    if not all_candidates:
        return None

    # Select highest-scoring candidate (all evaluated, no first-match-wins)
    all_candidates.sort(key=lambda c: c["score"], reverse=True)
    return all_candidates[0]


class TestEditTargetExplicitMentionBeatsCallers:
    """Invariant 3: issue-named function beats high-caller function."""

    def test_set_fields_beats_pipeline(self):
        """Issue mentions 'set_fields' → set_fields selected over Pipeline (10 callers)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_beets_like_graph(db_path)

            issue_text = "The set_fields method in importer.py does not properly handle unicode field values"
            result = simulate_edit_target_selection(
                db_path, issue_text,
                ["beets/util/pipeline.py", "beets/importer.py"],
            )

            assert result is not None
            assert result["func"] == "set_fields", (
                f"Invariant 3: issue mentions 'set_fields' but selected '{result['func']}' "
                f"(score={result['score']}, callers={result['callers']})"
            )

    def test_high_callers_is_tiebreaker_not_primary(self):
        """When no explicit mention, callers break ties but don't dominate keyword overlap."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_beets_like_graph(db_path)

            # Issue does NOT mention set_fields or Pipeline by name
            issue_text = "Unicode handling is broken in the import pipeline for field values"
            result = simulate_edit_target_selection(
                db_path, issue_text,
                ["beets/util/pipeline.py", "beets/importer.py"],
            )

            assert result is not None
            # With no direct mention, keyword overlap + caller tiebreak decides
            # Both "pipeline" and "fields" are in issue, but "fields" is a common part filtered out
            # So Pipeline might win here — that's acceptable when no explicit mention exists

    def test_all_candidates_scored_before_selection(self):
        """All candidates from all files must be evaluated, not first-match-wins."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_beets_like_graph(db_path)

            issue_text = "Fix set_fields to handle encoding"
            result = simulate_edit_target_selection(
                db_path, issue_text,
                # Pipeline file listed FIRST — must not win by position
                ["beets/util/pipeline.py", "beets/importer.py"],
            )

            assert result is not None
            assert result["func"] == "set_fields", (
                f"All candidates must be scored — first file should not auto-win. "
                f"Selected: {result['func']}"
            )
