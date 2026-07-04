"""TTD red-before-green for C1b (B3 semantic-nonsense contract) — HARM.

Defect artifact (beets, real run): the contract miner stored a raise-with-
traceback statement (``raise exc_info[1].with_traceback(...)``) as an
``exception_type`` property, so the brief rendered the garbage line

    Contract: raises raise,exc_info[1].with_traceback

i.e. a ``raises`` contract whose value is a STATEMENT FRAGMENT (commas, dots
into a subscript, a statement keyword) rather than a clean exception-type
identifier. clip_balanced is STRUCTURAL only — brackets are balanced, so the
nonsense passes through. The fix must SEMANTICALLY validate a mined ``raises``
value before render: a clean exception name is a dotted identifier
(``ValueError``, ``pkg.mod.MyError``), never a statement fragment.

These fixtures are built from the OBSERVED beets failure topology (a function
whose body re-raises a captured exc_info tuple with .with_traceback), NOT from
reading the implementation.
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.pretask.contract_map import (
    build_contract,
    contract_line,
    render_contract,
)

# The exact garbage the indexer stored for beets/util/__init__.py::reraise,
# split across two exception_type rows (the two operands of the bad parse).
_GARBAGE_RAISES = ("raise", "exc_info[1].with_traceback")
# The single-row comma-joined form the same defect can take.
_GARBAGE_RAISES_ONE_ROW = "raise,exc_info[1].with_traceback"


def _make_db(path: str, raises_values: list[str]) -> None:
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
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,signature,return_type,is_test) "
        "VALUES (1,'Function','reraise','beets/util/__init__.py',10,"
        "'def reraise(tp, value, tb=None):','',0)"
    )
    for i, v in enumerate(raises_values, start=1):
        conn.execute(
            "INSERT INTO properties (node_id,kind,value,line,confidence) VALUES (1,'exception_type',?,?,1.0)",
            (v, 11 + i),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def garbage_db(tmp_path):
    p = str(tmp_path / "graph.db")
    _make_db(p, list(_GARBAGE_RAISES))
    return p


@pytest.fixture()
def garbage_one_row_db(tmp_path):
    p = str(tmp_path / "graph.db")
    _make_db(p, [_GARBAGE_RAISES_ONE_ROW])
    return p


@pytest.fixture()
def clean_db(tmp_path):
    p = str(tmp_path / "graph.db")
    _make_db(p, ["ValueError", "TypeError"])
    return p


# --- C1b RED-before-green: the garbage must NOT render as a raises contract ---
def test_garbage_raises_suppressed_in_contract_line(garbage_db):
    line = contract_line(garbage_db, "beets/util/__init__.py", ["reraise"])
    assert "raise,exc_info" not in line, f"garbage raises leaked inline:\n{line!r}"
    assert "with_traceback" not in line, f"statement fragment leaked inline:\n{line!r}"


def test_garbage_raises_suppressed_in_render(garbage_db):
    block = render_contract(
        build_contract(garbage_db, [("beets/util/__init__.py", "reraise")], include_callees=False)
    )
    assert "with_traceback" not in block, f"statement fragment leaked in block:\n{block}"
    assert "exc_info" not in block, f"subscript fragment leaked in block:\n{block}"
    # correct-or-quiet: with NO clean raises and no other signal, the block must
    # be empty (a signature-only function still has signal via its signature,
    # but a `raises:` line built from garbage must never appear).
    assert "raises:" not in block, f"empty/garbage raises line rendered:\n{block}"


def test_garbage_raises_one_row_suppressed(garbage_one_row_db):
    line = contract_line(garbage_one_row_db, "beets/util/__init__.py", ["reraise"])
    block = render_contract(
        build_contract(
            garbage_one_row_db, [("beets/util/__init__.py", "reraise")], include_callees=False
        )
    )
    assert "with_traceback" not in line and "with_traceback" not in block


# --- Negative control: clean exception names must STILL render (no over-suppress) ---
def test_clean_raises_still_render(clean_db):
    line = contract_line(clean_db, "beets/util/__init__.py", ["reraise"])
    assert "raises ValueError" in line, f"clean raises over-suppressed inline:\n{line!r}"
    block = render_contract(
        build_contract(clean_db, [("beets/util/__init__.py", "reraise")], include_callees=False)
    )
    assert "ValueError" in block and "TypeError" in block, (
        f"clean exception names over-suppressed:\n{block}"
    )


def test_mixed_clean_and_garbage_keeps_only_clean(tmp_path):
    """A function whose mined raises mix a clean name with garbage keeps the clean
    name and drops the fragment (partial-suppression, not all-or-nothing)."""
    p = str(tmp_path / "graph.db")
    _make_db(p, ["ValueError", "exc_info[1].with_traceback", "raise"])
    line = contract_line(p, "beets/util/__init__.py", ["reraise"])
    assert "ValueError" in line
    assert "with_traceback" not in line and "exc_info" not in line


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
