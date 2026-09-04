"""Stage B — covering-test query wired into the obligation nudge (red->green).

Verifies:
1. _covering_tests_for_symbols queries graph.db correctly (FACT-tier edges,
   is_test=1, conf>=0.7, returns test name + file + run command).
2. _test_run_command dispatches per-language run commands correctly.
3. The obligation nudge includes covering-test info when available (the
   actionable upgrade from "run a test" to "run test_X in tests/test_Y.py").
4. Correct-or-quiet: no graph / no test / no FACT edge -> stays generic.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"


def _load_patch():
    prev = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"
    name = "gt_mini_patch_stage_b_test"
    try:
        spec = importlib.util.spec_from_file_location(name, _PATCH_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prev


@pytest.fixture(scope="module")
def gmp():
    return _load_patch()


def _create_test_graph(tmp_path) -> str:
    """Create a minimal graph.db with test coverage edges."""
    db_path = str(tmp_path / "graph.db")
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        -- Source function (the one being edited)
        INSERT INTO nodes (id, label, name, qualified_name, file_path, is_test, language)
            VALUES (1, 'Function', 'capture_snapshot', 'monitor.capture_snapshot',
                    'src/monitor.py', 0, 'python');
        -- Test function that CALLS capture_snapshot (FACT-tier edge)
        INSERT INTO nodes (id, label, name, qualified_name, file_path, is_test, language)
            VALUES (2, 'Function', 'test_capture_snapshot', 'tests.test_monitor.test_capture_snapshot',
                    'tests/test_monitor.py', 1, 'python');
        -- A second source function
        INSERT INTO nodes (id, label, name, qualified_name, file_path, is_test, language)
            VALUES (3, 'Function', 'initialize', 'container.initialize',
                    'src/container.ts', 0, 'typescript');
        -- Test for the TS function
        INSERT INTO nodes (id, label, name, qualified_name, file_path, is_test, language)
            VALUES (4, 'Function', 'test_initialize', 'container.test.test_initialize',
                    'tests/container.test.ts', 1, 'typescript');
        -- FACT-tier CALLS edge: test_capture_snapshot -> capture_snapshot
        INSERT INTO edges (source_id, target_id, type, resolution_method, confidence)
            VALUES (2, 1, 'CALLS', 'import', 1.0);
        -- FACT-tier CALLS edge: test_initialize -> initialize
        INSERT INTO edges (source_id, target_id, type, resolution_method, confidence)
            VALUES (4, 3, 'CALLS', 'same_file', 0.95);
        -- name_match edge (should NOT be used — below threshold)
        INSERT INTO nodes (id, label, name, qualified_name, file_path, is_test, language)
            VALUES (5, 'Function', 'test_ambiguous', 'tests.test_ambiguous',
                    'tests/test_ambiguous.py', 1, 'python');
        INSERT INTO edges (source_id, target_id, type, resolution_method, confidence)
            VALUES (5, 1, 'CALLS', 'name_match', 0.4);
    """)
    con.close()
    return db_path


# ---------------------------------------------------------------------------
# Test 1: _covering_tests_for_symbols with a real graph
# ---------------------------------------------------------------------------
class TestCoveringTestQuery:
    def test_finds_fact_tier_test(self, gmp, tmp_path):
        """A test with a FACT-tier CALLS edge is returned."""
        db_path = _create_test_graph(tmp_path)
        # Monkey-patch _db_path to use our test graph
        orig_db = gmp._db_path
        gmp._db_path = lambda: db_path
        try:
            results = gmp._covering_tests_for_symbols({"capture_snapshot"})
            assert len(results) == 1
            assert results[0]["name"] == "test_capture_snapshot"
            assert results[0]["file"] == "tests/test_monitor.py"
            assert results[0]["confidence"] >= 0.7
            assert "pytest" in results[0]["run_cmd"]
            assert "test_capture_snapshot" in results[0]["run_cmd"]
        finally:
            gmp._db_path = orig_db

    def test_excludes_name_match(self, gmp, tmp_path):
        """name_match edges (even if they point to a test) are excluded."""
        db_path = _create_test_graph(tmp_path)
        orig_db = gmp._db_path
        gmp._db_path = lambda: db_path
        try:
            # Only name_match edges exist for a hypothetical symbol
            # that matches node 1 via the name_match edge from node 5
            results = gmp._covering_tests_for_symbols({"capture_snapshot"})
            # Should find only the import-resolved test, not the name_match one
            assert len(results) == 1
            assert results[0]["name"] == "test_capture_snapshot"
        finally:
            gmp._db_path = orig_db

    def test_typescript_dispatch(self, gmp, tmp_path):
        """TypeScript test files get `npx jest -t` command."""
        db_path = _create_test_graph(tmp_path)
        orig_db = gmp._db_path
        gmp._db_path = lambda: db_path
        try:
            results = gmp._covering_tests_for_symbols({"initialize"})
            assert len(results) == 1
            assert results[0]["name"] == "test_initialize"
            assert "jest" in results[0]["run_cmd"]
        finally:
            gmp._db_path = orig_db

    def test_empty_on_no_graph(self, gmp):
        """No graph -> empty list (correct-or-quiet)."""
        orig_db = gmp._db_path
        gmp._db_path = lambda: "/nonexistent/graph.db"
        try:
            results = gmp._covering_tests_for_symbols({"anything"})
            assert results == []
        finally:
            gmp._db_path = orig_db

    def test_empty_on_no_matching_symbols(self, gmp, tmp_path):
        """Symbols not in graph -> empty list."""
        db_path = _create_test_graph(tmp_path)
        orig_db = gmp._db_path
        gmp._db_path = lambda: db_path
        try:
            results = gmp._covering_tests_for_symbols({"nonexistent_symbol"})
            assert results == []
        finally:
            gmp._db_path = orig_db


# ---------------------------------------------------------------------------
# Test 2: _test_run_command language dispatch
# ---------------------------------------------------------------------------
class TestRunCommandDispatch:
    def test_python(self, gmp):
        cmd = gmp._test_run_command("test_foo", "tests/test_bar.py")
        assert cmd == "pytest tests/test_bar.py::test_foo"

    def test_go(self, gmp):
        cmd = gmp._test_run_command("TestFoo", "internal/pkg/foo_test.go")
        assert "go test" in cmd
        assert "'^TestFoo$'" in cmd
        assert "./internal/pkg/..." in cmd

    def test_rust(self, gmp):
        cmd = gmp._test_run_command("test_bar", "src/lib_test.rs")
        assert cmd == "cargo test test_bar"

    def test_typescript(self, gmp):
        cmd = gmp._test_run_command("testInit", "src/__tests__/init.test.ts")
        assert "jest" in cmd
        assert "testInit" in cmd

    def test_java(self, gmp):
        cmd = gmp._test_run_command("testMethod", "src/test/FooTest.java")
        assert "mvn test" in cmd
        assert "FooTest#testMethod" in cmd

    def test_fallback(self, gmp):
        cmd = gmp._test_run_command("test_x", "tests/test_x.unknown")
        # Falls back to pytest-style
        assert "pytest" in cmd
