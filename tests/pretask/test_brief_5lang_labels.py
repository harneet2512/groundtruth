"""5-language parity pin: the brief must rank/contract TYPE-DEFINITION edit targets, not just
Function/Method. The indexer labels Go `type`/struct, Rust struct/enum/trait, TS interface, JS/TS
class all as `Class` (verified on a real graph), and Rust `impl` blocks as `ImplBlock`. A fix that
lands on one of those — the DOMINANT edit shape in Go/Rust/TS, rare in Python — must be visible to
the brief's ranker/sibling/contract. This pins every label filter in v1r_brief.py to the full
definition set ('Function','Method','Class','ImplBlock'). It BITES: with the old
('Function','Method') filter the Class/ImplBlock rows are dropped and these assertions fail.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from groundtruth.pretask.v1r_brief import _top_function_names

_SCHEMA = """
CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
  file_path TEXT, start_line INT, end_line INT, signature TEXT, return_type TEXT,
  is_exported INT, is_test INT, language TEXT, parent_id INT);
CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INT, target_id INT,
  type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL, metadata TEXT);
"""


def _make_db(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(_SCHEMA)
    c.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,is_test,language) "
        "VALUES (?,?,?,?,?,?,0,?)",
        [
            (1, "Class", "Foo", "b.go", 2, 4, "go"),  # go struct (type def) edit target
            (2, "Method", "M", "b.go", 3, 3, "go"),
            (3, "Function", "baz", "b.go", 6, 7, "go"),
            (4, "Class", "Bar", "e.rs", 1, 3, "rust"),  # rust struct
            (5, "ImplBlock", "Bar", "e.rs", 4, 6, "rust"),  # rust impl block
        ],
    )
    c.commit()
    c.close()


def test_brief_surfaces_go_struct_edit_target(tmp_path):
    db = str(tmp_path / "g.db")
    _make_db(db)
    out = _top_function_names(db, "b.go", issue_terms={"foo"})
    # 'Foo' is a Go struct (label Class) — the edit target a 'foo' issue points at.
    assert "Foo" in out, f"Go struct (label=Class) edit target not surfaced: {out}"


def test_brief_surfaces_rust_struct_and_impl(tmp_path):
    db = str(tmp_path / "g.db")
    _make_db(db)
    out = _top_function_names(db, "e.rs", issue_terms={"bar"})
    # 'Bar' exists as a Rust struct (Class) AND an impl block (ImplBlock); either label suffices.
    assert "Bar" in out, f"Rust struct/impl (label=Class/ImplBlock) edit target not surfaced: {out}"


def test_no_bare_function_method_filter_remains():
    """Anti-drift tripwire: no label filter in v1r_brief.py may exclude type definitions.
    Any new bare ('Function','Method') filter re-introduces the Python-only blindness."""
    src = Path(__file__).resolve().parents[2] / "src" / "groundtruth" / "pretask" / "v1r_brief.py"
    bare = re.findall(r"label IN \('Function',\s*'Method'\)", src.read_text(encoding="utf-8"))
    assert not bare, (
        f"{len(bare)} bare Function/Method-only label filter(s) remain (drop type-def edits)"
    )
