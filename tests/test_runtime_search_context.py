"""Capability tests for groundtruth.runtime.search_context — grep-trigger enrichment.

Ground truth: a fixture graph.db with a known call graph + FTS index. Assert
the block names the right symbols, callers, callees, and flow membership —
and that it returns empty (attach nothing) for vague or non-matching input.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.runtime.processes import detect_processes
from groundtruth.runtime.search_context import build_search_context, extract_search_terms


def _build_db(path: Path, *, with_fts: bool = True) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER,
            is_test INTEGER, signature TEXT
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, trust_tier TEXT, confidence REAL
        );
        """
    )
    db.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,"
        "is_test,signature) VALUES (?,?,?,?,?,?,0,'')",
        [
            (1, "Function", "main", "main.py", 1, 50),
            (2, "Function", "Install", "install.go", 19, 90),
            (3, "Function", "getZip", "install.go", 68, 119),
            (4, "Function", "unzip", "install.go", 122, 180),
        ],
    )
    db.executemany(
        "INSERT INTO edges (source_id,target_id,type,trust_tier,confidence)"
        " VALUES (?,?,'CALLS','CERTIFIED',1.0)",
        [(1, 2), (2, 3), (2, 4)],
    )
    if with_fts:
        db.executescript(
            "CREATE VIRTUAL TABLE nodes_fts USING fts5"
            "(name, qualified_name, signature, file_path);"
        )
        db.executemany(
            "INSERT INTO nodes_fts (rowid, name, qualified_name, file_path)"
            " VALUES (?,?,?,?)",
            [(1, "main", "main", "main.py"), (2, "Install", "Install", "install.go"),
             (3, "getZip", "getZip", "install.go"), (4, "unzip", "unzip", "install.go")],
        )
    db.commit()
    db.close()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "graph.db"
    _build_db(path)
    return path


def test_enriches_matching_pattern(db_path: Path) -> None:
    block = build_search_context(db_path, "Install")
    assert "<gt-search-context>" in block
    assert "Install (Function, install.go:19)" in block
    assert "called by: main (main.py:1)" in block
    assert "calls: getZip (install.go:68)" in block
    assert "unzip" in block


def test_flow_membership_shown(db_path: Path) -> None:
    procs = detect_processes(db_path, min_steps=2).processes
    block = build_search_context(db_path, "getZip", processes=procs)
    assert "in flow: main -> getZip" in block


def test_short_pattern_returns_empty(db_path: Path) -> None:
    assert build_search_context(db_path, "x") == ""
    assert build_search_context(db_path, "") == ""


def test_no_match_returns_empty(db_path: Path) -> None:
    assert build_search_context(db_path, "nonexistentSymbol") == ""


def test_regex_pattern_terms_extracted(db_path: Path) -> None:
    assert "getZip" in extract_search_terms("getZip|unzip")
    assert "fetch" in extract_search_terms(r"\bfetch\w*Data\b")


def test_works_without_fts(tmp_path: Path) -> None:
    path = tmp_path / "g.db"
    _build_db(path, with_fts=False)
    block = build_search_context(path, "Instal")
    assert "Install" in block  # LIKE fallback


def test_missing_db_returns_empty(tmp_path: Path) -> None:
    assert build_search_context(tmp_path / "nope.db", "Install") == ""
