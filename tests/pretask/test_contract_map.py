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
    edit_target_callee_contracts,
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


# ── item #17: callee sig/line/props must come from the node the verified edge
#    actually resolved to, NOT the lowest-line node over the same-name union ──


def _make_overload_db(path: str) -> None:
    """One file with TWO same-name callees (overloads / same-name methods on two
    classes). The verified edge from the edit target resolves to the SECOND
    (higher-line) one. The old lowest-line-over-union readers would emit the FIRST
    overload's signature/line/props — the wrong-node defect (item #17).
    """
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
    # id 1: edit target `run`
    # id 4: overload A `handle` (LOWER line 10) — the WRONG node (lowest-line trap)
    # id 5: overload B `handle` (HIGHER line 90) — the node the edge resolved to
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,signature,return_type,is_test) "
        "VALUES (?,?,?,?,?,?,?,0)",
        [
            (1, "Method", "run", "svc.py", 200, "def run(self):", ""),
            (4, "Method", "handle", "h.py", 10, "def handle(self, a: int) -> None:", "None"),
            (5, "Method", "handle", "h.py", 90, "def handle(self, a: str, b: dict) -> bool:", "bool"),
        ],
    )
    # The VERIFIED edge resolves run -> handle(id 5), the higher-line overload.
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) VALUES (?,?,?,?,?)",
        [
            (1, 5, "CALLS", "import", 1.0),
        ],
    )
    # Distinct behavioral props per overload, so a union-merge is detectable:
    #   overload A (id 4): raises KeyError    (must NOT appear — not the resolved node)
    #   overload B (id 5): raises RuntimeError (the resolved node's real contract)
    conn.executemany(
        "INSERT INTO properties (node_id,kind,value,line,confidence) VALUES (?,?,?,?,1.0)",
        [
            (4, "exception_type", "KeyError", 11),
            (5, "exception_type", "RuntimeError", 91),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def overload_db(tmp_path):
    p = str(tmp_path / "overload.db")
    _make_overload_db(p)
    return p


def test_callee_contract_uses_resolved_overload_sig_line(overload_db):
    """edit_target_callee_contracts must report the RESOLVED overload's signature
    and start_line (id 5 @ line 90), never the lowest-line homonym (id 4 @ line 10)."""
    callees = edit_target_callee_contracts(overload_db, "svc.py", ["run"])
    assert len(callees) == 1
    cc = callees[0]
    assert cc.callee == "handle"
    # The resolved overload (id 5) — its signature has TWO params + bool return.
    assert "b: dict" in cc.signature
    assert "-> bool" in cc.signature
    # ...and its line, not the lowest-line homonym's line 10.
    assert cc.line == 90
    # The wrong overload's distinctive signature must never leak.
    assert "a: int" not in cc.signature


def test_build_contract_callee_props_from_resolved_node(overload_db):
    """build_contract's callee branch must read raises from the resolved node (id 5,
    RuntimeError) — never the lowest-line homonym's KeyError, never a union merge."""
    items = build_contract(overload_db, [("svc.py", "run")], include_callees=True)
    callees = [e for e in items if e.is_callee and e.function == "handle"]
    assert len(callees) == 1
    cev = callees[0]
    # Resolved node's real raise.
    assert cev.raises == ("RuntimeError",)
    # The other overload's raise must NOT bleed in (no union over the same-name set).
    assert "KeyError" not in cev.raises
    # Signature is the resolved overload's.
    assert "b: dict" in cev.signature
    block = render_contract(items)
    assert "KeyError" not in block
