"""Layer 3 router timing tests for ``CollaborationRouter.on_edit``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.router import CollaborationRouter, EmissionKind, SuppressionReason
from groundtruth.state.agent_state import AgentState


def _build_db(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "core").mkdir(parents=True)
    (repo / "users").mkdir(parents=True)
    (repo / "core" / "target.py").write_text(
        "def target(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (repo / "users" / "foo.py").write_text(
        "from core.target import target\n"
        "def caller():\n"
        "    return target(1, 2)\n",
        encoding="utf-8",
    )
    (repo / "isolated.py").write_text(
        "def lonely():\n    return 1\n",
        encoding="utf-8",
    )
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
        (1, "Function", "target", "", "core/target.py", 1, 2, "def target(a, b)", "int", 1, 0, "python", 0),
        (2, "Function", "caller", "", "users/foo.py", 2, 3, "def caller()", None, 1, 0, "python", 0),
        (3, "Function", "lonely", "", "isolated.py", 1, 2, "def lonely()", None, 1, 0, "python", 0),
    ]
    con.executemany(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", nodes,
    )
    # caller -> target with line number 3
    con.execute(
        "INSERT INTO edges (source_id, target_id, type, source_line, confidence) VALUES (?, ?, ?, ?, ?)",
        (2, 1, "CALLS", 3, 1.0),
    )
    con.commit()
    con.close()
    return repo, str(db)


def _new_state(repo: Path, task: str = "t", max_iter: int = 100) -> AgentState:
    return AgentState.create(task_id=task, max_iterations=max_iter, repo_root=str(repo))


class TestEditWithCallerEmits:
    def test_edit_with_caller_contract_emits(self, tmp_path: Path) -> None:
        repo, db = _build_db(tmp_path)
        s = _new_state(repo)
        r = CollaborationRouter(s, db, repo_root=str(repo))
        em = r.on_edit(str(repo / "core" / "target.py"), ["target"])
        assert em.emit is True
        assert em.kind == EmissionKind.ON_EDIT_CONTRACT
        assert em.next_action_type in ("READ_CALLER_CONTRACT", "CHECK_SIGNATURE")
        # Caller-code evidence item should be present.
        kinds = {it["kind"] for it in em.evidence_items}
        assert "caller_code" in kinds


class TestEditWithoutUsefulContract:
    def test_edit_with_no_caller_or_contract_stays_silent(self, tmp_path: Path) -> None:
        repo, db = _build_db(tmp_path)
        # isolated.py has no callers, but it does have a contract — strip it.
        # Easiest: edit a function that doesn't exist in the graph at all.
        s = _new_state(repo)
        r = CollaborationRouter(s, db, repo_root=str(repo))
        em = r.on_edit(str(repo / "isolated.py"), ["does_not_exist"])
        assert em.emit is False
        # All providers were empty -> NO_EVIDENCE.
        assert em.suppression_reason == SuppressionReason.NO_EVIDENCE


class TestEditNoFunctionTarget:
    def test_empty_function_list_no_evidence(self, tmp_path: Path) -> None:
        repo, db = _build_db(tmp_path)
        s = _new_state(repo)
        r = CollaborationRouter(s, db, repo_root=str(repo))
        em = r.on_edit(str(repo / "core" / "target.py"), [])
        assert em.emit is False
        assert em.suppression_reason == SuppressionReason.NO_EVIDENCE
        assert em.suppression_detail == "no_function_target"


class TestEditDuplicateSuppresses:
    def test_same_edit_target_dedups(self, tmp_path: Path) -> None:
        repo, db = _build_db(tmp_path)
        s = _new_state(repo)
        r = CollaborationRouter(s, db, repo_root=str(repo))
        em1 = r.on_edit(str(repo / "core" / "target.py"), ["target"])
        assert em1.emit is True
        # Past debounce window.
        s.set_iteration(em1.iteration + r.debounce_iters + 1)
        em2 = r.on_edit(str(repo / "core" / "target.py"), ["target"])
        assert em2.emit is False
        assert em2.suppression_reason == SuppressionReason.DUPLICATE


class TestEditBudgetSuppresses:
    def test_edit_emissions_bypass_total_budget(self, tmp_path: Path) -> None:
        repo, db = _build_db(tmp_path)
        s = _new_state(repo)
        r = CollaborationRouter(s, db, repo_root=str(repo), total_budget=0)
        em1 = r.on_edit(str(repo / "core" / "target.py"), ["target"])
        assert em1.emit is True


class TestPendingSuggestionTTLRespected:
    def test_pending_suggestion_classified_followed(self, tmp_path: Path) -> None:
        """When the router emits a next_action and the agent reads that file,
        the AgentState pending-suggestion lifecycle marks it FOLLOWED_EXACT
        (verified end-to-end across Layer 2 + Layer 3).
        """
        repo, db = _build_db(tmp_path)
        s = _new_state(repo)
        r = CollaborationRouter(s, db, repo_root=str(repo))
        em = r.on_edit(str(repo / "core" / "target.py"), ["target"])
        assert em.emit is True
        assert em.next_action_file
        # Register the router's suggestion in AgentState (mimics what the
        # wrapper does today).
        sug = s.register_pending_suggestion(
            event_id="ev1",
            next_action_type=em.next_action_type,
            next_action_file=em.next_action_file,
            ttl_actions=3,
        )
        assert sug is not None
        # Agent reads the suggested file -> FOLLOWED_EXACT.
        s.process_agent_action(action_file=em.next_action_file)
        assert sug.followed is True

    def test_pending_suggestion_expires_as_ignored(self, tmp_path: Path) -> None:
        repo, db = _build_db(tmp_path)
        s = _new_state(repo)
        r = CollaborationRouter(s, db, repo_root=str(repo))
        em = r.on_edit(str(repo / "core" / "target.py"), ["target"])
        assert em.emit is True
        sug = s.register_pending_suggestion(
            event_id="ev1",
            next_action_type=em.next_action_type,
            next_action_file=em.next_action_file,
            ttl_actions=3,
        )
        assert sug is not None
        # Three unrelated actions -> ignored.
        for _ in range(3):
            s.process_agent_action(action_file="some/other.py")
        assert sug.expired
        assert sug.followed is False


class TestRouterBudgetSemantics:
    def test_total_budget_caps_views_but_not_edits(self, tmp_path: Path) -> None:
        repo, db = _build_db(tmp_path)
        s = _new_state(repo)
        r = CollaborationRouter(s, db, repo_root=str(repo), total_budget=1)
        emissions = []
        for i, (kind, target, fn) in enumerate(
            [
                ("view", "core/target.py", None),
                ("edit", "core/target.py", "target"),
                ("view", "users/foo.py", None),
                ("edit", "users/foo.py", "caller"),
            ]
        ):
            s.set_iteration((i + 1) * (r.debounce_iters + 1))
            if kind == "view":
                em = r.on_view(target)
            else:
                em = r.on_edit(target, [fn] if fn else [])
            emissions.append(em.emit)
        assert emissions == [True, True, False, True]


class TestSuppressionReasonsRecorded:
    def test_every_suppressed_emission_carries_reason(self, tmp_path: Path) -> None:
        repo, db = _build_db(tmp_path)
        s = _new_state(repo)
        r = CollaborationRouter(s, db, repo_root=str(repo))
        em_empty = r.on_edit("", ["target"])
        assert em_empty.suppression_reason == SuppressionReason.NO_EVIDENCE
        assert em_empty.suppression_detail == "empty_path"
        em_no_fn = r.on_edit(str(repo / "core" / "target.py"), [])
        assert em_no_fn.suppression_reason == SuppressionReason.NO_EVIDENCE
        assert em_no_fn.suppression_detail == "no_function_target"
