"""Layer 3 router timing tests for ``CollaborationRouter.on_view``.

These are admission-gate tests — they prove the router suppresses for the right
reasons and emits when it should, on small fixtures. They do NOT prove the
router helps any agent in practice.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.router import CollaborationRouter, EmissionKind, SuppressionReason
from groundtruth.state.agent_state import AgentState


def _build_db(tmp_path: Path) -> str:
    """Tiny graph.db. target.py has 2 callers, 1 callee, 1 importer."""
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
    nodes = [
        (1, "Function", "target", "", "core/target.py", 1, 5, "def target()", "int", 1, 0, "python", 0),
        (2, "Function", "caller", "", "users/foo.py", 1, 3, "def caller()", None, 1, 0, "python", 0),
        (3, "Function", "caller2", "", "users/bar.py", 1, 3, "def caller2()", None, 1, 0, "python", 0),
        (4, "Function", "helper", "", "core/helper.py", 1, 3, "def helper()", None, 1, 0, "python", 0),
        (5, "Function", "importer", "", "users/baz.py", 1, 3, "def importer()", None, 1, 0, "python", 0),
    ]
    con.executemany(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", nodes,
    )
    edges = [
        (2, 1, "CALLS", 1, 1.0),
        (3, 1, "CALLS", 1, 1.0),
        (1, 4, "CALLS", 1, 1.0),
        (5, 1, "IMPORTS", 1, 1.0),
    ]
    for s, t, ty, line, conf in edges:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, confidence) VALUES (?, ?, ?, ?, ?)",
            (s, t, ty, line, conf),
        )
    con.commit()
    con.close()
    return str(db)


def _new_state(task: str = "t", max_iter: int = 100) -> AgentState:
    return AgentState.create(task_id=task, max_iterations=max_iter, repo_root="/repo")


class TestUnvisitedRelevantEdgeEmits:
    def test_emits_with_caller_primary(self, tmp_path: Path) -> None:
        db = _build_db(tmp_path)
        s = _new_state()
        r = CollaborationRouter(s, db, repo_root="/repo")
        em = r.on_view("/repo/core/target.py")
        assert em.emit is True
        assert em.kind == EmissionKind.ON_VIEW_NEIGHBORHOOD
        assert em.primary_edge_file in ("users/foo.py", "users/bar.py")
        assert em.next_action_type == "READ_CALLER_CONTRACT"
        assert em.evidence_text
        assert em.evidence_items


class TestAlreadyViewedSuppresses:
    def test_all_neighbors_viewed_yields_stale(self, tmp_path: Path) -> None:
        db = _build_db(tmp_path)
        s = _new_state()
        # Pre-load every neighbor file as "already viewed".
        for f in ("users/foo.py", "users/bar.py", "core/helper.py", "users/baz.py"):
            s.record_view(f, sync_legacy_file=False)
        r = CollaborationRouter(s, db, repo_root="/repo")
        em = r.on_view("core/target.py")
        assert em.emit is False
        assert em.suppression_reason == SuppressionReason.STALE


class TestDuplicateSuppresses:
    def test_same_view_and_primary_yields_duplicate(self, tmp_path: Path) -> None:
        db = _build_db(tmp_path)
        s = _new_state()
        r = CollaborationRouter(s, db, repo_root="/repo")
        em1 = r.on_view("core/target.py")
        assert em1.emit is True
        # Advance iteration past debounce so the next call gets past the
        # debounce gate and we exercise the dedup gate specifically.
        s.set_iteration(em1.iteration + r.debounce_iters + 1)
        em2 = r.on_view("core/target.py")
        assert em2.emit is False
        assert em2.suppression_reason == SuppressionReason.DUPLICATE


class TestLateBandSuppresses:
    def test_late_band_yields_too_late(self, tmp_path: Path) -> None:
        db = _build_db(tmp_path)
        s = _new_state(max_iter=10)
        s.set_iteration(9)  # 90% of max -> beyond late_band_ratio 0.75
        r = CollaborationRouter(s, db, repo_root="/repo")
        em = r.on_view("core/target.py")
        assert em.emit is False
        assert em.suppression_reason == SuppressionReason.TOO_LATE


class TestNoEvidence:
    def test_unknown_file_yields_no_evidence(self, tmp_path: Path) -> None:
        db = _build_db(tmp_path)
        s = _new_state()
        r = CollaborationRouter(s, db, repo_root="/repo")
        em = r.on_view("core/does_not_exist.py")
        assert em.emit is False
        assert em.suppression_reason == SuppressionReason.NO_EVIDENCE

    def test_empty_path_yields_no_evidence(self, tmp_path: Path) -> None:
        db = _build_db(tmp_path)
        s = _new_state()
        r = CollaborationRouter(s, db, repo_root="/repo")
        em = r.on_view("")
        assert em.emit is False
        assert em.suppression_reason == SuppressionReason.NO_EVIDENCE


class TestBudgetSuppresses:
    def test_view_budget_cap(self, tmp_path: Path) -> None:
        db = _build_db(tmp_path)
        s = _new_state()
        r = CollaborationRouter(s, db, repo_root="/repo", view_budget=1, total_budget=99)
        # Two distinct files so dedup doesn't kick in first.
        em1 = r.on_view("core/target.py")
        assert em1.emit is True
        s.set_iteration(em1.iteration + r.debounce_iters + 1)
        em2 = r.on_view("users/foo.py")
        assert em2.emit is False
        assert em2.suppression_reason == SuppressionReason.BUDGET
        assert "view_budget_reached" in em2.suppression_detail

    def test_total_budget_cap(self, tmp_path: Path) -> None:
        db = _build_db(tmp_path)
        s = _new_state()
        r = CollaborationRouter(s, db, repo_root="/repo", view_budget=99, total_budget=1)
        em1 = r.on_view("core/target.py")
        assert em1.emit is True
        s.set_iteration(em1.iteration + r.debounce_iters + 1)
        em2 = r.on_view("users/foo.py")
        assert em2.emit is False
        assert em2.suppression_reason == SuppressionReason.BUDGET
        assert "total_budget_reached" in em2.suppression_detail


class TestDebounceSuppresses:
    def test_immediate_re_view_debounces(self, tmp_path: Path) -> None:
        db = _build_db(tmp_path)
        s = _new_state()
        r = CollaborationRouter(s, db, repo_root="/repo")
        em1 = r.on_view("core/target.py")
        assert em1.emit is True
        # Same iteration => same-kind debounce window applies.
        em2 = r.on_view("users/foo.py")
        assert em2.emit is False
        assert em2.suppression_reason == SuppressionReason.DEBOUNCE


class TestSuppressionReasonsRecorded:
    def test_every_suppression_has_reason_and_detail(self, tmp_path: Path) -> None:
        db = _build_db(tmp_path)
        s = _new_state()
        r = CollaborationRouter(s, db, repo_root="/repo")
        # Trigger several different suppression paths.
        em_late = r.on_view("")  # no_evidence
        assert em_late.suppression_reason is not None
        assert em_late.suppression_detail
        em_ok = r.on_view("core/target.py")
        assert em_ok.emit is True
        em_dup = r.on_view("core/target.py")  # duplicate or debounce — either way recorded
        assert em_dup.emit is False
        assert em_dup.suppression_reason is not None
        assert em_dup.suppression_detail
