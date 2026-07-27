"""`covering_red` is starved by an empty `focused_symbols` — upstream of every gate.

THE SECOND, INDEPENDENT KILL. The relevance intersection at `evaluate_feature_contract` holds
symbol-subject evidence forever, but `covering_red` never even reaches it. The seam selects its
covering tests with::

    symbols = set(self.attempt_runtime.work_state.focused_symbols)   # gt_mini_patch.py
    selected = select_covering_tests(_db_path(), symbols, repo_root=_root())

and `select_covering_tests` fail-closes on an empty symbol set (`covering_runner.py`,
``if not syms ... return []``). With `focused_symbols` permanently empty, `selected` is always
``[]``, `files` is always empty, and the whole covering execution block is skipped. No producer
runs, so there is nothing for any gate to hold: the feature is dark for a reason that has nothing
to do with the oracle, the phase policy, or delivery.

WHAT THIS FILE PROVES. That the starvation is real (empty focus -> no covering tests, with a
positive control showing the SAME graph yields tests when focus is populated), and therefore that
populating `focused_symbols` is sufficient to un-starve it — with NO covering-specific special
case anywhere. If a later change makes this pass for some other reason, the negative arm below
fails and says so.

WHY A GRAPH FIXTURE RATHER THAN A MOCK. The selection is a real SQL query over `nodes`/`edges`
with a FACT-tier resolution-method filter and a confidence floor. A mocked selector would prove
that a mock returns what it was told to return -- exactly the kind of vacuous green that has
cost this project days.
"""

from __future__ import annotations

import sqlite3

import pytest

from groundtruth.runtime.covering_runner import select_covering_tests


@pytest.fixture()
def graph_with_covering_test(tmp_path):
    """A graph where `tests/test_session.py` CALLS `refresh_session` via a FACT-tier edge."""
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "start_line INTEGER, label TEXT, is_test INTEGER)"
    )
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, resolution_method TEXT, confidence REAL)"
    )
    con.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?)",
        [
            (1, "refresh_session", "src/pkg/session.py", 10, "Function", 0),
            (2, "test_refresh", "tests/test_session.py", 3, "Function", 1),
        ],
    )
    # test_refresh -> refresh_session, deterministic method, above the 0.7 confidence floor.
    con.execute(
        "INSERT INTO edges VALUES (1, 2, 1, 'CALLS', 'import', 0.95)"
    )
    con.commit()
    con.close()
    return str(db)


def test_empty_focus_selects_no_covering_tests(graph_with_covering_test):
    """THE STARVATION, stated exactly as production hits it. This is what an empty
    `focused_symbols` does, and it happens before any gate can weigh in."""
    assert select_covering_tests(graph_with_covering_test, set()) == []


def test_the_same_graph_yields_a_covering_test_once_focus_is_populated(
    graph_with_covering_test,
):
    """POSITIVE CONTROL — and the whole point.

    Identical database, identical query, identical everything except the symbol set. If this
    returned [] as well, the negative above would be uninformative: it would prove only that
    the fixture has no covering tests, not that focus is the blocker.
    """
    selected = select_covering_tests(graph_with_covering_test, {"refresh_session"})
    assert selected, (
        "the covering selection is empty even WITH focus -- the fixture or the FACT-tier "
        "filter changed, and the negative arm above proves nothing until this passes"
    )
    assert selected[0]["file"] == "tests/test_session.py"


def test_selection_is_leak_safe(graph_with_covering_test):
    """The selector must never surface a test NAME -- it groups by file_path precisely so no
    internal identifier can reach a renderer. `leak == 0` is a hard product invariant."""
    selected = select_covering_tests(graph_with_covering_test, {"refresh_session"})
    for row in selected:
        assert set(row) <= {"file", "confidence"}, row
        assert "test_refresh" not in str(row), "a test identifier escaped the selector"


def test_an_unrelated_symbol_selects_nothing(graph_with_covering_test):
    """ANTI-WEAKENING. Populating focus must not make the selector indiscriminate: only symbols
    a test actually reaches may select it. Otherwise un-starving covering_red would trade
    silence for noise."""
    assert select_covering_tests(graph_with_covering_test, {"unrelated_symbol"}) == []


def test_the_seam_still_reads_focus_for_its_symbol_set():
    """PINS THE DEPENDENCY THIS FILE IS ABOUT.

    If the seam ever stops sourcing its covering symbols from `focused_symbols`, the reasoning
    here is obsolete and the un-starving argument no longer holds -- better to fail loudly than
    to keep asserting a chain that has quietly been re-routed.
    """
    import inspect

    from artifact_deepswe import gt_mini_patch as seam

    src = inspect.getsource(seam.CanonicalRuntimeAttachment)
    assert "work_state.focused_symbols" in src, (
        "the seam no longer derives covering symbols from focused_symbols"
    )
    assert "select_covering_tests(" in src
