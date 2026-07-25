"""AUDIT — covering-test target lookup was GLOBAL by bare symbol name.

`_covering_tests_for_symbols` resolved edited symbols with

    SELECT id, name, file_path FROM nodes WHERE name IN (...) AND is_test=0 LIMIT 20

matching GLOBALLY by bare name, unscoped to the file the agent actually edited. MEASURED on 8 real
graph.db files: 12% of non-test symbol names occur in more than one file, and the worst offenders
are the most commonly edited ones — `__init__` in 197-371 files, `validate` in 83.

Concretely (cfn-lint-3854, real graph): editing `__init__` matched **238** candidate nodes; LIMIT 20
kept 20 ARBITRARY ones. GT would then execute covering tests for symbols in unrelated files and
attribute the outcome to this edit — wrong evidence, which correct-or-quiet ranks as worse than
silence. Scoping to the edited paths (already known to the caller) leaves **7**.

GT_COVERING_SCOPED is default-off so the change is measurable; it should become the default once one
instrumented run confirms it does not starve selection.
"""
from __future__ import annotations
import os
import sqlite3

import pytest

_REAL = os.path.join(os.path.dirname(__file__), "..", "..",
                     ".tmp_phase0", "aws-cloudformation__cfn-lint-3854", "graph.db")


def _count(con, name, scope=None):
    if scope:
        q = ("SELECT COUNT(*) FROM nodes WHERE name=? AND COALESCE(is_test,0)=0 "
             "AND REPLACE(file_path,'\','/') IN (?)")
        return con.execute(q, (name, scope)).fetchone()[0]
    return con.execute(
        "SELECT COUNT(*) FROM nodes WHERE name=? AND COALESCE(is_test,0)=0",
        (name,)).fetchone()[0]


@pytest.mark.skipif(not os.path.exists(_REAL), reason="real graph.db corpus not present")
def test_scoping_eliminates_wrong_file_targets():
    con = sqlite3.connect(f"file:{os.path.abspath(_REAL)}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT file_path FROM nodes WHERE name='__init__' "
                          "AND COALESCE(is_test,0)=0 LIMIT 1").fetchone()
        assert row, "corpus lacks the ambiguous symbol this test is about"
        edited = row[0].replace("\\", "/")
        unscoped = _count(con, "__init__")
        scoped = _count(con, "__init__", edited)
        assert unscoped > 20, (
            f"expected an ambiguous symbol (>LIMIT 20) to demonstrate the defect, got {unscoped}")
        assert scoped < unscoped, "scoping did not reduce the target set"
        assert scoped >= 1, "scoping must not eliminate the genuine target"
    finally:
        con.close()


def test_flag_defaults_off(monkeypatch):
    """Byte-identical unless explicitly enabled."""
    monkeypatch.delenv("GT_COVERING_SCOPED", raising=False)
    assert os.environ.get("GT_COVERING_SCOPED", "0").strip() != "1"


def test_scoping_is_read_from_the_edited_rels_source():
    """The scope must come from what the agent edited, never from a heuristic."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "gt_mini_patch.py"),
               encoding="utf-8").read()
    i = src.index("def _covering_tests_for_symbols(")
    body = src[i:i + 3000]
    assert "_oracle_edited_rels" in body, \
        "target scoping must derive from the agent's own edited paths"
    assert "GT_COVERING_SCOPED" in body
