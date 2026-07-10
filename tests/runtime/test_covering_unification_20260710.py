"""W2 mandate (e) — covering-selection unification: golden-equivalence FIRST.

The mandate asks the mini's ``_covering_tests_for_symbols`` /
``_test_run_command`` to DELEGATE to the shared engine
(``covering_runner.select_covering_tests`` / ``build_covering_command``) and be
deleted — but ONLY after a golden-equivalence proof (same inputs -> same outputs).

These tests RUN that proof and record the OUTCOME honestly:

  1. On the CANONICAL case (each covering test file distinct) the two selectors
     AGREE — the shared engine is a drop-in for that input class.
  2. On the DUPLICATE-FILE case (a symbol called by >1 test in the SAME file) they
     DIVERGE: the mini ``GROUP BY (name, file_path)`` can emit the SAME file twice,
     while the engine ``GROUP BY file_path`` dedups. This is the blocker — the
     divergent list feeds the FROZEN submit gate's covering RUN set, so delegating
     would change which files run at submit-time (unproven without a paid witness).
  3. ``build_covering_command`` is FILE-scoped + leak-safe (no test NAME), whereas
     ``_test_run_command`` is NAME-scoped (``file::name``) — a different contract
     (and a leak surface), pinned here so a future unifier does not conflate them.

Conclusion pinned by these tests: keep both mini helpers; route NEW callers at the
shared engine; do not delete until the frozen-region divergence is witness-cleared.
Hermetic: synthetic graph.db, no network / no toolchain.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_ARTIFACT = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
if _ARTIFACT not in sys.path:
    sys.path.insert(0, _ARTIFACT)

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.covering_runner import (  # noqa: E402
    build_covering_command,
    select_covering_tests,
)


def _mk(tmp_path, nodes, edges):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY,label TEXT,name TEXT,"
        " qualified_name TEXT,file_path TEXT,start_line INTEGER,end_line INTEGER,"
        " signature TEXT,return_type TEXT,is_exported INTEGER,is_test INTEGER,"
        " language TEXT,parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY,source_id INTEGER,target_id INTEGER,"
        " type TEXT,source_line INTEGER,source_file TEXT,resolution_method TEXT,"
        " confidence REAL,metadata TEXT);")
    for n in nodes:
        con.execute("INSERT INTO nodes(id,label,name,file_path,is_test,start_line)"
                    " VALUES(?,?,?,?,?,?)",
                    (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
                     n.get("is_test", 0), n.get("start_line", 1)))
    for e in edges:
        con.execute("INSERT INTO edges(source_id,target_id,type,resolution_method,"
                    "confidence) VALUES(?,?,?,?,?)",
                    (e[0], e[1], "CALLS", e[2], e[3]))
    con.commit()
    con.close()
    return db


def test_canonical_case_selectors_agree(tmp_path, monkeypatch):
    """Distinct covering files: the mini helper and the shared engine AGREE."""
    db = _mk(tmp_path, [
        {"id": 1, "name": "foo", "file_path": "src/a.py"},
        {"id": 10, "name": "test_one", "file_path": "tests/t_a.py", "is_test": 1},
        {"id": 11, "name": "test_two", "file_path": "tests/t_b.py", "is_test": 1},
    ], [(10, 1, "import", 1.0), (11, 1, "same_file", 0.9)])
    monkeypatch.setattr(g, "_db_path", lambda: db)
    mini = g._covering_tests_for_symbols({"foo"})
    runner = select_covering_tests(db, {"foo"}, limit=2)
    assert mini == runner
    assert [c["file"] for c in runner] == ["tests/t_a.py", "tests/t_b.py"]


def test_duplicate_file_case_diverges_blocker(tmp_path, monkeypatch):
    """The DIVERGENCE that blocks delegation: two tests in the SAME file -> the mini
    emits the file TWICE (GROUP BY name,file), the engine dedups (GROUP BY file)."""
    db = _mk(tmp_path, [
        {"id": 1, "name": "foo", "file_path": "src/a.py"},
        {"id": 10, "name": "test_one", "file_path": "tests/t_a.py", "is_test": 1},
        {"id": 11, "name": "test_two", "file_path": "tests/t_a.py", "is_test": 1},
        {"id": 12, "name": "test_three", "file_path": "tests/t_b.py", "is_test": 1},
    ], [(10, 1, "import", 1.0), (11, 1, "import", 1.0), (12, 1, "same_file", 0.9)])
    monkeypatch.setattr(g, "_db_path", lambda: db)
    mini = g._covering_tests_for_symbols({"foo"})
    runner = select_covering_tests(db, {"foo"}, limit=2)
    mini_files = [c["file"] for c in mini]
    runner_files = [c["file"] for c in runner]
    assert mini_files == ["tests/t_a.py", "tests/t_a.py"]      # duplicate file
    assert runner_files == ["tests/t_a.py", "tests/t_b.py"]    # deduped, distinct
    assert mini != runner                                       # NOT golden-equivalent


def test_build_covering_command_is_filescoped_and_leaksafe():
    """``build_covering_command`` is FILE-scoped (no test NAME -> leak-safe), a
    DIFFERENT contract from the NAME-scoped ``_test_run_command`` — the two are not
    interchangeable, so ``_test_run_command`` cannot be blindly delegated."""
    assert build_covering_command("tests/test_bar.py") == ["pytest", "tests/test_bar.py"]
    assert build_covering_command("internal/pkg/foo_test.go") == [
        "go", "test", "./internal/pkg"]
    # the mini helper embeds the test NAME (a leak surface) — DIFFERENT output.
    name_scoped = g._test_run_command("test_foo", "tests/test_bar.py")
    assert "test_foo" in name_scoped and name_scoped != " ".join(
        build_covering_command("tests/test_bar.py"))
