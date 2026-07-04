"""C1 end-to-end: the v1r brief path (build_contract -> contract_line / render)
must NEVER emit a malformed guard value, even when the indexer stored a
mid-expression-truncated value (old binary build). Proves the _read_props
clip_balanced repair reaches the rendered Contract line.
"""
from __future__ import annotations

import re
import sqlite3

from groundtruth.pretask.contract_map import build_contract, contract_line, render_contract

# The exact malformed value an older indexer stored for haystack document_splitter
# (condText cut at 120 / consequence cut at 60), reproduced byte-for-byte.
_MALFORMED_GUARD = "raise: not isinstance(documents, list) or (documents and not"
_MALFORMED_RAISE = 'raise TypeError("DocumentSplitter expects a List of Document'


def _balanced(s: str) -> bool:
    """Independent oracle (not the implementation under test)."""
    in_str = ""
    esc = False
    depth = 0
    for ch in s:
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = ""
            continue
        if ch in "\"'":
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
    if in_str or depth != 0:
        return False
    return re.search(r"(\b(and|or|not|in|is)\b|[-+*/%<>=&|^~]|->)\s*$", s.strip()) is None


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
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,signature,return_type,is_test) "
        "VALUES (1,'Method','run','document_splitter.py',10,"
        "'def run(self, documents: List[Document]):','dict',0)"
    )
    conn.executemany(
        "INSERT INTO properties (node_id,kind,value,line,confidence) VALUES (1,?,?,?,1.0)",
        [
            ("guard_clause", _MALFORMED_GUARD, 11),
            ("guard_clause", _MALFORMED_RAISE, 12),
            ("exception_type", "TypeError", 11),
        ],
    )
    conn.commit()
    conn.close()


def test_contract_line_never_emits_malformed_guard(tmp_path):
    db = str(tmp_path / "graph.db")
    _make_db(db)
    line = contract_line(db, "document_splitter.py", ["run"])
    assert line, "expected a non-empty contract line (TypeError + repaired guard)"
    assert _balanced(line), f"contract_line emitted malformed guard: {line!r}"
    # the dangling forms must be gone
    assert not line.rstrip().endswith("not")
    assert "List of Document" not in line or line.count('"') % 2 == 0


def test_build_contract_render_block_balanced(tmp_path):
    db = str(tmp_path / "graph.db")
    _make_db(db)
    items = build_contract(db, [("document_splitter.py", "run")], include_callees=False)
    assert items
    for ev in items:
        for g in ev.guards:
            assert _balanced(g), f"stored guard not repaired: {g!r}"
    block = render_contract(items)
    # Balance is a CLAUSE property; the XML block legitimately ends in '>'.
    # Assert on the content lines (preserve/raises/returns), not the wrapper tag.
    assert block.count('"') % 2 == 0, f"unterminated literal in block: {block!r}"
    for ln in block.splitlines():
        s = ln.strip()
        if s.startswith(("preserve:", "raises:", "returns:", "bounds:")):
            payload = s.split(":", 1)[1]
            assert _balanced(payload), f"malformed contract content line: {ln!r}"
