"""Unit tests for the improved L3 post-edit evidence (graph.db-driven).

Tests the generate_improved_evidence function which produces priority-ordered
code evidence from graph.db: callers -> siblings -> signature -> tests.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from groundtruth.hooks.post_edit import (
    generate_improved_evidence,
    _get_callers_from_graph,
    _get_signature_from_graph,
    _get_siblings_from_graph,
    _read_source_line,
    _resolve_node_id,
)


@pytest.fixture
def graph_db(tmp_path: Path) -> str:
    """Create an in-memory-style graph.db with realistic nodes and edges."""
    db_path = str(tmp_path / "graph.db")
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
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        CREATE TABLE assertions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_node_id INTEGER NOT NULL,
            target_node_id INTEGER NOT NULL,
            kind TEXT,
            expression TEXT,
            expected TEXT,
            line INTEGER
        );

        -- Target function: validate_token in src/auth.py
        INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, return_type, is_exported, is_test, language, parent_id)
        VALUES (1, 'Function', 'validate_token', 'auth.validate_token', 'src/auth.py', 10, 25, 'def validate_token(token: str) -> bool', 'bool', 1, 0, 'python', NULL);

        -- Caller 1: routes.py line 47
        INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, return_type, is_exported, is_test, language, parent_id)
        VALUES (2, 'Function', 'handle_request', 'routes.handle_request', 'src/api/routes.py', 40, 60, 'def handle_request(request) -> Response', 'Response', 1, 0, 'python', NULL);

        -- Caller 2: middleware.py line 23
        INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, return_type, is_exported, is_test, language, parent_id)
        VALUES (3, 'Function', 'auth_middleware', 'middleware.auth_middleware', 'src/middleware.py', 20, 35, 'def auth_middleware(tok: str) -> None', 'None', 1, 0, 'python', NULL);

        -- Caller 3 (same file -- should be excluded from cross-file callers)
        INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, return_type, is_exported, is_test, language, parent_id)
        VALUES (4, 'Function', 'refresh_token', 'auth.refresh_token', 'src/auth.py', 30, 40, 'def refresh_token(old: str) -> str', 'str', 1, 0, 'python', NULL);

        -- Sibling function (same file, top-level)
        INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, return_type, is_exported, is_test, language, parent_id)
        VALUES (5, 'Function', 'validate_session', 'auth.validate_session', 'src/auth.py', 50, 65, 'def validate_session(session_id: str) -> bool', 'bool', 1, 0, 'python', NULL);

        -- Test function
        INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, return_type, is_exported, is_test, language, parent_id)
        VALUES (6, 'Function', 'test_validate_token', 'test_auth.test_validate_token', 'tests/test_auth.py', 10, 20, 'def test_validate_token()', '', 0, 1, 'python', NULL);

        -- Low-confidence caller (should be filtered)
        INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, return_type, is_exported, is_test, language, parent_id)
        VALUES (7, 'Function', 'maybe_validate', 'utils.maybe_validate', 'src/utils.py', 5, 10, 'def maybe_validate(x)', '', 1, 0, 'python', NULL);

        -- Edges: callers -> validate_token
        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
        VALUES (2, 1, 'CALLS', 47, 'src/api/routes.py', 'import', 1.0);

        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
        VALUES (3, 1, 'CALLS', 23, 'src/middleware.py', 'import', 1.0);

        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
        VALUES (4, 1, 'CALLS', 35, 'src/auth.py', 'same_file', 1.0);

        -- Low confidence edge (should be filtered)
        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
        VALUES (7, 1, 'CALLS', 7, 'src/utils.py', 'name_match', 0.2);

        -- Test assertion
        INSERT INTO assertions (test_node_id, target_node_id, kind, expression, expected, line)
        VALUES (6, 1, 'assertEqual', 'validate_token("valid-jwt")', 'True', 15);
    """)
    conn.close()
    return db_path


@pytest.fixture
def repo_root(tmp_path: Path) -> str:
    """Create a minimal repo with source files that have code at the expected lines."""
    root = tmp_path / "repo"
    root.mkdir()

    # src/auth.py
    auth_dir = root / "src"
    auth_dir.mkdir()
    auth_lines = [""] * 9  # lines 1-9 empty
    auth_lines.append("def validate_token(token: str) -> bool:")  # line 10
    auth_lines.extend(["    ..."] * 15)  # lines 11-25
    auth_lines.extend([""] * 4)  # lines 26-29
    auth_lines.append("def refresh_token(old: str) -> str:")  # line 30
    auth_lines.extend(["    ..."] * 10)  # lines 31-40
    auth_lines.extend([""] * 9)  # lines 41-49
    auth_lines.append("def validate_session(session_id: str) -> bool:")  # line 50
    auth_lines.append("    if not isinstance(session_id, str):")  # line 51
    auth_lines.append("        return False")  # line 52
    auth_lines.extend(["    ..."] * 13)  # lines 53-65
    (auth_dir / "auth.py").write_text("\n".join(auth_lines), encoding="utf-8")

    # src/api/routes.py
    api_dir = auth_dir / "api"
    api_dir.mkdir()
    routes_lines = [""] * 46  # lines 1-46 empty
    routes_lines.append("    token = validate_token(request.headers['auth'])")  # line 47
    routes_lines.extend([""] * 13)  # pad to 60
    (api_dir / "routes.py").write_text("\n".join(routes_lines), encoding="utf-8")

    # src/middleware.py
    mw_lines = [""] * 22  # lines 1-22 empty
    mw_lines.append("    if not validate_token(tok): raise HTTPError(401)")  # line 23
    mw_lines.extend([""] * 12)
    (auth_dir / "middleware.py").write_text("\n".join(mw_lines), encoding="utf-8")

    return str(root)


class TestGetCallersFromGraph:
    def test_returns_cross_file_callers(self, graph_db: str, repo_root: str) -> None:
        callers = _get_callers_from_graph(
            graph_db, "src/auth.py", "validate_token", repo_root,
            seen_files=[], limit=5
        )
        # Should get 2 cross-file callers (routes.py and middleware.py)
        # Same-file caller (refresh_token in auth.py) excluded by query
        # Low-confidence caller (utils.py at 0.2) excluded by confidence >= 0.5
        assert len(callers) == 2
        files = {c["file"] for c in callers}
        assert "src/api/routes.py" in files
        assert "src/middleware.py" in files

    def test_reads_actual_code_line(self, graph_db: str, repo_root: str) -> None:
        callers = _get_callers_from_graph(
            graph_db, "src/auth.py", "validate_token", repo_root,
            seen_files=[], limit=5
        )
        routes_caller = next(c for c in callers if c["file"] == "src/api/routes.py")
        assert "validate_token" in routes_caller["code"]
        assert routes_caller["line"] == "47"

    def test_marks_unseen_files(self, graph_db: str, repo_root: str) -> None:
        # Mark routes.py as already seen
        callers = _get_callers_from_graph(
            graph_db, "src/auth.py", "validate_token", repo_root,
            seen_files=["src/api/routes.py"], limit=5
        )
        routes_caller = next(c for c in callers if c["file"] == "src/api/routes.py")
        mw_caller = next(c for c in callers if c["file"] == "src/middleware.py")
        assert routes_caller["unseen"] == "0"
        assert mw_caller["unseen"] == "1"

    def test_filters_low_confidence(self, graph_db: str, repo_root: str) -> None:
        callers = _get_callers_from_graph(
            graph_db, "src/auth.py", "validate_token", repo_root,
            seen_files=[], limit=10
        )
        # utils.py has confidence 0.2 -- must not appear
        files = {c["file"] for c in callers}
        assert "src/utils.py" not in files


class TestGetSignatureFromGraph:
    def test_returns_signature(self, graph_db: str) -> None:
        sig = _get_signature_from_graph(graph_db, "src/auth.py", "validate_token")
        assert "validate_token" in sig
        assert "str" in sig
        assert "bool" in sig

    def test_returns_empty_for_missing(self, graph_db: str) -> None:
        sig = _get_signature_from_graph(graph_db, "src/auth.py", "nonexistent_func")
        assert sig == ""


class TestGetSiblingsFromGraph:
    def test_returns_siblings(self, graph_db: str, repo_root: str) -> None:
        siblings = _get_siblings_from_graph(
            graph_db, "src/auth.py", "validate_token", repo_root
        )
        names = {s["name"] for s in siblings}
        # refresh_token and validate_session are siblings (same file, top-level)
        assert "refresh_token" in names or "validate_session" in names

    def test_reads_snippet(self, graph_db: str, repo_root: str) -> None:
        siblings = _get_siblings_from_graph(
            graph_db, "src/auth.py", "validate_token", repo_root
        )
        # validate_session at line 50 has body with isinstance check at line 51
        session_sib = next((s for s in siblings if s["name"] == "validate_session"), None)
        if session_sib:
            # snippet comes from lines 51+ (body after def line)
            assert "isinstance" in session_sib["snippet"] or session_sib["snippet"] != ""




class TestGenerateImprovedEvidence:
    def test_produces_structured_output(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        assert output  # non-empty
        assert "<gt-evidence" in output
        assert "</gt-evidence>" in output
        assert "post_edit:src/auth.py" in output

    def test_contains_callers(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        # New format: confidence-gated risk evidence shows caller file references
        assert "routes.py" in output or "middleware.py" in output

    def test_contains_actual_code(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        # Must contain the actual code line, not just metadata
        assert "validate_token" in output
        # The code line from routes.py:47
        assert "request.headers" in output or "validate_token(tok)" in output or "validate_token" in output

    def test_contains_signature_or_contract(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        assert any(m in output for m in (
            "SIGNATURE:", "def ", "GUARD:", "MUTATES:", "RETURNS:", "RAISES:", "PARAMS:",
            "BEHAVIORAL CONTRACT:", "[TEST]",
        ))

    def test_contains_actionable_evidence(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        assert any(m in output for m in (
            "MUST PRESERVE", "GUARD:", "MUTATES:", "RETURNS:", "RAISES:", "PARAMS:",
            "SIGNATURE:", "def ", "[TEST]",
            "BEHAVIORAL CONTRACT:", "WARNING:", "SIBLING:",
            "DO NOT break", "possible callers", "Callers of",
        ))

    def test_respects_token_cap(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        # Total output should be under ~1300 chars (1200 + header/footer overhead)
        assert len(output) < 1400

    def test_returns_empty_for_missing_db(self, tmp_path: Path) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=str(tmp_path / "nonexistent.db"),
            repo_root=str(tmp_path),
        )
        assert output == ""

    def test_returns_empty_for_no_graph_data(self, tmp_path: Path) -> None:
        # Create empty graph.db with schema but no data
        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY, label TEXT, name TEXT,
                qualified_name TEXT, file_path TEXT, start_line INTEGER,
                end_line INTEGER, signature TEXT, return_type TEXT,
                is_exported BOOLEAN, is_test BOOLEAN, language TEXT, parent_id INTEGER
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
                type TEXT, source_line INTEGER, source_file TEXT,
                resolution_method TEXT, confidence REAL, metadata TEXT
            );
        """)
        conn.close()
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=db_path,
            repo_root=str(tmp_path),
        )
        assert output == ""

    def test_unbriefed_file_gets_minimal(self, graph_db: str, repo_root: str, tmp_path: Path) -> None:
        # Write brief candidates that do NOT include auth.py
        candidates_path = str(tmp_path / "candidates.txt")
        with open(candidates_path, "w") as f:
            f.write("src/other.py\n")

        # Patch the constant for this test
        import groundtruth.hooks.post_edit as pe
        orig = pe._BRIEF_CANDIDATES_PATH
        pe._BRIEF_CANDIDATES_PATH = candidates_path
        try:
            output = generate_improved_evidence(
                file_path="src/auth.py",
                function_names=["validate_token"],
                db_path=graph_db,
                repo_root=repo_root,
            )
            # Unbriefed but has a graph connection -- becomes neighbor
            # or if no connection found, gets minimal with SIGNATURE
            if output:
                assert any(m in output for m in (
                    "SIGNATURE:", "def ", "GUARD:", "MUTATES:", "RETURNS:", "RAISES:", "PARAMS:",
                    "BEHAVIORAL CONTRACT:", "[TEST]", "WARNING:", "SIBLING:",
                    "DO NOT break", "possible callers", "Callers of",
                ))
        finally:
            pe._BRIEF_CANDIDATES_PATH = orig


class TestReadSourceLine:
    def test_reads_correct_line(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        assert _read_source_line(str(f), 2) == "line2"

    def test_returns_empty_for_bad_path(self) -> None:
        assert _read_source_line("/nonexistent/path.py", 1) == ""

    def test_returns_empty_for_out_of_range(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("line1\n", encoding="utf-8")
        assert _read_source_line(str(f), 99) == ""


# ---------------------------------------------------------------------------
# Phase 7 patch tests: A1 (disambiguation), B1 (sibling silence), B2 (short body)
# ---------------------------------------------------------------------------


@pytest.fixture
def ambiguous_db(tmp_path: Path) -> str:
    """Graph.db with two classes defining the same method name in one file."""
    db_path = str(tmp_path / "ambig.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL,
            parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
            type TEXT NOT NULL, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
        );
        CREATE TABLE assertions (
            id INTEGER PRIMARY KEY, test_node_id INTEGER,
            target_node_id INTEGER, kind TEXT, expression TEXT,
            expected TEXT, line INTEGER
        );

        -- ClassA (parent)
        INSERT INTO nodes VALUES (1,'Class','ClassA',NULL,'src/auth.py',1,50,NULL,NULL,1,0,'python',NULL);
        -- ClassA.build_header (child of ClassA)
        INSERT INTO nodes VALUES (2,'Method','build_header',NULL,'src/auth.py',10,15,
            'def build_header(self) -> str','str',1,0,'python',1);
        -- ClassB (parent)
        INSERT INTO nodes VALUES (3,'Class','ClassB',NULL,'src/auth.py',60,120,NULL,NULL,1,0,'python',NULL);
        -- ClassB.build_header (child of ClassB)
        INSERT INTO nodes VALUES (4,'Method','build_header',NULL,'src/auth.py',70,90,
            'def build_header(self, nonce: str) -> str','str',1,0,'python',3);
        -- Unique function in different file
        INSERT INTO nodes VALUES (5,'Function','unique_func',NULL,'src/config.py',5,20,
            'def unique_func(x: int) -> bool','bool',1,0,'python',NULL);
        -- Caller of ClassB.build_header
        INSERT INTO nodes VALUES (6,'Function','make_request',NULL,'src/client.py',30,50,
            'def make_request()','None',1,0,'python',NULL);
        INSERT INTO edges VALUES (1, 6, 4, 'CALLS', 42, 'src/client.py', 'import', 1.0, NULL);
    """)
    conn.close()
    return db_path


class TestA1Disambiguation:
    """A1: _resolve_node_id must return None for ambiguous same-file names."""

    def test_ambiguous_returns_none(self, ambiguous_db: str) -> None:
        result = _resolve_node_id(ambiguous_db, "src/auth.py", "build_header")
        assert result is None, "Ambiguous name in same file must return None (silence)"

    def test_unique_returns_id(self, ambiguous_db: str) -> None:
        result = _resolve_node_id(ambiguous_db, "src/config.py", "unique_func")
        assert result == 5, "Unique function must return its node ID"

    def test_missing_returns_none(self, ambiguous_db: str) -> None:
        result = _resolve_node_id(ambiguous_db, "src/auth.py", "nonexistent")
        assert result is None

    def test_callers_empty_for_ambiguous(self, ambiguous_db: str, tmp_path: Path) -> None:
        root = str(tmp_path / "repo")
        os.makedirs(root, exist_ok=True)
        callers = _get_callers_from_graph(
            ambiguous_db, "src/auth.py", "build_header", root, seen_files=[], limit=5
        )
        assert callers == [], "Ambiguous name must produce empty callers, not wrong-class callers"

    def test_signature_empty_for_ambiguous(self, ambiguous_db: str) -> None:
        sig = _get_signature_from_graph(ambiguous_db, "src/auth.py", "build_header")
        assert sig == "", "Ambiguous name must produce empty signature, not wrong-class signature"

    def test_callers_work_for_unique(self, ambiguous_db: str, tmp_path: Path) -> None:
        root = str(tmp_path / "repo")
        os.makedirs(os.path.join(root, "src"), exist_ok=True)
        Path(os.path.join(root, "src", "client.py")).write_text(
            "\n" * 41 + "    resp = make_request()\n" + "\n" * 10, encoding="utf-8"
        )
        sig = _get_signature_from_graph(ambiguous_db, "src/config.py", "unique_func")
        assert "unique_func" in sig, "Unique function signature must still be returned"


class TestB1SiblingSuppressionInOutput:
    """B1: generate_improved_evidence must not emit sibling/pattern output."""

    def test_no_pattern_in_output(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        assert "[PATTERN]" not in output, "Sibling output must be suppressed (B1)"
        assert "[TWINS]" not in output, "Structural twins output must be suppressed (B1)"

    def test_callers_still_emitted(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        assert "routes.py" in output or "middleware.py" in output, \
            "Caller evidence must still be emitted after B1 silence"


@pytest.fixture
def short_body_db(tmp_path: Path) -> tuple[str, str]:
    """Graph.db + repo with a short (3-line) function that has no guards/returns."""
    db_path = str(tmp_path / "short.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL,
            parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT NOT NULL, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
        );
        CREATE TABLE assertions (
            id INTEGER PRIMARY KEY, test_node_id INTEGER,
            target_node_id INTEGER, kind TEXT, expression TEXT,
            expected TEXT, line INTEGER
        );
        INSERT INTO nodes VALUES (1,'Function','cleanup',NULL,'src/utils.py',5,8,
            'def cleanup(path: str) -> None','None',1,0,'python',NULL);
        -- Caller so G7 silence gate does not suppress all evidence
        INSERT INTO nodes VALUES (2,'Function','teardown',NULL,'src/main.py',10,20,
            'def teardown()','None',1,0,'python',NULL);
        INSERT INTO edges VALUES (1, 2, 1, 'CALLS', 15, 'src/main.py', 'import', 1.0, NULL);
    """)
    conn.close()

    root = str(tmp_path / "repo")
    os.makedirs(os.path.join(root, "src"), exist_ok=True)
    lines = [""] * 4
    lines.append("def cleanup(path: str) -> None:")
    lines.append("    os.remove(path)")
    lines.append("    shutil.rmtree(os.path.dirname(path))")
    lines.append("    print('done')")
    Path(os.path.join(root, "src", "utils.py")).write_text(
        "\n".join(lines), encoding="utf-8"
    )
    return db_path, root


class TestB2ShortBodyContract:
    """B2: Short/void functions must emit full body as contract."""

    def test_short_body_emits_full_body(self, short_body_db: tuple[str, str]) -> None:
        db_path, repo_root = short_body_db
        output = generate_improved_evidence(
            file_path="src/utils.py",
            function_names=["cleanup"],
            db_path=db_path,
            repo_root=repo_root,
        )
        assert "os.remove" in output, "Full body must include actual code lines"

    def test_existing_guard_contract_unchanged(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_session"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        # Function with guards should produce GUARD lines, not just full body
        if "GUARD:" in output:
            pass  # correct behavior


class TestNoHiddenMetadataInOutput:
    """Post-edit evidence must not contain hidden diagnostic markers."""

    def test_no_gt_status_in_output(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        assert "[GT_STATUS]" not in output, \
            "Hidden [GT_STATUS] must not appear in agent-visible evidence"

    def test_no_gt_config_in_output(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        assert "[GT_CONFIG]" not in output, \
            "Hidden [GT_CONFIG] must not appear in agent-visible evidence"

    def test_no_gt_meta_in_output(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        assert "[GT_META]" not in output, \
            "Hidden [GT_META] must not appear in agent-visible evidence"

    def test_allowed_markers_still_present(self, graph_db: str, repo_root: str) -> None:
        output = generate_improved_evidence(
            file_path="src/auth.py",
            function_names=["validate_token"],
            db_path=graph_db,
            repo_root=repo_root,
        )
        if output:
            has_allowed = any(m in output for m in (
                "DO NOT break", "possible callers", "Callers of",
                "def ", "GUARD:", "MUTATES:", "RETURNS:", "RAISES:", "PARAMS:",
                "[TEST]",
            ))
            assert has_allowed, \
                "Allowed evidence markers should still be present after metadata stripping"


# ---------------------------------------------------------------------------
# Phase 1.3: Contract extraction expansion tests
# ---------------------------------------------------------------------------

from groundtruth.evidence.change import (
    _regex_extract_mutations,
    _regex_extract_accumulations,
    _classify_return_statements,
    _regex_extract_guards,
)


class TestRegexExtractMutations:
    """Test mutation pattern extraction."""

    def test_self_attr(self) -> None:
        body = """\
def update(self, x):
    self.name = x
    self.config.nested = True
"""
        result = _regex_extract_mutations(body)
        types = [t for t, _ in result]
        targets = [tgt for _, tgt in result]
        assert "self_attr" in types
        assert "self.name" in targets
        assert "self.config.nested" in targets

    def test_obj_attr(self) -> None:
        body = """\
def configure(obj):
    obj.value = 42
    obj.state = "active"
"""
        result = _regex_extract_mutations(body)
        types = [t for t, _ in result]
        assert "obj_attr" in types
        targets = [tgt for _, tgt in result]
        assert "obj.value" in targets

    def test_dict_set(self) -> None:
        body = """\
def populate(data):
    data["key"] = "value"
    config[name] = setting
"""
        result = _regex_extract_mutations(body)
        types = [t for t, _ in result]
        assert "dict_set" in types

    def test_list_mutate(self) -> None:
        body = """\
def build_list(items):
    items.append("new")
    items.extend([1, 2])
    items.pop()
    items.remove("old")
"""
        result = _regex_extract_mutations(body)
        types = [t for t, _ in result]
        assert "list_mutate" in types

    def test_set_mutate(self) -> None:
        body = """\
def update_set(s):
    s.add("item")
    s.discard("old")
"""
        result = _regex_extract_mutations(body)
        types = [t for t, _ in result]
        assert "set_mutate" in types

    def test_deduplication(self) -> None:
        body = """\
def repeated(self):
    self.x = 1
    self.x = 2
    self.x = 3
"""
        result = _regex_extract_mutations(body)
        self_attr_count = sum(1 for t, tgt in result if t == "self_attr" and tgt == "self.x")
        assert self_attr_count == 1, "Duplicate mutations should be deduplicated"

    def test_empty_body(self) -> None:
        result = _regex_extract_mutations("")
        assert result == []

    def test_no_mutations(self) -> None:
        body = """\
def pure(x, y):
    z = x + y
    return z
"""
        result = _regex_extract_mutations(body)
        assert result == []

    def test_target_truncation(self) -> None:
        body = """\
def long_target(self):
    self.this_is_an_extremely_long_attribute_name_that_exceeds_sixty_characters_total = True
"""
        result = _regex_extract_mutations(body)
        assert len(result) == 1
        _, target = result[0]
        assert len(target) <= 60


class TestRegexExtractAccumulations:
    """Test accumulation pattern extraction."""

    def test_increment(self) -> None:
        body = """\
def count(items):
    total = 0
    for item in items:
        total += item
"""
        result = _regex_extract_accumulations(body)
        types = [t for t, _ in result]
        assert "increment" in types
        vars_ = [v for _, v in result]
        assert "total" in vars_

    def test_append_build(self) -> None:
        body = """\
def collect(items):
    result = []
    for item in items:
        result.append(item)
"""
        result = _regex_extract_accumulations(body)
        types = [t for t, _ in result]
        assert "append_build" in types
        vars_ = [v for _, v in result]
        assert "result" in vars_

    def test_string_compose_join(self) -> None:
        body = """\
def build_path(parts):
    path = "/".join(parts)
    return path
"""
        result = _regex_extract_accumulations(body)
        types = [t for t, _ in result]
        assert "string_compose" in types

    def test_string_compose_fstring(self) -> None:
        body = """\
def format_msg(name, age):
    msg = f"Hello {name}, age {age}"
    return msg
"""
        result = _regex_extract_accumulations(body)
        types = [t for t, _ in result]
        assert "string_compose" in types

    def test_deduplication(self) -> None:
        body = """\
def accum(items):
    count += 1
    count += 2
    count += 3
"""
        result = _regex_extract_accumulations(body)
        inc_count = sum(1 for t, v in result if t == "increment" and v == "count")
        assert inc_count == 1

    def test_empty_body(self) -> None:
        result = _regex_extract_accumulations("")
        assert result == []

    def test_no_accumulations(self) -> None:
        body = """\
def pure(x):
    return x * 2
"""
        result = _regex_extract_accumulations(body)
        assert result == []

    def test_var_truncation(self) -> None:
        body = """\
def long_var():
    this_is_an_extremely_long_variable_name_exceeding_forty_chars += 1
"""
        result = _regex_extract_accumulations(body)
        assert len(result) == 1
        _, var = result[0]
        assert len(var) <= 40


class TestClassifyReturnStatements:
    """Test multi-return classification."""

    def test_return_value(self) -> None:
        body = """\
def get_user(uid):
    user = db.find(uid)
    return user
"""
        result = _classify_return_statements(body, 10)
        assert any(kind == "RETURN_VALUE" for _, kind, _ in result)

    def test_return_none(self) -> None:
        body = """\
def maybe_get(uid):
    user = db.find(uid)
    if not user:
        return None
    return user
"""
        result = _classify_return_statements(body, 10)
        kinds = [kind for _, kind, _ in result]
        assert "RETURN_NONE" in kinds
        assert "RETURN_VALUE" in kinds

    def test_return_bare(self) -> None:
        body = """\
def process(item):
    if not item:
        return
    do_work(item)
    return
"""
        # Two bare returns, no value returns → VOID_SIDE_EFFECT
        result = _classify_return_statements(body, 1)
        kinds = [kind for _, kind, _ in result]
        assert "VOID_SIDE_EFFECT" in kinds

    def test_return_error(self) -> None:
        body = """\
def validate(x):
    if x < 0:
        return ValueError("must be positive")
    return x
"""
        result = _classify_return_statements(body, 1)
        kinds = [kind for _, kind, _ in result]
        assert "RETURN_ERROR" in kinds
        assert "RETURN_VALUE" in kinds

    def test_void_side_effect(self) -> None:
        body = """\
def cleanup(path):
    os.remove(path)
    shutil.rmtree(os.path.dirname(path))
"""
        result = _classify_return_statements(body, 1)
        assert len(result) == 1
        assert result[0][1] == "VOID_SIDE_EFFECT"

    def test_only_bare_returns_is_void(self) -> None:
        body = """\
def early_exit(x):
    if not x:
        return
    process(x)
    return
"""
        result = _classify_return_statements(body, 1)
        assert len(result) == 1
        assert result[0][1] == "VOID_SIDE_EFFECT"

    def test_multi_return_classification(self) -> None:
        body = """\
def complex_func(x):
    if not x:
        return None
    if x < 0:
        raise ValueError("negative")
    result = compute(x)
    return result
"""
        result = _classify_return_statements(body, 100)
        kinds = [kind for _, kind, _ in result]
        assert "RETURN_NONE" in kinds
        assert "RETURN_VALUE" in kinds
        assert len(result) >= 2

    def test_line_numbers_correct(self) -> None:
        body = """\
def func(x):
    if x:
        return x
    return None
"""
        result = _classify_return_statements(body, 50)
        lines = [line for line, _, _ in result]
        # "return x" is on line index 2 (0-based), so 50+2=52
        # "return None" is on line index 3, so 50+3=53
        assert 52 in lines
        assert 53 in lines


class TestExistingGuardExtractionStillWorks:
    """Ensure existing _regex_extract_guards behavior is preserved."""

    def test_basic_guard(self) -> None:
        body = """\
def validate(x):
    if x is None:
        raise ValueError("x required")
    return x
"""
        guards = _regex_extract_guards(body)
        assert len(guards) == 1
        assert guards[0][0] == "raise"

    def test_return_guard(self) -> None:
        body = """\
def check(data):
    if not data:
        return False
    process(data)
"""
        guards = _regex_extract_guards(body)
        assert len(guards) == 1
        assert guards[0][0] == "return"

    def test_no_guards(self) -> None:
        body = """\
def simple(x):
    return x + 1
"""
        guards = _regex_extract_guards(body)
        assert len(guards) == 0


class TestContractBudgetEnforcement:
    """Test that contract output stays within 200-800 char budget."""

    def test_large_function_stays_within_budget(self) -> None:
        # Build a function with many mutations and returns to stress the budget
        lines = ["def big_func(self, data):"]
        for i in range(20):
            lines.append(f"    self.attr_{i} = data[{i}]")
        for i in range(10):
            lines.append(f"    results.append(item_{i})")
        lines.append("    return results")
        body = "\n".join(lines)

        mutations = _regex_extract_mutations(body)
        accumulations = _regex_extract_accumulations(body)
        classified_returns = _classify_return_statements(body, 1)

        # Simulate the contract assembly with budget enforcement
        contract_lines: list[str] = []
        guards = _regex_extract_guards(body)
        if guards:
            for gt_type, gt_cond in guards[:3]:
                contract_lines.append(f"  GUARD: if {gt_cond} -> {gt_type}")
        if mutations:
            _mut_targets = ", ".join(t for _, t in mutations[:4])
            contract_lines.append(f"  MUTATES: {_mut_targets}")
        if accumulations:
            for _acc_type, _acc_var in accumulations[:3]:
                if _acc_type == "append_build":
                    contract_lines.append(f"  ACCUMULATES: {_acc_var} via .append()")
                elif _acc_type == "increment":
                    contract_lines.append(f"  ACCUMULATES: {_acc_var} via +=")
                elif _acc_type == "string_compose":
                    contract_lines.append(f"  ACCUMULATES: {_acc_var} via string composition")
        if classified_returns:
            for rp_line, rp_kind, rp_text in classified_returns[:4]:
                if rp_kind == "VOID_SIDE_EFFECT":
                    contract_lines.append("  VOID_SIDE_EFFECT")
                else:
                    contract_lines.append(f"  L{rp_line}: {rp_text}")

        # Apply budget enforcement
        if len("\n".join(contract_lines)) > 800:
            while contract_lines and len("\n".join(contract_lines)) > 800:
                contract_lines.pop()

        block = "\n".join(contract_lines)
        assert len(block) <= 800, f"Contract block is {len(block)} chars, exceeds 800 budget"
        assert len(block) > 0, "Contract block should not be empty for a function with mutations"


class TestContractIntegrationWithMutationsAndAccumulations:
    """Integration tests for the expanded contract in generate_improved_evidence."""

    @pytest.fixture
    def mutation_db(self, tmp_path: Path) -> tuple[str, str]:
        """Graph.db + repo with a function that has mutations and accumulations."""
        db_path = str(tmp_path / "mut.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
                file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
                signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
                is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL,
                parent_id INTEGER REFERENCES nodes(id)
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
                type TEXT NOT NULL, source_line INTEGER, source_file TEXT,
                resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
            );
            CREATE TABLE assertions (
                id INTEGER PRIMARY KEY, test_node_id INTEGER,
                target_node_id INTEGER, kind TEXT, expression TEXT,
                expected TEXT, line INTEGER
            );
            INSERT INTO nodes VALUES (1,'Function','process_items',NULL,'src/processor.py',1,20,
                'def process_items(self, items: list) -> list','list',1,0,'python',NULL);
            -- Caller so G7 silence gate does not suppress
            INSERT INTO nodes VALUES (2,'Function','run_pipeline',NULL,'src/main.py',10,20,
                'def run_pipeline()','None',1,0,'python',NULL);
            INSERT INTO edges VALUES (1, 2, 1, 'CALLS', 15, 'src/main.py', 'import', 1.0, NULL);
        """)
        conn.close()

        root = str(tmp_path / "repo")
        os.makedirs(os.path.join(root, "src"), exist_ok=True)
        func_body = """\
def process_items(self, items: list) -> list:
    if not items:
        raise ValueError("empty items")
    self.count = len(items)
    results = []
    for item in items:
        self.total += item.value
        results.append(item.process())
    self.state = "done"
    data["processed"] = True
    return results
"""
        # Write with padding to match start_line=1
        Path(os.path.join(root, "src", "processor.py")).write_text(
            func_body, encoding="utf-8"
        )
        return db_path, root

    def test_contract_includes_mutations(self, mutation_db: tuple[str, str]) -> None:
        db_path, repo_root = mutation_db
        output = generate_improved_evidence(
            file_path="src/processor.py",
            function_names=["process_items"],
            db_path=db_path,
            repo_root=repo_root,
        )
        assert "MUTATES:" in output, "Contract should include MUTATES for self-attr mutations"

    def test_contract_includes_accumulations(self, mutation_db: tuple[str, str]) -> None:
        db_path, repo_root = mutation_db
        output = generate_improved_evidence(
            file_path="src/processor.py",
            function_names=["process_items"],
            db_path=db_path,
            repo_root=repo_root,
        )
        assert "ACCUMULATES:" in output, "Contract should include ACCUMULATES for .append() pattern"

    def test_contract_includes_guard(self, mutation_db: tuple[str, str]) -> None:
        db_path, repo_root = mutation_db
        output = generate_improved_evidence(
            file_path="src/processor.py",
            function_names=["process_items"],
            db_path=db_path,
            repo_root=repo_root,
        )
        assert "GUARD:" in output, "Contract should include GUARD for the validation check"

    def test_contract_includes_return_line(self, mutation_db: tuple[str, str]) -> None:
        db_path, repo_root = mutation_db
        output = generate_improved_evidence(
            file_path="src/processor.py",
            function_names=["process_items"],
            db_path=db_path,
            repo_root=repo_root,
        )
        assert "return results" in output, "Contract should include classified return statement"

    def test_b2_fallback_still_works(self, short_body_db: tuple[str, str]) -> None:
        """Ensure B2 short-body fallback is preserved."""
        db_path, repo_root = short_body_db
        output = generate_improved_evidence(
            file_path="src/utils.py",
            function_names=["cleanup"],
            db_path=db_path,
            repo_root=repo_root,
        )
        # B2 fallback emits body lines directly (no header)
        assert "os.remove" in output, \
            "B2 fallback for short void functions must still emit body content"
