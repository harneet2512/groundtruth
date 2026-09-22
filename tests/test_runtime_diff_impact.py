"""Capability tests for groundtruth.runtime.diff_impact — pre-submit diff gate.

Ground truth: fixture graph.db where Install calls getZip+unzip and main
calls Install. A diff inside getZip's body must surface Install at depth 1,
main at depth 2, and the affected flow. Clean/noise diffs must be silent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.runtime.diff_impact import (
    diff_impact,
    parse_diff_files_and_hunks,
    render_impact_block,
)
from groundtruth.runtime.processes import detect_processes


def _build_db(path: Path) -> None:
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
            (2, "Function", "Install", "install.go", 19, 60),
            (3, "Function", "getZip", "install.go", 68, 119),
            (4, "Function", "unzip", "install.go", 122, 180),
        ],
    )
    db.executemany(
        "INSERT INTO edges (source_id,target_id,type,trust_tier,confidence)"
        " VALUES (?,?,'CALLS','CERTIFIED',1.0)",
        [(1, 2), (2, 3), (2, 4)],
    )
    db.commit()
    db.close()


DIFF = """diff --git a/install.go b/install.go
index 111..222 100644
--- a/install.go
+++ b/install.go
@@ -80,5 +80,10 @@ func getZip() {
-old line
+new line
"""


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "graph.db"
    _build_db(path)
    return path


def test_diff_parse() -> None:
    files = parse_diff_files_and_hunks(DIFF)
    assert files == {"install.go": [(80, 10)]}


def test_impact_maps_changed_symbol(db_path: Path) -> None:
    result = diff_impact(db_path, DIFF)
    assert [s.name for s in result.changed_symbols] == ["getZip"]


def test_callers_banded_by_depth(db_path: Path) -> None:
    result = diff_impact(db_path, DIFF)
    assert result.callers_by_depth[1] == [("Install", "install.go:19")]
    assert result.callers_by_depth[2] == [("main", "main.py:1")]
    assert 3 not in result.callers_by_depth


def test_affected_flows_reported(db_path: Path) -> None:
    procs = detect_processes(db_path, min_steps=2).processes
    result = diff_impact(db_path, DIFF, processes=procs)
    assert any("getZip" in p.label for p in result.affected_flows)


def test_render_block_content(db_path: Path) -> None:
    procs = detect_processes(db_path, min_steps=2).processes
    text = render_impact_block(diff_impact(db_path, DIFF, processes=procs))
    assert "WILL BREAK" in text
    assert "Install (install.go:19)" in text
    assert "Affected flows" in text


def test_hunk_outside_symbol_is_silent(db_path: Path) -> None:
    diff = DIFF.replace("@@ -80,5 +80,10 @@", "@@ -190,5 +190,10 @@")
    result = diff_impact(db_path, diff)
    assert result.empty
    assert render_impact_block(result) == ""


def test_non_diff_input_is_silent(db_path: Path) -> None:
    assert diff_impact(db_path, "total 42\n-rw-r--r--\n").empty


def test_changed_symbols_excluded_from_caller_list(db_path: Path) -> None:
    """A changed symbol must not list itself as an impacted caller."""
    result = diff_impact(db_path, DIFF)
    flat = [name for callers in result.callers_by_depth.values() for name, _ in callers]
    assert "getZip" not in flat


def test_same_named_callers_in_different_files_are_distinct(tmp_path: Path) -> None:
    """Two different functions named ``run`` both call the changed symbol:
    keyed by bare name they collapsed into one caller."""
    path = tmp_path / "g.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, is_test INTEGER,"
        " signature TEXT);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER,"
        " target_id INTEGER, type TEXT, trust_tier TEXT, confidence REAL);"
    )
    db.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,0,'')",
        [
            (1, "Function", "target", "core.py", 1, 5),
            (2, "Function", "run", "a.py", 3, 6),
            (3, "Function", "run", "b.py", 3, 6),
        ],
    )
    db.executemany(
        "INSERT INTO edges (source_id,target_id,type,trust_tier,confidence)"
        " VALUES (?,?,'CALLS',?,?)",
        [(2, 1, "CERTIFIED", 1.0), (3, 1, "CANDIDATE", 0.6)],
    )
    db.commit()
    db.close()
    diff = "--- a/core.py\n+++ b/core.py\n@@ -2 +2 @@\n-x\n+y\n"
    result = diff_impact(path, diff)
    assert result.callers_by_depth[1] == [("run", "a.py:3"), ("run", "b.py:3")]
    tiers = {d.file_path: d.trust_tier for d in result.caller_details_by_depth[1]}
    assert tiers == {"a.py": "CERTIFIED", "b.py": "CANDIDATE"}
