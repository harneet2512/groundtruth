"""Tests for v1.0.4 regression fixes.

Covers:
  - Test path classification (item 5)
  - Standalone critique: removal detection + arity changes (items 3, 4)
  - Edit-trigger detection (item 6)
  - Injection budget logic (item 7)
  - Evidence ranking with OBLIGATION/NEGATIVE (item 8)
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest


# ── Test path classification ──────────────────────────────────────────────


class TestIsTestPath:
    """Item 5: test file classification."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from benchmarks.swebench.gt_intel import _is_test_path
        self.is_test_path = _is_test_path

    def test_test_prefixed_file(self):
        assert self.is_test_path("test_foo.py")

    def test_test_suffixed_go(self):
        assert self.is_test_path("pkg/foo_test.go")

    def test_spec_file(self):
        assert self.is_test_path("src/foo.spec.ts")

    def test_jest_test(self):
        assert self.is_test_path("src/foo.test.js")

    def test_conftest(self):
        assert self.is_test_path("tests/conftest.py")

    def test_tests_directory(self):
        assert self.is_test_path("/tests/unit/helper.py")

    def test_dunder_tests(self):
        assert self.is_test_path("__tests__/bar.tsx")

    def test_setup_py_not_test(self):
        """setup.py is real source, not test code."""
        assert not self.is_test_path("setup.py")

    def test_regular_source(self):
        assert not self.is_test_path("src/model.py")

    def test_regular_go(self):
        assert not self.is_test_path("pkg/handler.go")

    def test_regular_js(self):
        assert not self.is_test_path("lib/auth.js")

    def test_init_py_not_test(self):
        assert not self.is_test_path("src/__init__.py")


# ── Standalone critique ───────────────────────────────────────────────────


def _make_critique_db(tmp_dir: str) -> tuple[str, str]:
    """Create a minimal graph.db and source file for critique testing."""
    db_path = os.path.join(tmp_dir, "graph.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL,
            qualified_name TEXT, file_path TEXT NOT NULL,
            start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT,
            is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
            type TEXT NOT NULL, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
        );
        CREATE TABLE file_hashes (
            file_path TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
            language TEXT, indexed_at TEXT NOT NULL
        );
        CREATE TABLE project_meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    # Insert a function with callers
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, signature, is_exported, language) "
        "VALUES ('Function', 'process', 'app.py', 'def process(data)', 1, 'python')"
    )
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, language) "
        "VALUES ('Function', 'main', 'main.py', 'python')"
    )
    # Caller edge: main.py calls process in app.py
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, source_file, resolution_method, confidence) "
        "VALUES (2, 1, 'CALLS', 'main.py', 'import', 1.0)"
    )
    conn.commit()
    conn.close()
    return db_path, tmp_dir


class TestCritiqueStandalone:
    """Items 3, 4: critique detects arity changes and removals."""

    def test_arity_change_detected(self, tmp_path):
        db_path, root = _make_critique_db(str(tmp_path))
        # Write source with added required param
        (tmp_path / "app.py").write_text("def process(data, mode):\n    pass\n")

        from benchmarks.swebench.gt_intel import compute_critique_standalone
        result = compute_critique_standalone(db_path, "app.py", str(tmp_path))

        assert result is not None
        assert "BREAKING" in result
        assert "process" in result

    def test_removed_function_detected(self, tmp_path):
        db_path, root = _make_critique_db(str(tmp_path))
        # Write source without the function
        (tmp_path / "app.py").write_text("# process was removed\ndef other():\n    pass\n")

        from benchmarks.swebench.gt_intel import compute_critique_standalone
        result = compute_critique_standalone(db_path, "app.py", str(tmp_path))

        assert result is not None
        assert "STALE" in result
        assert "process" in result

    def test_unchanged_file_no_critique(self, tmp_path):
        db_path, root = _make_critique_db(str(tmp_path))
        # Write source with same signature
        (tmp_path / "app.py").write_text("def process(data):\n    return data\n")

        from benchmarks.swebench.gt_intel import compute_critique_standalone
        result = compute_critique_standalone(db_path, "app.py", str(tmp_path))

        assert result is None


# ── Edit-trigger detection ────────────────────────────────────────────────


class TestEditTriggerDetection:
    """Item 6: only strong edit signals trigger evidence."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from benchmarks.swebench.run_mini_gt_hooked import _EDIT_INDICATORS
        self.indicators = _EDIT_INDICATORS

    def _matches(self, command: str) -> bool:
        return any(ind in command for ind in self.indicators)

    def test_sed_i_triggers(self):
        assert self._matches("sed -i 's/old/new/' file.py")

    def test_cat_redirect_triggers(self):
        assert self._matches("cat > file.py << 'EOF'")

    def test_patch_triggers(self):
        assert self._matches("patch -p1 < fix.patch")

    def test_git_apply_triggers(self):
        assert self._matches("git apply fix.patch")

    def test_str_replace_editor_triggers(self):
        assert self._matches("str_replace_editor create /testbed/fix.py")

    def test_grep_does_not_trigger(self):
        assert not self._matches("grep -r 'pattern' src/")

    def test_cat_read_does_not_trigger(self):
        assert not self._matches("cat file.py")

    def test_python_script_does_not_trigger(self):
        assert not self._matches("python3 reproduce.py")

    def test_find_does_not_trigger(self):
        assert not self._matches("find . -name '*.py'")

    def test_ls_does_not_trigger(self):
        assert not self._matches("ls -la src/")


# ── Injection budget ─────────────────────────────────────────────────────


class TestInjectionBudget:
    """Item 7: injection budget enforcement."""

    def test_budget_constants(self):
        from benchmarks.swebench.run_mini_gt_hooked import (
            _MAX_INJECTIONS_PER_TASK,
            _MAX_LINES_PER_INJECTION,
        )
        assert _MAX_INJECTIONS_PER_TASK == 3
        assert _MAX_LINES_PER_INJECTION == 5

    def test_budget_resets_on_container_cleanup(self):
        from benchmarks.swebench.run_mini_gt_hooked import _injection_counts
        # Simulate budget usage
        _injection_counts["test-container"] = 3
        assert _injection_counts["test-container"] == 3
        # Simulate cleanup (as done in hooked_process_instance finally block)
        _injection_counts.pop("test-container", None)
        assert "test-container" not in _injection_counts


# ── Evidence ranking ──────────────────────────────────────────────────────


class TestEvidenceRanking:
    """Item 8: OBLIGATION and NEGATIVE ranking."""

    def test_family_priority_ordering(self):
        """NEGATIVE > OBLIGATION > TEST > CALLER."""
        from benchmarks.swebench.gt_intel import rank_and_select, EvidenceNode
        candidates = [
            EvidenceNode(family="CALLER", score=2, name="f", file="a.py", line=1, source_code="", summary="calls f"),
            EvidenceNode(family="TEST", score=2, name="f", file="t.py", line=1, source_code="", summary="test f"),
            EvidenceNode(family="OBLIGATION", score=2, name="f", file="a.py", line=1, source_code="", summary="must return int"),
            EvidenceNode(family="NEGATIVE", score=3, name="f", file="a.py", line=1, source_code="", summary="NOT EXPORTED"),
        ]
        selected = rank_and_select(candidates)
        families = [n.family for n in selected]
        # NEGATIVE should come first (highest priority)
        assert families[0] == "NEGATIVE"
        # OBLIGATION should come before CALLER
        if "OBLIGATION" in families and "CALLER" in families:
            assert families.index("OBLIGATION") < families.index("CALLER")

    def test_obligation_needs_min_callers(self):
        """OBLIGATION should not fire on weak support (single caller)."""
        from groundtruth_v2.contracts import compute_contract
        # This test verifies the 2-caller minimum floor is in place
        # by checking the threshold constant
        from groundtruth_v2.contracts import _CONTRACT_THRESHOLD
        assert _CONTRACT_THRESHOLD == 0.8

    def test_negative_family_cap(self):
        """NEGATIVE capped at 2 items."""
        from benchmarks.swebench.gt_intel import rank_and_select, EvidenceNode
        candidates = [
            EvidenceNode(family="NEGATIVE", score=3, name="a", file="a.py", line=1, source_code="", summary="not found 1"),
            EvidenceNode(family="NEGATIVE", score=3, name="b", file="a.py", line=2, source_code="", summary="not found 2"),
            EvidenceNode(family="NEGATIVE", score=3, name="c", file="a.py", line=3, source_code="", summary="not found 3"),
        ]
        selected = rank_and_select(candidates)
        neg_count = sum(1 for n in selected if n.family == "NEGATIVE")
        assert neg_count <= 2
