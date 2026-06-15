"""Delivery-surface test/demo path filter — gt_mini_patch.py.

Two generalized GT delivery bugs (witnessed live on ts/awilix + js/csstree):

  BUG-A  the live <gt-scope> CONSENSUS producer (_consensus_block) filtered
         vendored + cross-language neighbours but had NO test-file filter, so it
         surfaced ``__tests__/awilix.test.ts`` as an in-scope file. The agent is
         told "DO NOT MODIFY tests" — a scope line naming a test file is noise.
  BUG-C  the [WITNESS] producer (rows from _resolved_witnesses_for_file) surfaced
         ``[WITNESS] getStuff called by examples/simple/services/functionalService.js``
         — an examples/ DEMO file, not real source.

Both are fixed by a single generalized DIRECTORY-SEGMENT predicate
``_is_test_or_demo_path`` wired into the neighbour-accept condition of the
<gt-scope> producer and the row-accept points of the [WITNESS] producer.

These are UNIT tests of the patched code (not a benchmark/agent run). They go
RED on the pre-fix code (no test/demo filter) and GREEN after the surgical fix.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. Pure-predicate truth table (the generalized contract).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "__tests__/awilix.test.ts",          # witnessed BUG-A leak (dir + basename marker)
        "examples/simple/x.js",              # witnessed BUG-C leak (demo dir)
        "test/lexer.js",                     # csstree: relative top-level test/ dir, NO leading slash
        "tests/conftest.py",                 # plural test dir
        "spec/parser_spec.rb",               # spec dir
        "src/foo_test.go",                   # basename *_test.* marker
        "src/foo.test.ts",                   # basename *.test.* marker
        "src/foo.spec.ts",                   # basename *.spec.* marker
        "pkg/test_helpers.py",               # basename test_* marker
        "node_modules/awilix/index.js",      # vendor/non-source dir
        "dist/bundle.js",                    # build-output dir
        "docs/usage.md",                     # docs dir
        "benchmark/run.js",                  # benchmark dir
        "./examples/demo.js",                # leading ./ must be stripped before segment split
    ],
)
def test_test_or_demo_paths_are_true(path):
    assert g._is_test_or_demo_path(path) is True, path


@pytest.mark.parametrize(
    "path",
    [
        "src/container.ts",                   # real source
        "lib/lexer/Lexer.js",                 # 'lexer' contains 'test'? no — and not a test dir
        "src/services/functionalService.js",  # the REAL source twin of the demo leak
        "internal/resolver/resolver.go",      # real source
        "groundtruth/pretask/v1r_brief.py",   # real source
        "src/contest/x.py",                    # 'contest' must NOT match 'test' (segment, not substring)
        "src/latest/build.py",                 # 'latest' must NOT match 'test'
    ],
)
def test_real_source_paths_are_false(path):
    assert g._is_test_or_demo_path(path) is False, path


# --------------------------------------------------------------------------- #
# 2. Behavioral: the <gt-scope> producer excludes the test neighbour (BUG-A).
# --------------------------------------------------------------------------- #
def _make_scope_graph(path: str) -> None:
    """src/container.ts (the viewed file) is connected by a DETERMINISTIC 'import'
    edge to BOTH a real source neighbour (src/resolvers.ts) and a test neighbour
    (__tests__/awilix.test.ts). Only the source neighbour may reach <gt-scope>."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,
            language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, resolution_method TEXT, confidence REAL
        );
        """
    )
    con.execute("INSERT INTO nodes VALUES (1,'Function','createContainer','src/container.ts','typescript',NULL)")
    con.execute("INSERT INTO nodes VALUES (2,'Function','resolve','src/resolvers.ts','typescript',NULL)")
    con.execute("INSERT INTO nodes VALUES (3,'Function','testCreate','__tests__/awilix.test.ts','typescript',NULL)")
    # container.ts <- resolvers.ts (real source neighbour, deterministic edge)
    con.execute("INSERT INTO edges VALUES (NULL,2,1,'CALLS','import',1.0)")
    # container.ts <- __tests__/awilix.test.ts (test neighbour, deterministic edge)
    con.execute("INSERT INTO edges VALUES (NULL,3,1,'CALLS','import',1.0)")
    con.commit()
    con.close()


@pytest.fixture
def _scope_env(monkeypatch, tmp_path):
    db = str(tmp_path / "graph.db")
    _make_scope_graph(db)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    # consensus fires once per run; reset the run-level latch + scope memory.
    monkeypatch.setattr(g, "_consensus_fired", False)
    monkeypatch.setattr(g, "_consensus_scope", set())
    return db


def test_gt_scope_excludes_test_neighbour(_scope_env):
    """GREEN: the real-source neighbour reaches <gt-scope>; the __tests__ neighbour
    is filtered. RED pre-fix: both deterministic neighbours rendered -> the test
    file leaked as 'graph-connected'."""
    block = g._consensus_block("src/container.ts", "/testbed")
    assert "<gt-scope" in block, "expected a non-empty scope block (source neighbour present)"
    # the real source neighbour IS surfaced
    assert "resolvers.ts" in block
    # the test file is NOT surfaced (BUG-A)
    assert "awilix.test.ts" not in block
    assert "__tests__" not in block


def test_gt_scope_quiet_when_only_test_neighbour(monkeypatch, tmp_path):
    """If the ONLY deterministic neighbour is a test file, <gt-scope> emits nothing
    (correct-or-quiet) rather than a scope whose only line is the viewed file."""
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,
            language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, resolution_method TEXT, confidence REAL
        );
        """
    )
    con.execute("INSERT INTO nodes VALUES (1,'Function','createContainer','src/container.ts','typescript',NULL)")
    con.execute("INSERT INTO nodes VALUES (2,'Function','testCreate','__tests__/awilix.test.ts','typescript',NULL)")
    con.execute("INSERT INTO edges VALUES (NULL,2,1,'CALLS','import',1.0)")
    con.commit()
    con.close()
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_consensus_fired", False)
    monkeypatch.setattr(g, "_consensus_scope", set())
    block = g._consensus_block("src/container.ts", "/testbed")
    assert block == "", "scope with only a test neighbour must be quiet"


# --------------------------------------------------------------------------- #
# 3. Behavioral: the [WITNESS] producer drops a demo-dir caller (BUG-C).
# --------------------------------------------------------------------------- #
def test_witness_excludes_demo_caller(monkeypatch, tmp_path):
    """A cross-file caller living under examples/ is not a real-source witness.
    GREEN: the demo caller row is skipped; the real-source caller row is kept.
    RED pre-fix: examples/.../functionalService.js leaked as a [WITNESS] caller."""
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,
            language TEXT, parent_id INTEGER, is_test INTEGER, start_line INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, resolution_method TEXT, confidence REAL, source_line INTEGER
        );
        """
    )
    # getStuff is defined in the viewed file; it is CALLED from two places.
    con.execute("INSERT INTO nodes VALUES (1,'Function','getStuff','src/lib.js','javascript',NULL,0,5)")
    con.execute("INSERT INTO nodes VALUES (2,'Function','realCaller','src/app.js','javascript',NULL,0,10)")
    con.execute("INSERT INTO nodes VALUES (3,'Function','demoCaller','examples/simple/services/functionalService.js','javascript',NULL,0,20)")
    # real source caller -> getStuff (deterministic), line 10
    con.execute("INSERT INTO edges VALUES (NULL,2,1,'CALLS','import',1.0,10)")
    # demo caller -> getStuff (deterministic), line 20
    con.execute("INSERT INTO edges VALUES (NULL,3,1,'CALLS','import',1.0,20)")
    con.commit()
    con.close()

    # _code_at reads the live call-site line; stub it so snippet-attestation passes
    # for BOTH callers (the demo caller must be dropped by the PATH filter, not by a
    # missing snippet — otherwise the test wouldn't prove the path guard).
    monkeypatch.setattr(g, "_code_at", lambda root, fp, line: "getStuff()")

    con = g._connect_ro(db)
    try:
        rows = g._resolved_witnesses_for_file(con, "src/lib.js", "/testbed", max_each=5)
    finally:
        con.close()
    caller_files = [r["file_path"] for r in rows if r["direction"] == "caller"]
    assert "src/app.js" in caller_files, "the real-source caller must remain a witness"
    assert not any("examples/" in f for f in caller_files), "demo-dir caller leaked as a witness"
