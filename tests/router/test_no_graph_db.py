"""NO_GRAPH_DB classification — router must distinguish absent vs empty."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.router import CollaborationRouter
from groundtruth.router.decisions import SuppressionReason
from groundtruth.state.agent_state import AgentState


def _make_empty_graph_db(tmp_path: Path) -> str:
    """Real schema, zero rows. Different from a missing file."""
    db = tmp_path / "graph.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT,
            is_exported INTEGER, is_test INTEGER,
            language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.5,
            metadata TEXT
        );
        """
    )
    con.commit()
    con.close()
    return str(db)


class TestNoGraphDbDistinct:
    def test_missing_file_yields_no_graph_db_on_view(self, tmp_path: Path) -> None:
        state = AgentState.create(task_id="t", max_iterations=100, repo_root="/repo")
        router = CollaborationRouter(state, str(tmp_path / "absent.db"), "/repo")
        em = router.on_view("core/x.py")
        assert em.emit is False
        assert em.suppression_reason == SuppressionReason.NO_GRAPH_DB
        assert em.suppression_detail == "graph_db_missing"

    def test_missing_file_yields_no_graph_db_on_edit(self, tmp_path: Path) -> None:
        state = AgentState.create(task_id="t", max_iterations=100, repo_root="/repo")
        router = CollaborationRouter(state, "", "/repo")
        em = router.on_edit("core/x.py", ["target"])
        assert em.emit is False
        assert em.suppression_reason == SuppressionReason.NO_GRAPH_DB

    def test_empty_db_yields_no_evidence_not_no_graph_db(self, tmp_path: Path) -> None:
        """Schema exists, rows are empty — providers run and return [].

        This is a deliberately stricter contract than "no rows": the router
        must NOT collapse "empty graph" into "missing graph", or the metric
        attribution becomes meaningless.
        """
        db = _make_empty_graph_db(tmp_path)
        state = AgentState.create(task_id="t", max_iterations=100, repo_root="/repo")
        router = CollaborationRouter(state, db, "/repo")
        em = router.on_view("core/x.py")
        assert em.emit is False
        assert em.suppression_reason == SuppressionReason.NO_EVIDENCE
        assert em.suppression_reason != SuppressionReason.NO_GRAPH_DB

    def test_no_graph_db_short_circuits_before_dedup(self, tmp_path: Path) -> None:
        """A second on_view against a missing graph also reports NO_GRAPH_DB,
        not DUPLICATE or DEBOUNCE — i.e., the classifier is in front of dedup.
        """
        state = AgentState.create(task_id="t", max_iterations=100, repo_root="/repo")
        router = CollaborationRouter(state, "/no/such.db", "/repo")
        em1 = router.on_view("core/x.py")
        em2 = router.on_view("core/y.py")
        assert em1.suppression_reason == SuppressionReason.NO_GRAPH_DB
        assert em2.suppression_reason == SuppressionReason.NO_GRAPH_DB

    def test_provider_counters_zero_when_no_graph_db(self, tmp_path: Path) -> None:
        state = AgentState.create(task_id="t", max_iterations=100, repo_root="/repo")
        router = CollaborationRouter(state, "/no/such.db", "/repo")
        router.on_view("core/x.py")
        router.on_edit("core/y.py", ["target"])
        # No providers were consulted; the counters stay at zero.
        assert router.provider_request_count == 0
        assert router.provider_empty_count == 0
        assert router.provider_request_log == []
