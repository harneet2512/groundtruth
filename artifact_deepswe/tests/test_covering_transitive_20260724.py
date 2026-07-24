"""AUDIT 2026-07-24 — TRANSITIVE COVERING.

Covering selection required a DIRECT ``test -> symbol`` CALLS edge, but a test almost never calls
an edited leaf helper directly — it calls a PUBLIC API that reaches it. Coverage is TRANSITIVE, so
a 1-hop query structurally cannot see it. MEASURED: selection empty on 28/29 tasks (SS-2 diagnosis)
and 19 ``plan_none`` rows in run 30121930273 => ``covering_red`` could never deliver.

Over-selection is SAFE here (unlike an evidence claim): the selected test is then EXECUTED and only
a genuine RED is ever reported. Execution is the ground truth; the graph is only the shortlist.
"""
from __future__ import annotations

import sqlite3

import gt_mini_patch as g

_TEST_SRC = "def test_api():\n    assert True\n"
_CORE_SRC = "def leaf_helper():\n    return 1\n"


def _graph(tmp_path, outer_method: str = "lsp"):
    """test_api() -> public_api() -> leaf_helper()  (agent edits leaf_helper; NO direct edge)."""
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, name TEXT, file_path TEXT,"
        " is_test INTEGER, label TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, resolution_method TEXT, confidence REAL, source_line INTEGER,"
        " source_file TEXT, metadata TEXT);")
    con.execute("INSERT INTO nodes VALUES(1,'leaf_helper','src/core.py',0,'Function',1,5,'','python')")
    con.execute("INSERT INTO nodes VALUES(2,'public_api','src/api.py',0,'Function',1,5,'','python')")
    con.execute("INSERT INTO nodes VALUES(3,'test_api','tests/test_api.py',1,'Function',1,5,'','python')")
    con.execute("INSERT INTO edges VALUES(1,2,1,'CALLS','lsp',0.95,1,'src/api.py','')")
    con.execute(
        "INSERT INTO edges VALUES(2,3,2,'CALLS',?,0.95,1,'tests/test_api.py','')", (outer_method,))
    con.commit()
    con.close()
    # the runner-eligibility filter drops PHANTOM tests (graph nodes with no file on disk),
    # so the fixture must materialize the files that selection is checked against.
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text(_TEST_SRC, encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "core.py").write_text(_CORE_SRC, encoding="utf-8")
    return db


def test_one_hop_finds_nothing_but_transitive_finds_the_test(tmp_path, monkeypatch):
    db = _graph(tmp_path)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    # OFF (default) -> 1-hop finds nothing: this IS the 28/29 `plan_none` reality (byte-identical)
    monkeypatch.delenv("GT_COVERING_TRANSITIVE", raising=False)
    assert g._covering_tests_for_symbols({"leaf_helper"}) == [], \
        "1-hop must still find nothing when the flag is off (byte-identical)"
    # ON -> the bounded 2-hop fallback finds the test that reaches leaf_helper via public_api
    monkeypatch.setenv("GT_COVERING_TRANSITIVE", "1")
    got = g._covering_tests_for_symbols({"leaf_helper"})
    assert got, "REGRESSION: transitive covering found no test for an indirectly-covered symbol"
    assert any("test_api" in str(r) for r in got), f"wrong test selected: {got}"


def test_transitive_keeps_the_deterministic_edge_gate(tmp_path, monkeypatch):
    """Gates preserved: a FUZZY outer edge must never yield a covering test."""
    db = _graph(tmp_path, outer_method="heuristic")
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_COVERING_TRANSITIVE", "1")
    assert g._covering_tests_for_symbols({"leaf_helper"}) == [], \
        "a non-deterministic edge must NOT yield a covering test"
