"""Behavior tests for contract_map — the deterministic CONTRACT reader.

Builds a synthetic graph.db matching the real schema (nodes/edges/properties) and
asserts:
  - a function's own contract (signature + raises + guards + return_shape) surfaces;
  - a VERIFIED 1-hop callee's raises surface (the "callee raises X" lever);
  - a name_match callee is NEVER shown (correct-or-quiet, no laundering);
  - render abstains (empty string) when there is no signal.
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.pretask.contract_map import (
    build_contract,
    contract_line,
    render_contract,
)


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT,
            line INTEGER, confidence REAL
        );
        """
    )
    # node 1: validate (edit target) — raises ValueError, has guard, returns value
    # node 2: _check (VERIFIED callee via import) — raises TypeError
    # node 3: walk (name_match callee) — raises OSError, MUST be suppressed
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,signature,return_type,is_test) "
        "VALUES (?,?,?,?,?,?,?,0)",
        [
            (1, "Function", "validate", "app.py", 10, "def validate(data: list) -> bool:", "bool"),
            (2, "Function", "_check", "util.py", 5, "def _check(x):", ""),
            (3, "Function", "walk", "core.py", 20, "def walk(root):", ""),
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) VALUES (?,?,?,?,?)",
        [
            (1, 2, "CALLS", "import", 1.0),       # verified -> _check shown
            # conf 0.9 is ABOVE _NAME_MATCH_FLOOR so it clears _neighbors' visibility
            # filter — suppression must therefore come from the deterministic-method
            # gate (name_match not in _DETERMINISTIC_METHODS), genuinely exercising it.
            (1, 3, "CALLS", "name_match", 0.9),   # name_match -> walk suppressed by the gate
        ],
    )
    conn.executemany(
        "INSERT INTO properties (node_id,kind,value,line,confidence) VALUES (?,?,?,?,1.0)",
        [
            (1, "exception_type", "ValueError", 12),
            (1, "guard_clause", "raise: not data", 11),
            (1, "return_shape", "value", 15),
            (2, "exception_type", "TypeError", 6),
            (3, "exception_type", "OSError", 21),  # on the name_match callee
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "graph.db")
    _make_db(p)
    return p


def test_own_contract_surfaces(db):
    items = build_contract(db, [("app.py", "validate")], include_callees=False)
    assert len(items) == 1
    ev = items[0]
    assert ev.raises == ("ValueError",)
    assert ev.guards == ("raise: not data",)
    assert ev.return_shape == "value"
    assert "validate" in ev.signature


def test_verified_callee_raises_surface(db):
    items = build_contract(db, [("app.py", "validate")], include_callees=True)
    callees = [e for e in items if e.is_callee]
    assert any(e.function == "_check" and e.raises == ("TypeError",) for e in callees)


def test_name_match_callee_suppressed(db):
    # walk is reachable only via a name_match edge — never surface it as a fact.
    items = build_contract(db, [("app.py", "validate")], include_callees=True)
    assert all(e.function != "walk" for e in items)
    block = render_contract(items)
    assert "OSError" not in block
    assert "walk" not in block


def test_render_has_real_content(db):
    block = render_contract(build_contract(db, [("app.py", "validate")]))
    assert block.startswith("<gt-contract>")
    assert "raises: ValueError" in block
    assert "preserve: raise: not data" in block
    assert "TypeError" in block  # the verified callee


def test_correct_or_quiet_on_missing(db):
    # Unknown function -> no node -> empty, never a guess.
    assert build_contract(db, [("app.py", "does_not_exist")]) == []
    assert render_contract([]) == ""


def test_contract_line_inline(db):
    line = contract_line(db, "app.py", ["validate"])
    assert "raises ValueError" in line
    assert "returns value" in line
