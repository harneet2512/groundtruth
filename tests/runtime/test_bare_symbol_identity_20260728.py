"""C4: repository-qualified symbol identity must survive the runtime pipeline.

A bare name is not a repository identity.  If ``helper`` is defined in both
``src/a.py`` and ``src/b.py``, admitting the bare string into focus makes the
active-decision neighborhood relevant to both files and makes covering
selection run tests for both implementations.

The safe representation remains a string for compatibility with canonical
event metadata, but the string is qualified as ``repo/path.py::symbol``.
Consumers that genuinely require bare names must project them explicitly;
that projection must not become the lookup key that establishes graph identity.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import covering_runner
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe
from groundtruth.runtime.gateway import ToolEvent


A_FILE = "src/a.py"
B_FILE = "src/b.py"
A_IDENTITY = f"{A_FILE}::helper"
B_IDENTITY = f"{B_FILE}::helper"
UNIQUE_FILE = "src/c.py"
UNIQUE_IDENTITY = f"{UNIQUE_FILE}::render"

REVISION = rr.RevisionVector(
    repository_content="repo-c4",
    graph="graph-c4",
    lsp="lsp-c4",
    runtime_evidence="runtime-c4",
)


def _graph(tmp_path: Path) -> str:
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY,
          name TEXT,
          file_path TEXT,
          start_line INTEGER,
          label TEXT,
          is_test INTEGER
        );
        CREATE TABLE edges (
          id INTEGER PRIMARY KEY,
          source_id INTEGER,
          target_id INTEGER,
          type TEXT,
          resolution_method TEXT,
          confidence REAL
        );
        """
    )
    con.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?)",
        [
            (1, "helper", A_FILE, 10, "Function", 0),
            (2, "helper", B_FILE, 20, "Function", 0),
            (3, "test_a_behavior", "tests/test_a.py", 3, "Function", 1),
            (4, "test_b_behavior", "tests/test_b.py", 4, "Function", 1),
            (5, "render", UNIQUE_FILE, 30, "Function", 0),
        ],
    )
    con.executemany(
        "INSERT INTO edges VALUES (?,?,?,?,?,?)",
        [
            (1, 3, 1, "CALLS", "import", 1.0),
            (2, 4, 2, "CALLS", "import", 1.0),
        ],
    )
    con.commit()
    con.close()
    return str(db)


@pytest.fixture()
def graph_db(tmp_path, monkeypatch) -> str:
    db = _graph(tmp_path)
    monkeypatch.setattr(seam, "_db_path", lambda: db, raising=False)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path), raising=False)
    return db


def _decision(*symbols: str) -> rr.ActiveDecision:
    state = dataclasses.replace(
        rr.WorkState.initial(attempt_id="attempt-c4", revision=REVISION),
        focused_symbols=tuple(symbols),
    )
    return seam.CanonicalRuntimeAttachment._active_decision(
        (), state, REVISION, ()
    )


def test_unqualified_search_abstains_when_two_files_define_the_name(graph_db):
    """The command supplies no file identity, so two definition homes are ambiguous."""
    assert seam._resolved_search_symbols(
        rr.ActionOperation.SEARCH, "rg helper ."
    ) == ()


def test_viewed_symbol_keeps_the_viewed_file_as_part_of_its_identity(graph_db):
    """A structured file-view subject provides exact identity; discarding it is lossy."""
    assert seam._viewed_symbols_for_action(
        rr.ActionOperation.VIEW_SOURCE, A_FILE
    ) == (A_IDENTITY,)


def test_qualified_result_survives_adapter_and_reducer_unchanged():
    action = rr.CanonicalAction(
        action_id="action-c4",
        operation=rr.ActionOperation.VIEW_SOURCE,
        tool_family="shell",
        tool_name="mini-swe",
        structured_operation="view",
        subject=A_FILE,
    )
    proposal = miniswe.canonicalize_action_proposal(
        action,
        event_id="event-c4-proposal",
        attempt_id="attempt-c4",
        sequence=1,
        model_turn_id="turn-c4",
        observation_id="observation-c4",
        revision=REVISION,
        previous_event_hash="",
    )
    result = miniswe.canonicalize_tool_result(
        ToolEvent(
            kind="view",
            carrier_kind="view",
            command=f"sed -n '1,80p' {A_FILE}",
            output="def helper(): pass",
            exit_status=0,
            semantic_events=(),
            semantics_authoritative=True,
        ),
        proposal=proposal,
        result=rr.CanonicalResult(
            status="success",
            viewed_symbols=(A_IDENTITY,),
        ),
        event_id="event-c4-result",
        sequence=2,
        observation_id="observation-c4",
        revision_after=REVISION,
        previous_event_hash=proposal.content_hash,
    )
    state = rr.WorkState.initial(attempt_id="attempt-c4", revision=REVISION)
    state = rr.reduce_event(rr.reduce_event(state, proposal), result)
    assert state.focused_symbols == (A_IDENTITY,)
    assert "helper" not in state.focused_symbols


def test_qualified_focus_admits_only_its_own_definition_home(graph_db):
    decision = _decision(A_IDENTITY)
    assert f"subject:{A_FILE}" in decision.causal_neighborhood
    assert f"subject:{B_FILE}" not in decision.causal_neighborhood
    assert "subject:helper" not in decision.causal_neighborhood


def test_unique_qualified_focus_retains_safe_bare_subject_compatibility(graph_db):
    decision = _decision(UNIQUE_IDENTITY)
    assert f"subject:{UNIQUE_FILE}" in decision.causal_neighborhood
    assert "subject:render" in decision.causal_neighborhood


def test_bare_projection_is_explicit_and_deterministic():
    project = getattr(covering_runner, "project_bare_symbol_names", None)
    assert callable(project), (
        "covering consumers need an explicit qualified-to-bare projection; "
        "inline string stripping makes identity loss invisible"
    )
    assert project(
        (A_IDENTITY, B_IDENTITY, "src/c.py::render")
    ) == ("helper", "render")


def test_qualified_covering_selection_reaches_only_the_matching_target(graph_db):
    selected = covering_runner.select_covering_tests(graph_db, {A_IDENTITY})
    assert selected == [{"file": "tests/test_a.py", "confidence": 1.0}]


def test_bare_covering_selection_abstains_on_two_definition_homes(graph_db):
    """Projecting to ``helper`` cannot authorize tests for both unrelated targets."""
    assert covering_runner.select_covering_tests(graph_db, {"helper"}) == []
