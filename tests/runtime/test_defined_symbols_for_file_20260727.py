"""Resolving a viewed file to its symbols is a GRAPH query, never a text parse.

WHY THIS FUNCTION EXISTS. `CanonicalResult.viewed_symbols` is the input that finally populates
`work_state.focused_symbols`, which is permanently empty on a shell harness and which two
independent mechanisms depend on: the relevance intersection in `evaluate_feature_contract`, and
`select_covering_tests`, which fail-closes on an empty symbol set.

WHERE THE ANSWER MAY COME FROM. Not from the command string, and not from the rendered output.
Recovering "which symbol did this show" from `sed -n '100,140p' foo.py` is semantic reading, and GT
is LLM-free and deterministic by mandate. The authority is `graph.db`: the file the operation
actually read, resolved to the definitions the graph says live there. Same source, same
`_DEF_LABELS`, same read-only connection as every other gateway graph query.

THE LINE-RANGE CASE MATTERS. A view of lines 100-140 shows the symbols defined in that window, not
every symbol in the file. Claiming the whole file would put symbols into focus that the agent never
saw, which then admit unrelated evidence through the relevance gate -- inventing relevance is worse
than having none, because it converts a silent feature into a wrong one.

CORRECT-OR-QUIET. No database, no schema, no rows, a bad range, any sqlite error -> `()`. An empty
result is honest; a guessed symbol is not.
"""

from __future__ import annotations

import sqlite3

import pytest

from groundtruth.runtime.gateway import defined_symbols_for_file


@pytest.fixture()
def graph_db(tmp_path):
    """A minimal graph.db with the columns the real query reads."""
    path = tmp_path / "graph.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "start_line INTEGER, label TEXT, is_test INTEGER)"
    )
    rows = [
        (1, "refresh_session", "src/pkg/session.py", 10, "Function", 0),
        (2, "TokenStore", "src/pkg/session.py", 120, "Class", 0),
        (3, "helper", "src/pkg/other.py", 5, "Function", 0),
        (4, "test_refresh", "tests/test_session.py", 3, "Function", 1),
        (5, "SOME_CONST", "src/pkg/session.py", 4, "Variable", 0),
    ]
    con.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return str(path)


def test_returns_the_definitions_the_graph_places_in_that_file(graph_db):
    """THE FIX. Authority is the graph, and only the requested file's definitions come back."""
    assert defined_symbols_for_file(graph_db, "src/pkg/session.py") == (
        "refresh_session", "TokenStore",
    )


def test_other_files_are_never_included(graph_db):
    """POSITIVE CONTROL on the filter: `helper` exists and is findable, just not here."""
    assert defined_symbols_for_file(graph_db, "src/pkg/other.py") == ("helper",)
    assert "helper" not in defined_symbols_for_file(graph_db, "src/pkg/session.py")


def test_test_definitions_are_excluded(graph_db):
    """Test symbols are not production surface; the existing graph queries all exclude them
    via COALESCE(is_test,0)=0 and this must not diverge."""
    assert defined_symbols_for_file(graph_db, "tests/test_session.py") == ()


def test_non_definition_labels_are_excluded(graph_db):
    """Only `_DEF_LABELS` are definitions. `SOME_CONST` is a Variable in the same file and must
    not enter focus -- focus drives an intersection, so every extra member widens what evidence
    is admitted."""
    assert "SOME_CONST" not in defined_symbols_for_file(graph_db, "src/pkg/session.py")


def test_a_line_range_narrows_to_what_was_actually_shown(graph_db):
    """THE POINT OF THE RANGE. A view of lines 1-50 did not show `TokenStore` at line 120.
    Putting it in focus would claim the agent saw something it did not."""
    assert defined_symbols_for_file(
        graph_db, "src/pkg/session.py", start_line=1, end_line=50
    ) == ("refresh_session",)
    assert defined_symbols_for_file(
        graph_db, "src/pkg/session.py", start_line=100, end_line=200
    ) == ("TokenStore",)


def test_no_range_means_the_whole_file(graph_db):
    """ANTI-WEAKENING: the range is optional, and its absence must not silently return ()."""
    assert len(defined_symbols_for_file(graph_db, "src/pkg/session.py")) == 2


def test_missing_database_is_quiet_not_an_error(tmp_path):
    """CORRECT-OR-QUIET. Telemetry-adjacent resolution must never raise into the agent loop."""
    assert defined_symbols_for_file(str(tmp_path / "nope.db"), "src/pkg/session.py") == ()
    assert defined_symbols_for_file("", "src/pkg/session.py") == ()


def test_a_schemaless_database_is_quiet(tmp_path):
    """A real graph.db from an older build may lack the columns; that is not a crash."""
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert defined_symbols_for_file(str(path), "src/pkg/session.py") == ()


def test_unknown_file_returns_nothing(graph_db):
    """The honest empty: the graph has no definitions there."""
    assert defined_symbols_for_file(graph_db, "src/pkg/does_not_exist.py") == ()


def test_a_missing_database_is_never_CREATED(tmp_path):
    """ADDED BECAUSE A MUTATION SURVIVED (behavioural half).

    `sqlite3.connect(path)` CREATES the file when it does not exist. If the resolver ever opened
    the graph read-write without the isfile guard, a per-attempt code path would silently
    materialise an empty `graph.db` where repository truth is supposed to live -- and every
    later reader would see a valid, empty graph rather than a missing one. That is the
    correct-or-quiet failure turned into a false negative.
    """
    missing = tmp_path / "not_there.db"
    assert defined_symbols_for_file(str(missing), "src/pkg/session.py") == ()
    assert not missing.exists(), "the resolver created a database that did not exist"


def test_the_resolver_uses_the_read_only_connection_helper():
    """ADDED BECAUSE A MUTATION SURVIVED (structural half), and deliberately structural.

    Swapping `_connect_ro` for a plain `sqlite3.connect` leaves every behavioural assertion
    green: a SELECT writes nothing, so no observable difference exists from outside. The
    invariant is still real -- `graph.db` is SHARED repository truth, a read-write handle from a
    per-attempt path can emit WAL/journal side-files next to it, and any future statement added
    to this function would silently gain write authority. `_connect_ro` also carries the
    `PRAGMA query_only=1` fallback for the case where the ro-URI open fails.

    When behaviour cannot distinguish right from wrong, pinning the mechanism is the honest
    test; pretending a byte-comparison covers it would be worse than admitting the limit.
    """
    import inspect

    from groundtruth.runtime import gateway

    src = inspect.getsource(gateway.defined_symbols_for_file)
    assert "_connect_ro(" in src, (
        "the resolver no longer opens graph.db through the read-only helper"
    )
    assert "sqlite3.connect(" not in src, (
        "a direct read-write sqlite3.connect appeared in the resolver"
    )


def test_the_graph_is_never_written(graph_db):
    """The graph is authoritative repository truth and must be opened read-only -- the same
    invariant `_connect_ro` exists to enforce. A resolver that could mutate it would corrupt
    shared truth from a per-attempt code path."""
    before = open(graph_db, "rb").read()
    defined_symbols_for_file(graph_db, "src/pkg/session.py")
    assert open(graph_db, "rb").read() == before
