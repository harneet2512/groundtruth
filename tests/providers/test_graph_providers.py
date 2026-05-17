"""Parity tests for Layer 4 graph providers.

Builds a tiny ``graph.db`` in-memory and verifies that the new provider
functions return the same shapes as the legacy SQL inside
``src/groundtruth/hooks/post_view.py::graph_navigation``.

These are admission-gate tests only. They prove the extracted providers
are byte-equivalent to the legacy queries on a fixed fixture. They do NOT
prove GT helps any agent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.providers.graph_providers import (
    CalleeEdge,
    CallerEdge,
    FunctionInfo,
    ImporterEdge,
    callee_provider,
    caller_provider,
    hub_scale_provider,
    importer_provider,
    in_degree_provider,
    top_functions_provider,
)
from groundtruth.providers.scoring import (
    issue_relevance_scorer,
    score_edges_by_issue_relevance,
)


def _make_db(tmp_path: Path) -> str:
    """Build a small graph.db with deterministic content."""
    db = tmp_path / "graph.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            label TEXT,
            name TEXT,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported INTEGER DEFAULT 0,
            is_test INTEGER DEFAULT 0,
            language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_id INTEGER,
            target_id INTEGER,
            type TEXT,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.5,
            metadata TEXT
        );
        """
    )
    # nodes
    rows = [
        # id, label, name, q, file_path, start, end, sig, ret, exp, test, lang, parent
        (1, "Function", "target", "core.target", "core/target.py", 10, 30, "def target(a, b)", "int", 1, 0, "python", 0),
        (2, "Function", "caller_one", "users.foo", "users/foo.py", 5, 20, "def caller_one()", None, 1, 0, "python", 0),
        (3, "Function", "caller_two", "users.bar", "users/bar.py", 5, 20, "def caller_two()", None, 1, 0, "python", 0),
        (4, "Function", "caller_three", "shared.utils", "shared/utils.py", 1, 10, "def caller_three()", None, 1, 0, "python", 0),
        (5, "Function", "callee_low", "core.helper", "core/helper.py", 1, 10, "def callee_low()", None, 1, 0, "python", 0),
        (6, "Function", "test_target", "tests.test_core", "tests/test_core.py", 1, 8, "def test_target()", None, 0, 1, "python", 0),
    ]
    con.executemany(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
    )
    # edges: callers/callees/importers
    edges = [
        # source -> target, type, conf
        (2, 1, "CALLS", 1.0),  # users/foo.py -> core/target.py
        (2, 1, "CALLS", 1.0),  # duplicate edge to boost count
        (3, 1, "CALLS", 0.6),
        (4, 1, "CALLS", 0.3),  # below confidence floor
        (1, 5, "CALLS", 1.0),  # core/target.py -> core/helper.py
        (2, 5, "CALLS", 1.0),  # users/foo -> core/helper
        (3, 5, "IMPORTS", 1.0),
        (4, 1, "IMPORTS", 1.0),
    ]
    for src, tgt, etype, conf in edges:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, confidence) VALUES (?, ?, ?, ?, ?)",
            (src, tgt, etype, 7, conf),
        )
    con.commit()
    con.close()
    return str(db)


class TestCallerProvider:
    def test_returns_callers_ordered_by_count(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        rows = caller_provider(db, "core/target.py", limit=5)
        assert rows == [
            CallerEdge(file_path="users/foo.py", count=2),
            CallerEdge(file_path="users/bar.py", count=1),
        ]

    def test_filters_below_min_confidence(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        # shared/utils -> target has confidence 0.3; with default floor 0.5 it must be excluded.
        rows = caller_provider(db, "core/target.py")
        files = [r.file_path for r in rows]
        assert "shared/utils.py" not in files

    def test_includes_low_conf_when_min_lowered(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        rows = caller_provider(db, "core/target.py", min_confidence=0.0)
        files = [r.file_path for r in rows]
        assert "shared/utils.py" in files

    def test_missing_db_returns_empty(self) -> None:
        assert caller_provider("/no/such/db", "foo.py") == []


class TestCalleeProvider:
    def test_returns_callees(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        rows = callee_provider(db, "core/target.py")
        assert rows == [CalleeEdge(file_path="core/helper.py", count=1)]

    def test_does_not_return_caller_as_callee(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        # users/foo.py -> core/target.py is a CALL into target; when we ask for
        # callees of target, foo should not appear.
        rows = callee_provider(db, "core/target.py")
        files = [r.file_path for r in rows]
        assert "users/foo.py" not in files


class TestImporterProvider:
    def test_returns_importers(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        rows = importer_provider(db, "core/target.py")
        assert ImporterEdge(file_path="shared/utils.py") in rows

    def test_limits_results(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        rows = importer_provider(db, "core/target.py", limit=1)
        assert len(rows) <= 1


class TestTopFunctions:
    def test_orders_by_reference_count(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        rows = top_functions_provider(db, "core/target.py", limit=5)
        assert rows[0].name == "target"
        assert rows[0].ref_count >= 1

    def test_excludes_tests(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        rows = top_functions_provider(db, "tests/test_core.py")
        assert rows == []


class TestInDegreeAndHubScale:
    def test_in_degree_counts_calls_only(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        # target has 4 CALLS edges into it (one is conf=0.3, but in_degree
        # counts all CALLS regardless of confidence — mirrors the legacy hub
        # query in post_view).
        deg = in_degree_provider(db, "core/target.py")
        assert deg == 4

    def test_hub_scale_returns_50_on_empty(self, tmp_path: Path) -> None:
        # Empty db (no edges) — fallback to 50.
        empty = tmp_path / "empty.db"
        sqlite3.connect(str(empty)).executescript(
            "CREATE TABLE nodes (id INT, file_path TEXT); "
            "CREATE TABLE edges (source_id INT, target_id INT, type TEXT, confidence REAL);"
        )
        assert hub_scale_provider(str(empty)) == 50

    def test_hub_scale_returns_real_p90(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        scale = hub_scale_provider(db)
        # Only target has a meaningful in-degree (4); helper has 2; others 0.
        # p90 lands on one of those buckets — assert > 0 to prove the query ran.
        assert scale >= 1


class TestIssueRelevanceScorer:
    def test_empty_terms_returns_zero(self) -> None:
        assert issue_relevance_scorer("anything", set()) == 0.0

    def test_partial_overlap(self) -> None:
        # 2/3 terms present
        assert issue_relevance_scorer("alpha gamma", {"alpha", "beta", "gamma"}) == pytest.approx(2 / 3)

    def test_case_insensitive(self) -> None:
        assert issue_relevance_scorer("ALPHA Beta", {"alpha", "beta"}) == 1.0


class TestScoreEdgesByIssue:
    def test_orders_by_keyword_hits(self, tmp_path: Path) -> None:
        # Create three files; rank by issue-keyword presence.
        (tmp_path / "a.py").write_text("def timezone(): pass\n")
        (tmp_path / "b.py").write_text("def foo(): pass\n")
        (tmp_path / "c.py").write_text("import datetime; def timezone(): pass\n")
        rows = score_edges_by_issue_relevance(
            [("a.py", 3), ("b.py", 1), ("c.py", 2)],
            str(tmp_path),
            {"timezone", "datetime"},
        )
        # c.py has both terms (2/2), a.py has 1/2, b.py has 0/2.
        assert rows[0][0] == "c.py"
        assert rows[-1][0] == "b.py"

    def test_no_issue_terms_preserves_order(self, tmp_path: Path) -> None:
        rows = score_edges_by_issue_relevance([("a.py", 1), ("b.py", 2)], str(tmp_path), set())
        assert [r[0] for r in rows] == ["a.py", "b.py"]


class TestParityWithLegacySQL:
    """Pin: the provider returns the same SQL result as the legacy inline query."""

    def test_caller_provider_matches_legacy_query(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        # Issue the legacy query directly.
        conn = sqlite3.connect(db)
        rows = conn.execute(
            """
            SELECT DISTINCT nsrc.file_path, COUNT(*) AS cnt
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
              AND COALESCE(e.confidence, 0.5) >= 0.5
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path = ?
              AND nsrc.file_path != ?
            GROUP BY nsrc.file_path
            ORDER BY cnt DESC
            LIMIT 5
            """,
            ("core/target.py", "core/target.py"),
        ).fetchall()
        conn.close()
        legacy = [(r[0], r[1]) for r in rows]
        provider = [(r.file_path, r.count) for r in caller_provider(db, "core/target.py", limit=5)]
        assert provider == legacy
