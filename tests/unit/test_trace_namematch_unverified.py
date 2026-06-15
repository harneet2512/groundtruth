"""L4 honesty: handle_trace / handle_impact must NOT launder a name_match
caller as a verified fact.

`ImportGraph.find_callers` BFS fallback gates only on confidence>=0.5, which
admits a name_match@0.6 GUESS (a caller matched only by name across N
same-named symbols — The Distracting Effect, arXiv:2505.06914). These edges
must reach the agent flagged `(unverified)` / `verified=False`, never as bare
facts. A truly verified edge (resolution_method=import/same_file/...) must
render unchanged.

The fixture builds a Go-indexer-schema graph.db WITHOUT a closure table so the
live BFS path (the one that carries resolution_method) runs — exactly the
production code path the GraphStore bridge uses.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.index.graph import ImportGraph
from groundtruth.index.graph_store import GraphStore
from groundtruth.mcp.tools import handle_impact, handle_trace
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok


def _make_mixed_caller_db(path: str) -> None:
    """Go-schema graph.db, NO closure table (forces the BFS path).

    Call graph (CALLS edges into target()):
        verified_caller()  --import (conf 1.0)-->     target()   FACT
        guess_caller()     --name_match (conf 0.6)--> target()   GUESS

    node 1 = target          src/target.py
    node 2 = verified_caller src/verified.py   (import-resolved -> FACT)
    node 3 = guess_caller    src/guess.py      (name_match -> unverified)
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "is_exported, is_test, language) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, "Function", "target", "src/target.py", 1, 10, 1, 0, "python"),
            (2, "Function", "verified_caller", "src/verified.py", 1, 10, 1, 0, "python"),
            (3, "Function", "guess_caller", "src/guess.py", 1, 10, 1, 0, "python"),
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence) VALUES (?,?,?,?,?,?,?)",
        [
            # FACT: import-verified (>=0.5 so the BFS confidence gate admits it too)
            (2, 1, "CALLS", 5, "src/verified.py", "import", 1.0),
            # GUESS: name_match@0.6 — admitted by the conf>=0.5 BFS gate, must be flagged
            (3, 1, "CALLS", 7, "src/guess.py", "name_match", 0.6),
        ],
    )
    conn.commit()
    conn.close()


def _open(path: str) -> GraphStore:
    store = GraphStore(db_path=path)
    res = store.initialize()
    assert isinstance(res, Ok), res
    return store


def test_find_callers_marks_namematch_unverified(tmp_path: Path) -> None:
    """Graph layer: the name_match caller carries is_fact=False; the import
    caller carries is_fact=True. (BFS path, no closure table.)"""
    db = str(tmp_path / "mixed.db")
    _make_mixed_caller_db(db)
    graph = ImportGraph(_open(db))

    result = graph.find_callers("target")
    assert isinstance(result, Ok)
    by_file = {r.file_path: r for r in result.value}
    assert by_file["src/verified.py"].is_fact is True
    assert by_file["src/guess.py"].is_fact is False


@pytest.mark.asyncio
async def test_handle_trace_renders_namematch_unverified(tmp_path: Path) -> None:
    """handle_trace must render the name_match caller as `(unverified)` /
    verified=False, and the import caller as verified=True (not a bare fact)."""
    db = str(tmp_path / "mixed.db")
    _make_mixed_caller_db(db)
    store = _open(db)
    graph = ImportGraph(store)
    tracker = InterventionTracker(store)

    result = await handle_trace(
        symbol="target",
        store=store,
        graph=graph,
        tracker=tracker,
        direction="callers",
    )

    callers = {c["file"]: c for c in result["callers"]}
    assert "src/verified.py" in callers
    assert "src/guess.py" in callers

    # FACT caller: verified, no (unverified) marker.
    assert callers["src/verified.py"]["verified"] is True
    assert "(unverified)" not in callers["src/verified.py"].get("context", "")

    # GUESS caller: NOT laundered as a fact.
    assert callers["src/guess.py"]["verified"] is False
    assert "(unverified)" in callers["src/guess.py"].get("context", "")


@pytest.mark.asyncio
async def test_handle_impact_namematch_break_risk_unverified(tmp_path: Path) -> None:
    """handle_impact must not stamp a confident break_risk on a name_match
    GUESS: the name_match caller is `verified=False` with break_risk UNVERIFIED,
    while the verified caller keeps a real risk label."""
    db = str(tmp_path / "mixed.db")
    _make_mixed_caller_db(db)
    store = _open(db)
    graph = ImportGraph(store)
    tracker = InterventionTracker(store)

    result = await handle_impact(
        symbol="target",
        store=store,
        graph=graph,
        tracker=tracker,
        root_path=str(tmp_path),  # no real source files -> usage-line read is empty
    )

    callers = {c["file"]: c for c in result["direct_callers"]}
    assert callers["src/verified.py"]["verified"] is True
    assert callers["src/guess.py"]["verified"] is False
    assert callers["src/guess.py"]["break_risk"] == "UNVERIFIED"
    assert callers["src/guess.py"]["call_style"] == "unverified"
