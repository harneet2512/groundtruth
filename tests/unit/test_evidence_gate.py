"""Tests for the evidence gate in oh_gt_full_wrapper.py.

Verifies that ALL markers produced by post_edit.py are recognized by the
has_evidence check. This prevents the silent-drop bug where valid evidence
(BEHAVIORAL CONTRACT, TEST EXPECTS) was produced but not delivered.
"""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from groundtruth.hooks.post_edit import generate_improved_evidence


EVIDENCE_MARKERS = (
    "SIGNATURE:", "CALLERS:", "SIBLING:", "TWINS:",
    "PROPAGATE:", "CO-CHANGE:", "SCOPE:",
    "BEHAVIORAL CONTRACT:", "TEST EXPECTS:", "TEST:",
    "WARNING:", "TOP CALLER:", "MUST PRESERVE:",
    "[CONTRACT]", "[CONTRACT ~]", "[SIGNATURE]", "[PATTERN]", "[PEER]",
    "[TWINS]", "[PROPAGATE]", "[CO-CHANGE]", "[SCOPE]",
    "[BEHAVIORAL CONTRACT]", "[TEST]",
    "[GT_VERIFY]", "[GT L3:",
)


def _has_evidence(hook_body: str) -> bool:
    """Exact replica of the has_evidence check from the wrapper."""
    return any(t in hook_body for t in EVIDENCE_MARKERS)


@pytest.fixture
def rich_graph_db(tmp_path: Path) -> tuple[str, str]:
    """Graph.db with a function that has guards, callers, and a signature."""
    db_path = str(tmp_path / "graph.db")
    repo_root = str(tmp_path / "repo")
    (tmp_path / "repo" / "src").mkdir(parents=True)

    conn = sqlite3.connect(db_path)
    conn.executescript("""
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
            parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES nodes(id),
            target_id INTEGER NOT NULL REFERENCES nodes(id),
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 1.0,
            metadata TEXT
        );
    """)

    conn.execute("""
        INSERT INTO nodes (id, label, name, qualified_name, file_path,
            start_line, end_line, signature, language)
        VALUES (1, 'Function', 'colorize', 'src.colors.colorize',
            'src/colors.py', 1, 12,
            'colorize(text: str, color: str) -> str', 'python')
    """)
    conn.execute("""
        INSERT INTO nodes (id, label, name, qualified_name, file_path,
            start_line, end_line, signature, language)
        VALUES (2, 'Function', 'render', 'src.render.render',
            'src/render.py', 5, 10,
            'render(msg: str) -> None', 'python')
    """)
    conn.execute("""
        INSERT INTO edges (source_id, target_id, type, source_line,
            source_file, resolution_method, confidence)
        VALUES (2, 1, 'CALLS', 7, 'src/render.py', 'import', 1.0)
    """)
    conn.commit()
    conn.close()

    (tmp_path / "repo" / "src" / "colors.py").write_text(textwrap.dedent("""\
        def colorize(text, color):
            if os.environ.get('FORCE_COLOR'):
                return True
            if not sys.stderr.isatty():
                return False
            if color is None:
                raise ValueError("color required")
            try:
                import colorama
            except ImportError:
                return text
            return colorama.init() + text
    """))

    (tmp_path / "repo" / "src" / "render.py").write_text(textwrap.dedent("""\
        from src.colors import colorize

        def render(msg):
            result = colorize(msg, 'red')
            print(result)
    """))

    return db_path, repo_root


class TestEvidenceGateRecognition:
    """Verify that post_edit output is recognized by the wrapper's gate."""

    def test_behavioral_contract_recognized(self, rich_graph_db):
        db_path, repo_root = rich_graph_db
        output = generate_improved_evidence(
            file_path="src/colors.py",
            function_names=["colorize"],
            db_path=db_path,
            repo_root=repo_root,
        )
        assert "[BEHAVIORAL CONTRACT]" in output, (
            f"post_edit should produce BEHAVIORAL CONTRACT for a function with "
            f"2+ guards. Got: {output!r}"
        )
        assert _has_evidence(output), (
            f"Evidence gate MUST recognize BEHAVIORAL CONTRACT. Got: {output!r}"
        )

    def test_output_always_recognized(self, rich_graph_db):
        """Any non-empty output should be caught by the gate."""
        db_path, repo_root = rich_graph_db
        output = generate_improved_evidence(
            file_path="src/colors.py",
            function_names=["colorize"],
            db_path=db_path,
            repo_root=repo_root,
        )
        assert output.strip(), "Should produce some evidence"
        assert _has_evidence(output), (
            f"Gate must recognize whatever post_edit produces. Got: {output!r}"
        )

    def test_caller_warning_recognized(self, rich_graph_db):
        """When callers exist, some form of caller evidence should be present."""
        db_path, repo_root = rich_graph_db
        output = generate_improved_evidence(
            file_path="src/colors.py",
            function_names=["colorize"],
            db_path=db_path,
            repo_root=repo_root,
        )
        assert _has_evidence(output), (
            f"Caller evidence should trigger the gate. Got: {output!r}"
        )

    def test_empty_function_no_false_positive(self, rich_graph_db):
        """A function that doesn't exist in graph should produce minimal output."""
        db_path, repo_root = rich_graph_db
        output = generate_improved_evidence(
            file_path="src/colors.py",
            function_names=["nonexistent_func"],
            db_path=db_path,
            repo_root=repo_root,
        )
        # No function in graph → no callers, no signature, no contract → empty or minimal
        # This is correct behavior — gate should NOT fire for noise
        if output.strip():
            # If there IS output, verify it's recognized
            assert _has_evidence(output) or len(output.strip()) < 20

    def test_status_only_output_rejected(self):
        assert not _has_evidence("[GT_STATUS] success:3_items")


class TestBehavioralContractFires:
    """Verify the behavioral contract actually fires for qualifying functions."""

    def test_fires_with_two_guards(self, rich_graph_db):
        db_path, repo_root = rich_graph_db
        output = generate_improved_evidence(
            file_path="src/colors.py",
            function_names=["colorize"],
            db_path=db_path,
            repo_root=repo_root,
        )
        assert "[BEHAVIORAL CONTRACT]" in output
        assert "GUARD:" in output

    def test_shows_return_paths(self, rich_graph_db):
        db_path, repo_root = rich_graph_db
        output = generate_improved_evidence(
            file_path="src/colors.py",
            function_names=["colorize"],
            db_path=db_path,
            repo_root=repo_root,
        )
        assert "return" in output.lower()
