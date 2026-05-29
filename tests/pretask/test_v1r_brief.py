"""Tests for V1R brief generator."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from groundtruth.pretask.v1r_brief import (
    FileEntry,
    _top_functions,
    _test_files_for,
    _caller_contract_for_file,
    render_brief,
    generate_v1r_brief,
)


# --- TTD: categorical correct-or-quiet caller gate (wire.md #2) -------------
# Frozen artifact (beancount-931 canary): the v1r brief showed stdlib `os.walk`
# (caller find_files) as a confident `Callers:` fact of beancount's
# `account.walk`. The edge was a single-candidate name_match scored 0.9, which
# the old `confidence >= 0.9` gate laundered as a verified caller. These tests
# reproduce that artifact and assert name_match is NEVER rendered as a fact,
# while a genuinely deterministic (import/same_file) edge still is.
_WALK_SCHEMA = """
    CREATE TABLE nodes (
        id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
        file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
        return_type TEXT, is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
        language TEXT DEFAULT 'python', parent_id INTEGER
    );
    CREATE TABLE edges (
        id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
        source_line INTEGER, source_file TEXT, resolution_method TEXT,
        confidence REAL DEFAULT 0.5, metadata TEXT
    );
    INSERT INTO nodes (id, label, name, file_path, is_test) VALUES
        (1, 'Function', 'walk', 'beancount/core/account.py', 0),
        (2, 'Function', 'find_files', 'beancount/scripts/directories.py', 0),
        (3, 'Function', 'load', 'beancount/loader.py', 0);
"""


def _walk_db(tmp_path: Path, edges: list[tuple[int, int, str, float, int]]) -> tuple[str, str]:
    """Build the account.walk graph + on-disk repo. edges =
    (source_id, target_id, resolution_method, confidence, source_line)."""
    repo = tmp_path / "repo"
    (repo / "beancount" / "core").mkdir(parents=True, exist_ok=True)
    (repo / "beancount" / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "beancount" / "core" / "account.py").write_text(
        "def walk(root):\n    pass\n", encoding="utf-8"
    )
    (repo / "beancount" / "scripts" / "directories.py").write_text(
        "    for r in os.walk(path):\n", encoding="utf-8"
    )
    (repo / "beancount" / "loader.py").write_text(
        "    return account.walk(root)\n", encoding="utf-8"
    )
    db_path = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_WALK_SCHEMA)
    for src, tgt, method, conf, line in edges:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, "
            "resolution_method, confidence) VALUES (?,?,'CALLS',?,?,?)",
            (src, tgt, line, method, conf),
        )
    conn.commit()
    conn.close()
    return db_path, str(repo)


def test_caller_namematch_never_laundered_as_fact(tmp_path: Path) -> None:
    """name_match @ 0.9 must NOT render as `find_files() in ...` (the os.walk bug).

    RED before the categorical-gate fix (old gate matched confidence>=0.9 and
    rendered the function-name fact); GREEN after.
    """
    db, repo = _walk_db(tmp_path, [(2, 1, "name_match", 0.9, 1)])
    out = _caller_contract_for_file(db, "beancount/core/account.py", repo, ["walk"])
    assert "() in " not in out, f"name_match laundered as a caller fact: {out!r}"
    assert "find_files()" not in out
    # 0.9 >= floor -> shown only as an unverified location hint, no name claim.
    assert out == "" or "(unverified)" in out


def test_caller_import_edge_is_a_fact(tmp_path: Path) -> None:
    """A deterministic (import) edge IS rendered as a confident caller fact —
    regression guard that the fix did not over-suppress real callers."""
    db, repo = _walk_db(tmp_path, [(3, 1, "import", 1.0, 1)])
    out = _caller_contract_for_file(db, "beancount/core/account.py", repo, ["walk"])
    assert "load() in beancount/loader.py:1" in out
    assert "(unverified)" not in out


def test_caller_namematch_below_floor_suppressed(tmp_path: Path) -> None:
    """name_match below _NAME_MATCH_FLOOR (0.5) is suppressed entirely."""
    db, repo = _walk_db(tmp_path, [(2, 1, "name_match", 0.3, 1)])
    out = _caller_contract_for_file(db, "beancount/core/account.py", repo, ["walk"])
    assert out == ""


def test_caller_fact_wins_over_unverified(tmp_path: Path) -> None:
    """With both a deterministic and a name_match caller present, only the fact
    is shown — the unverified guess is never mixed in beside a verified caller."""
    db, repo = _walk_db(
        tmp_path,
        [(2, 1, "name_match", 0.9, 1), (3, 1, "import", 1.0, 1)],
    )
    out = _caller_contract_for_file(db, "beancount/core/account.py", repo, ["walk"])
    assert "load() in beancount/loader.py:1" in out
    assert "find_files()" not in out
    assert "(unverified)" not in out


def test_caller_stdlib_shadow_dropped_even_when_tagged_deterministic(tmp_path: Path) -> None:
    """RUN VERDICT (beancount-931 canary 26619606504): the os.walk callsite was
    name-matched to project account.walk and the edge carried a DETERMINISTIC
    resolution_method, so the provenance gate trusted it and rendered the laundered
    fact. The stdlib-shadow guard must drop it regardless of provenance.

    find_files (directories.py line 1 = `for r in os.walk(path):`) -> walk, tagged
    'import' (deterministic). RED before the guard (renders find_files() fact),
    GREEN after (dropped -> empty).
    """
    db, repo = _walk_db(tmp_path, [(2, 1, "import", 1.0, 1)])
    out = _caller_contract_for_file(db, "beancount/core/account.py", repo, ["walk"])
    assert out == "", f"stdlib os.walk shadow rendered as a caller: {out!r}"
    assert "find_files()" not in out


def test_caller_project_caller_not_dropped_by_stdlib_guard(tmp_path: Path) -> None:
    """Regression guard: a REAL project caller (account.walk via import) whose code
    is `return account.walk(root)` must NOT be dropped — 'account' is not stdlib."""
    db, repo = _walk_db(tmp_path, [(3, 1, "import", 1.0, 1)])
    out = _caller_contract_for_file(db, "beancount/core/account.py", repo, ["walk"])
    assert "load() in beancount/loader.py:1" in out


def test_caller_old_schema_renders_unverified_not_suppressed(tmp_path: Path) -> None:
    """Old graph.db with NO confidence/resolution_method columns: cross-file callers
    must render as `file:line (unverified)` hints, not be suppressed entirely.
    Regression guard for the categorical-gate rewrite (review C2/C3/C4): the old
    code had an explicit fallback that rendered file:line; the rewrite dropped it.
    RED before the `not has_conf` fix, GREEN after."""
    repo = tmp_path / "repo"
    (repo / "beancount" / "core").mkdir(parents=True)
    (repo / "tools").mkdir(parents=True)
    (repo / "beancount" / "core" / "account.py").write_text(
        "def compute(x):\n    pass\n", encoding="utf-8"
    )
    (repo / "tools" / "x.py").write_text("    z = compute(val)\n", encoding="utf-8")
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,
                            file_path TEXT, is_test INTEGER DEFAULT 0);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT,
                            type TEXT, source_line INT);
        INSERT INTO nodes (id, label, name, file_path, is_test) VALUES
            (1, 'Function', 'compute', 'beancount/core/account.py', 0),
            (2, 'Function', 'build_path', 'tools/x.py', 0);
        INSERT INTO edges (id, source_id, target_id, type, source_line)
            VALUES (1, 2, 1, 'CALLS', 1);
        """
    )
    conn.commit()
    conn.close()
    out = _caller_contract_for_file(db, "beancount/core/account.py", str(repo), ["compute"])
    assert "(unverified)" in out, f"old-schema caller suppressed instead of hinted: {out!r}"
    assert "tools/x.py:1" in out


# --- TTD: <gt-graph-map> wiring into the live brief (wire.md #1) -------------


def test_render_brief_appends_graph_map(tmp_path: Path) -> None:
    """render_brief(graph_db=...) appends a <gt-graph-map> sibling block.

    RED before wiring (render_brief had no graph_db param -> TypeError);
    GREEN after. The map carries the import-resolved caller as a fact.
    """
    db, _repo = _walk_db(tmp_path, [(3, 1, "import", 1.0, 1)])
    files = [
        FileEntry(
            path="beancount/core/account.py",
            score=0.9,
            functions=["walk"],
            function_names=["walk"],
            test_mappings=["tests/test_account.py"],
        )
    ]
    out = render_brief(files, graph_db=db)
    assert "<gt-graph-map>" in out
    assert "</gt-graph-map>" in out
    assert "load" in out  # the import-resolved caller surfaced in the map


def test_render_brief_no_graph_map_without_db() -> None:
    """No graph_db -> no <gt-graph-map> (backward-compatible default)."""
    files = [
        FileEntry(
            path="beancount/core/account.py",
            score=0.9,
            functions=["walk"],
            function_names=["walk"],
            test_mappings=["tests/test_account.py"],
        )
    ]
    out = render_brief(files)
    assert "<gt-graph-map>" not in out


def test_render_brief_graph_map_quiet_when_no_confident_edge(tmp_path: Path) -> None:
    """Correct-or-quiet: a name_match-only edge below floor yields no map block."""
    db, _repo = _walk_db(tmp_path, [(2, 1, "name_match", 0.2, 1)])
    files = [
        FileEntry(
            path="beancount/core/account.py",
            score=0.9,
            functions=["walk"],
            function_names=["walk"],
            test_mappings=["tests/test_account.py"],
        )
    ]
    out = render_brief(files, graph_db=db)
    assert "<gt-graph-map>" not in out


@patch("groundtruth.pretask.v1r_brief.run_v74")
def test_generate_v1r_brief_carries_graph_map_no_laundering(
    mock_v74: MagicMock, tmp_path: Path
) -> None:
    """Full pipeline E2E: generate_v1r_brief threads graph_db into render_brief,
    so the final brief_text carries <gt-graph-map> built from the SAME db, and a
    name_match caller is never laundered as a fact anywhere in the brief.

    The db has both a real import caller (load -> walk, fact) and the os.walk
    name_match artifact (find_files -> walk, 0.9). The import caller must surface
    as a fact; find_files() must never appear as a confident caller.
    """
    db, repo = _walk_db(
        tmp_path,
        [(3, 1, "import", 1.0, 1), (2, 1, "name_match", 0.9, 1)],
    )
    mock_v74.return_value = MagicMock(
        ranked_full=[
            {"path": "beancount/core/account.py", "score": 0.9, "components": {"path": 0.0}}
        ]
    )
    result = generate_v1r_brief("fix account walk traversal", repo, db)
    assert "<gt-graph-map>" in result.brief_text
    assert "load" in result.brief_text  # real import caller surfaced
    assert "find_files() in" not in result.brief_text  # name_match never laundered


@pytest.fixture
def graph_db(tmp_path: Path) -> str:
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
            language TEXT NOT NULL DEFAULT 'python',
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
            confidence REAL DEFAULT 0.5,
            metadata TEXT
        );
        INSERT INTO nodes (id, label, name, file_path, is_test) VALUES
            (1, 'Function', 'login_user', 'src/auth/handler.py', 0),
            (2, 'Function', 'verify_token', 'src/auth/handler.py', 0),
            (3, 'Function', 'test_login', 'tests/test_auth.py', 1),
            (4, 'Function', 'test_verify', 'tests/test_auth.py', 1),
            (5, 'Function', 'require_auth', 'src/auth/middleware.py', 0);
        INSERT INTO edges (source_id, target_id, type, confidence) VALUES
            (3, 1, 'CALLS', 1.0),
            (4, 2, 'CALLS', 1.0),
            (5, 2, 'CALLS', 1.0);
    """)
    conn.close()
    return db_path


def test_top_functions(graph_db: str) -> None:
    funcs = _top_functions(graph_db, "src/auth/handler.py")
    assert "verify_token" in funcs
    assert "login_user" in funcs
    assert len(funcs) <= 3


def test_top_functions_returns_by_ref_count(graph_db: str) -> None:
    funcs = _top_functions(graph_db, "src/auth/handler.py")
    assert funcs[0] == "verify_token"


def test_test_files_for(graph_db: str) -> None:
    tests = _test_files_for(graph_db, "src/auth/handler.py")
    assert "tests/test_auth.py" in tests


def test_test_files_empty_for_unknown(graph_db: str) -> None:
    tests = _test_files_for(graph_db, "nonexistent.py")
    assert tests == []


def test_render_brief_no_prose() -> None:
    # Tier-as-filter revert (commit 11aab174, v1r_brief.py:697-716): tiers are an
    # internal filter, [INFO] entries are dropped. Both entries here carry graph
    # evidence (test mapping -> [WARNING], or contract -> [VERIFIED]) so they
    # survive the filter and both render.
    files = [
        FileEntry(
            path="src/auth/handler.py",
            score=0.9,
            functions=["login_user", "verify_token"],
            test_mappings=["tests/test_auth.py"],
        ),
        FileEntry(
            path="src/auth/middleware.py",
            score=0.7,
            functions=["require_auth"],
            contract="login_user() in src/auth/handler.py:1 `require_auth()`",
        ),
    ]
    text = render_brief(files)
    assert text.startswith("<gt-task-brief>")
    assert text.endswith("</gt-task-brief>")
    assert "login_user" in text
    assert "require_auth" in text
    assert "Tests: tests/test_auth.py" in text
    # No in-band tier labels and no prose directives in agent-facing output.
    for forbidden in [
        "[VERIFIED]",
        "[WARNING]",
        "[INFO]",
        "justification",
        "constraint",
        "CONSTRAINT",
        "mirror",
        "scaffold",
        "editing elsewhere",
        "Edit existing",
        "Do not",
        "IMPLEMENTATION",
        "PATTERN",
        "CONTRACT",
        "SIDE FILES",
    ]:
        assert forbidden not in text, f"Brief must not contain prose/tier label: '{forbidden}'"


def test_render_brief_numbered() -> None:
    # Tier-as-filter revert (commit 11aab174): only entries with graph evidence
    # survive. Both entries get a test mapping ([WARNING] tier) so both render
    # and the numbered "N. path" format is exercised.
    files = [
        FileEntry(path="a.py", score=1.0, functions=["foo"], test_mappings=["tests/test_a.py"]),
        FileEntry(path="b.py", score=0.5, functions=["bar"], test_mappings=["tests/test_b.py"]),
    ]
    text = render_brief(files)
    assert "1. a.py" in text
    assert "2. b.py" in text


@patch("groundtruth.pretask.v1r_brief.run_v74")
def test_generate_v1r_brief_empty_on_no_signal(mock_v74: MagicMock) -> None:
    mock_v74.return_value = MagicMock(ranked_full=[])
    result = generate_v1r_brief("fix auth bug", "/repo", "/db.sqlite")
    assert result.files == []
    assert "<gt-task-brief>" in result.brief_text


@patch("groundtruth.pretask.v1r_brief.run_v74")
@patch("groundtruth.pretask.v1r_brief._top_functions", return_value=[])
@patch("groundtruth.pretask.v1r_brief._test_files_for", return_value=[])
def test_generate_v1r_brief_emits_low_score_candidates(_t, _f, mock_v74: MagicMock) -> None:
    mock_v74.return_value = MagicMock(ranked_full=[{"path": "a.py", "score": 0.1}])
    result = generate_v1r_brief("fix auth bug", "/repo", "/db.sqlite")
    assert len(result.files) == 1
    assert result.files[0].path == "a.py"
    assert "1. a.py" in result.brief_text


@patch("groundtruth.pretask.v1r_brief.run_v74")
@patch("groundtruth.pretask.v1r_brief._top_functions", return_value=["foo"])
@patch("groundtruth.pretask.v1r_brief._test_files_for", return_value=["tests/test_a.py"])
def test_generate_v1r_brief_respects_max_files(_mock_tests, _mock_funcs, mock_v74) -> None:
    mock_v74.return_value = MagicMock(
        ranked_full=[{"path": f"file{i}.py", "score": 0.9 - i * 0.1} for i in range(10)]
    )
    result = generate_v1r_brief("fix bug", "/repo", "/db.sqlite", max_files=3)
    assert len(result.files) <= 3


@patch("groundtruth.pretask.v1r_brief.run_v74")
@patch("groundtruth.pretask.v1r_brief._top_functions", return_value=["foo"])
@patch("groundtruth.pretask.v1r_brief._test_files_for", return_value=[])
def test_generate_v1r_brief_token_cap(_mock_tests, _mock_funcs, mock_v74) -> None:
    mock_v74.return_value = MagicMock(
        ranked_full=[{"path": f"very/long/path/to/file{i}.py", "score": 0.9} for i in range(10)]
    )
    result = generate_v1r_brief("fix bug", "/repo", "/db.sqlite", max_brief_tokens=100)
    assert result.token_estimate <= 100


@pytest.fixture
def sparse_graph_db(tmp_path: Path) -> str:
    """Graph DB with < 2 edges per file — triggers sparse mode."""
    db_path = str(tmp_path / "sparse.db")
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
            language TEXT NOT NULL DEFAULT 'python',
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
            confidence REAL DEFAULT 0.5,
            metadata TEXT
        );
        INSERT INTO nodes (id, label, name, file_path, is_test) VALUES
            (1, 'Function', 'parse_url', 'src/urls.py', 0),
            (2, 'Function', 'validate', 'src/validator.py', 0),
            (3, 'Function', 'render_page', 'src/render.py', 0),
            (4, 'Function', 'test_parse', 'tests/test_urls.py', 1);
        INSERT INTO edges (source_id, target_id, type, confidence) VALUES
            (4, 1, 'CALLS', 1.0);
    """)
    conn.close()
    return db_path


@patch("groundtruth.pretask.v1r_brief.run_v74")
@patch("groundtruth.pretask.v1r_brief._top_functions", return_value=[])
@patch("groundtruth.pretask.v1r_brief._test_files_for", return_value=[])
def test_sparse_graph_no_suppression(
    _mock_tests, _mock_funcs, mock_v74, sparse_graph_db: str
) -> None:
    """On sparse graphs, modulus gate must NOT suppress the brief."""
    mock_v74.return_value = MagicMock(
        ranked_full=[
            {"path": "src/urls.py", "score": 0.8, "components": {"path": 0.0}},
            {"path": "src/validator.py", "score": 0.7, "components": {"path": 0.0}},
            {"path": "src/render.py", "score": 0.6, "components": {"path": 0.0}},
        ]
    )
    result = generate_v1r_brief("fix url parsing bug", "/repo", sparse_graph_db)
    assert result.brief_text != ""
    assert len(result.files) > 0


@patch("groundtruth.pretask.v1r_brief.run_v74")
@patch("groundtruth.pretask.v1r_brief._top_functions", return_value=[])
@patch("groundtruth.pretask.v1r_brief._test_files_for", return_value=[])
def test_path_match_preservation(_mock_tests, _mock_funcs, mock_v74, tmp_path: Path) -> None:
    """Files with strong path-name match must survive into top-5 even if BM25-outranked."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT,
            qualified_name TEXT, file_path TEXT, start_line INTEGER,
            end_line INTEGER, signature TEXT, return_type TEXT,
            is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
            language TEXT DEFAULT 'python', parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.5, metadata TEXT
        );
        INSERT INTO nodes (id, label, name, file_path, is_test) VALUES
            (1, 'Function', 'foo', 'src/hub1.py', 0),
            (2, 'Function', 'bar', 'src/hub2.py', 0),
            (3, 'Function', 'baz', 'src/hub3.py', 0),
            (4, 'Function', 'qux', 'src/hub4.py', 0),
            (5, 'Function', 'quux', 'src/hub5.py', 0),
            (6, 'Function', 'color_apply', 'src/colorama.py', 0);
        INSERT INTO edges (source_id, target_id, type, confidence) VALUES
            (1, 2, 'CALLS', 1.0),
            (2, 3, 'CALLS', 1.0),
            (3, 4, 'CALLS', 1.0);
    """)
    conn.close()

    mock_v74.return_value = MagicMock(
        ranked_full=[
            {"path": "src/hub1.py", "score": 0.9, "components": {"path": 0.0}},
            {"path": "src/hub2.py", "score": 0.85, "components": {"path": 0.0}},
            {"path": "src/hub3.py", "score": 0.80, "components": {"path": 0.0}},
            {"path": "src/hub4.py", "score": 0.75, "components": {"path": 0.0}},
            {"path": "src/hub5.py", "score": 0.70, "components": {"path": 0.0}},
            {"path": "src/colorama.py", "score": 0.30, "components": {"path": 0.7}},
        ]
    )
    result = generate_v1r_brief("fix colorama color rendering issue", "/repo", db_path, max_files=5)
    paths = [f.path for f in result.files]
    assert "src/colorama.py" in paths, f"Path-matched file should survive into brief, got: {paths}"
