"""The seam must actually POPULATE `viewed_symbols`, or the whole chain stays inert.

WHERE THIS SITS. `CanonicalResult.viewed_symbols` exists and the adapter emits `SYMBOL_VIEWED`
from it; `gateway.defined_symbols_for_file` resolves a file to its definitions from `graph.db`.
Both are tested. But the seam that builds `CanonicalResult` never set the field, so in production
it was always `()`, `focused_symbols` stayed empty, and every symbol-subject evidence record stayed
HELD forever at the relevance intersection while `select_covering_tests` stayed starved. A contract
nothing populates changes nothing.

THE AUTHORITY. The viewed FILE comes from the STRUCTURED PROPOSAL SUBJECT, never the raw command
text -- the seam already states this in `normalize_event(viewed_files=...)`: the command "cannot be
trusted for file identity across harnesses (see the cd-prefix blindings that muted post_search)".
The symbols then come from `graph.db`. At no point is rendered output parsed.

WHOLE FILE, NOT A PARSED LINE RANGE -- a deliberate decision, recorded here.
A view of `foo.py` puts every definition in `foo.py` into focus, not only those on the lines
displayed. The seam cannot know the displayed range without parsing the command
(`sed -n '100,140p'`), which is the channel this design rejects. The over-claim is therefore
bounded to FILES THE AGENT ACTUALLY OPENED -- the same granularity `focused_files` already
contributes, not a widening beyond it. Evidence about a symbol defined in a file the agent is
reading is plausibly relevant; that is file-granularity relevance, not invented relevance. The
alternative -- emitting nothing whenever the range is unknown -- leaves five fact classes dead
permanently. `defined_symbols_for_file` still accepts a line window for any operation that can
supply one honestly.
"""

from __future__ import annotations

import sqlite3

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime.reasoning_runtime import ActionOperation


VIEWED = "src/pkg/session.py"


@pytest.fixture()
def graph(tmp_path, monkeypatch):
    """A real graph.db, wired in through the seam's own path accessors."""
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "start_line INTEGER, label TEXT, is_test INTEGER)"
    )
    con.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?)",
        [
            (1, "refresh_session", VIEWED, 10, "Function", 0),
            (2, "TokenStore", VIEWED, 120, "Class", 0),
            (3, "elsewhere", "src/pkg/other.py", 5, "Function", 0),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(seam, "_db_path", lambda: str(db), raising=False)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path), raising=False)
    return str(db)


def test_a_view_resolves_the_files_symbols(graph):
    """THE FIX. The seam turns 'the agent opened this file' into 'these symbols are in play'."""
    assert seam._viewed_symbols_for_action(ActionOperation.VIEW_SOURCE, VIEWED) == (
        "refresh_session", "TokenStore",
    )


def test_only_the_viewed_files_symbols_are_returned(graph):
    """POSITIVE CONTROL on the scope bound: `elsewhere` is resolvable, just not from this view.
    This is what keeps the over-claim confined to files the agent actually opened."""
    got = seam._viewed_symbols_for_action(ActionOperation.VIEW_SOURCE, VIEWED)
    assert "elsewhere" not in got


def test_non_view_operations_resolve_nothing(graph):
    """A search, an edit or a test is not a view. Widening focus on every action would make the
    relevance intersection match nearly anything, which is the failure mode the gate exists to
    prevent."""
    for operation in (
        ActionOperation.SEARCH,
        ActionOperation.EDIT,
        ActionOperation.TEST,
        ActionOperation.SUBMIT,
        ActionOperation.OTHER,
    ):
        assert seam._viewed_symbols_for_action(operation, VIEWED) == (), operation


def test_no_subject_resolves_nothing(graph):
    """Correct-or-quiet: the structured subject is the authority, and without it there is no
    file identity to resolve. The raw command is deliberately never consulted."""
    assert seam._viewed_symbols_for_action(ActionOperation.VIEW_SOURCE, "") == ()


def test_a_missing_graph_is_quiet(tmp_path, monkeypatch):
    """Resolution must never raise into the agent loop, and a missing graph is an honest
    empty -- not an error and not a guess."""
    monkeypatch.setattr(seam, "_db_path", lambda: str(tmp_path / "absent.db"), raising=False)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path), raising=False)
    assert seam._viewed_symbols_for_action(ActionOperation.VIEW_SOURCE, VIEWED) == ()


def test_resolution_failure_cannot_raise(monkeypatch):
    """ANTI-REGRESSION. Every observability path in this seam is best-effort; a graph fault must
    degrade to silence, never break the agent's turn."""
    def _boom():
        raise RuntimeError("graph exploded")

    monkeypatch.setattr(seam, "_db_path", _boom, raising=False)
    assert seam._viewed_symbols_for_action(ActionOperation.VIEW_SOURCE, VIEWED) == ()


def test_the_seam_passes_the_symbols_into_CanonicalResult():
    """THE WIRING ITSELF. The helper is worthless if the construction site ignores it -- that is
    precisely the state this file exists to end (field + adapter + resolver all correct, and
    production still emitting `()` because nobody connected them)."""
    import inspect

    src = inspect.getsource(seam.CanonicalRuntimeAttachment.observe_action_result)
    assert "viewed_symbols=" in src, (
        "CanonicalResult is still built without viewed_symbols -- focused_symbols stays empty "
        "in production and this entire change is inert"
    )
    assert "_viewed_symbols_for_action(" in src, (
        "the construction site does not call the resolver"
    )
