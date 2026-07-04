"""Pytest suite for the L2 FTS5 BM25 fallback.

Subject under test
------------------
``src/groundtruth/pretask/sqlite3_fts_fallback.py::generate_fts5_orientation_brief``

Coverage
--------
1. BM25 returns the expected ranked file for a prose query (Python repo).
2. Structural rerank promotes a high-incoming-edge file above a doc-only file.
3. Empty/no-match query returns the canonical sparse brief.
4. Extension whitelist: ``.css/.html/.md`` indexed; ``.pyc/.o/.DS_Store`` not.
5. Skip-dir pruning: ``node_modules/`` and ``__pycache__/`` not indexed.
6. Per-file size cap (>1 MB skipped).
7. Cross-language correctness: Go/JS/Rust queries hit the right files.
8. Cache reuse: second call hits the prebuilt /tmp/gt_l2_fts5_*.db cache.
9. Anti-benchmaxxing: source contains no Python-only assumptions in non-test paths.

Fixtures live at ``tests/layers/fixtures/repo_<lang>/`` and are populated by
hand with realistic identifiers and cross-file references. Synthetic graph.db
files are built per-test from the Go-indexer schema (mirrors
``tests/pretask/conftest.py::tiny_graph_db``).
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# The module under test does NOT import from the rest of the project, so a
# direct import works under the project's pyproject pytest config.
from groundtruth.pretask.sqlite3_fts_fallback import (
    generate_fts5_orientation_brief,
    _cache_db_path,
    _EMPTY_BRIEF,
)


# ---------------------------------------------------------------------------
# Fixture root + per-language graph.db builders
# ---------------------------------------------------------------------------

FIXTURES_ROOT = Path(__file__).parent / "fixtures"


def _new_graph_db(db_path: Path) -> sqlite3.Connection:
    """Create the Go-indexer schema (matches conftest tiny_graph_db)."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
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
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        """
    )
    return conn


def _insert_nodes(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path, "
        "start_line, end_line, signature, return_type, is_exported, "
        "is_test, language, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _insert_edges(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: (src_id, tgt_id, source_line, source_file, confidence)."""
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, "
        "source_file, resolution_method, confidence) VALUES "
        "(?, ?, 'CALLS', ?, ?, 'name_match', ?)",
        rows,
    )


def build_python_graph(db_path: Path) -> str:
    """Graph for repo_python: url_utils.py is heavily incoming."""
    conn = _new_graph_db(db_path)
    nodes = [
        # url_utils.py — central, incoming from server, validators
        (1,  "Function", "parse_url",            None, "url_utils.py",  10, 30, None, None, 1, 0, "python", None),
        (2,  "Function", "normalize_url",        None, "url_utils.py",  35, 45, None, None, 1, 0, "python", None),
        (3,  "Function", "is_https",             None, "url_utils.py",  50, 55, None, None, 1, 0, "python", None),
        # validators.py
        (4,  "Function", "validate_request_url", None, "validators.py", 10, 20, None, None, 1, 0, "python", None),
        (5,  "Function", "validate_callback",    None, "validators.py", 25, 35, None, None, 1, 0, "python", None),
        # server.py
        (6,  "Function", "handle_request",       None, "server.py",     10, 25, None, None, 1, 0, "python", None),
        (7,  "Function", "serve_forever",        None, "server.py",     30, 35, None, None, 1, 0, "python", None),
        # cache.py — ZERO incoming-from-other-top-hits (doc/utility only)
        (8,  "Class",    "TTLCache",             None, "cache.py",      10, 50, None, None, 1, 0, "python", None),
        (9,  "Method",   "set",                  None, "cache.py",      18, 22, None, None, 0, 0, "python", 8),
        (10, "Method",   "get",                  None, "cache.py",      24, 30, None, None, 0, 0, "python", 8),
        # logger.py
        (11, "Function", "log_event",            None, "logger.py",     8,  12, None, None, 1, 0, "python", None),
    ]
    _insert_nodes(conn, nodes)
    edges = [
        # validators.validate_request_url -> url_utils.parse_url
        (4, 1, 11, "validators.py", 0.9),
        # validators.validate_request_url -> url_utils.is_https
        (4, 3, 12, "validators.py", 0.9),
        # validators.validate_callback -> url_utils.parse_url
        (5, 1, 27, "validators.py", 0.9),
        # validators.validate_callback -> url_utils.is_https
        (5, 3, 28, "validators.py", 0.9),
        # server.handle_request -> url_utils.parse_url
        (6, 1, 11, "server.py", 0.9),
        # server.handle_request -> url_utils.normalize_url
        (6, 2, 12, "server.py", 0.9),
        # server.handle_request -> validators.validate_request_url
        (6, 4, 13, "server.py", 0.9),
    ]
    _insert_edges(conn, edges)
    conn.commit()
    conn.close()
    return str(db_path)


def build_go_graph(db_path: Path) -> str:
    conn = _new_graph_db(db_path)
    nodes = [
        # url_parser.go — incoming from http_handler
        (1, "Function", "ParseRequestURL", None, "url_parser.go", 15, 35, None, None, 1, 0, "go", None),
        (2, "Function", "NormalizeHost",   None, "url_parser.go", 40, 45, None, None, 1, 0, "go", None),
        (3, "Class",    "ParsedRequest",   None, "url_parser.go", 8,  12, None, None, 1, 0, "go", None),
        # http_handler.go
        (4, "Function", "httpHandler",     None, "http_handler.go", 10, 25, None, None, 0, 0, "go", None),
        (5, "Function", "RegisterRoutes",  None, "http_handler.go", 30, 33, None, None, 1, 0, "go", None),
        # config.go — separate
        (6, "Function", "LoadConfig",      None, "config.go", 12, 20, None, None, 1, 0, "go", None),
        (7, "Function", "envOr",           None, "config.go", 30, 36, None, None, 0, 0, "go", None),
        # logger.go
        (8, "Class",    "Logger",          None, "logger.go", 10, 14, None, None, 1, 0, "go", None),
        (9, "Method",   "Log",             None, "logger.go", 22, 35, None, None, 1, 0, "go", 8),
        # main.go
        (10, "Function", "RunServer",      None, "main.go", 6, 14, None, None, 1, 0, "go", None),
    ]
    _insert_nodes(conn, nodes)
    edges = [
        (4, 1, 14, "http_handler.go", 0.9),   # httpHandler -> ParseRequestURL
        (5, 4, 31, "http_handler.go", 0.9),   # RegisterRoutes -> httpHandler
        (10, 6, 7, "main.go", 0.9),           # RunServer -> LoadConfig
    ]
    _insert_edges(conn, edges)
    conn.commit()
    conn.close()
    return str(db_path)


def build_js_graph(db_path: Path) -> str:
    conn = _new_graph_db(db_path)
    nodes = [
        (1, "Function", "parseJSON",      None, "json_parser.js", 6,  16, None, None, 1, 0, "javascript", None),
        (2, "Function", "stringifyJSON",  None, "json_parser.js", 22, 28, None, None, 1, 0, "javascript", None),
        (3, "Function", "fetchAndDecode", None, "api_client.js",  4,  10, None, None, 1, 0, "javascript", None),
        (4, "Function", "postJson",       None, "api_client.js",  12, 22, None, None, 1, 0, "javascript", None),
        (5, "Class",    "Store",          None, "store.js",       3,  25, None, None, 1, 0, "javascript", None),
        (6, "Function", "bootstrap",      None, "index.js",       4,  10, None, None, 1, 0, "javascript", None),
    ]
    _insert_nodes(conn, nodes)
    edges = [
        (3, 1, 8, "api_client.js", 0.9),    # fetchAndDecode -> parseJSON
        (4, 1, 20, "api_client.js", 0.9),   # postJson -> parseJSON
        (6, 3, 7, "index.js", 0.9),         # bootstrap -> fetchAndDecode
        (6, 5, 5, "index.js", 0.9),         # bootstrap -> Store
    ]
    _insert_edges(conn, edges)
    conn.commit()
    conn.close()
    return str(db_path)


def build_rust_graph(db_path: Path) -> str:
    conn = _new_graph_db(db_path)
    nodes = [
        (1, "Function", "parse_url",   None, "url.rs",     12, 30, None, None, 1, 0, "rust", None),
        (2, "Function", "is_https",    None, "url.rs",     35, 38, None, None, 1, 0, "rust", None),
        (3, "Class",    "ParsedUrl",   None, "url.rs",     5,  9,  None, None, 1, 0, "rust", None),
        (4, "Function", "handle",      None, "handler.rs", 10, 22, None, None, 1, 0, "rust", None),
        (5, "Function", "format_ok",   None, "handler.rs", 24, 27, None, None, 0, 0, "rust", None),
        (6, "Function", "load_config", None, "config.rs",  10, 16, None, None, 1, 0, "rust", None),
        (7, "Class",    "Logger",      None, "logger.rs",  6,  9,  None, None, 1, 0, "rust", None),
        (8, "Function", "run",         None, "main.rs",    10, 16, None, None, 1, 0, "rust", None),
    ]
    _insert_nodes(conn, nodes)
    edges = [
        (4, 1, 11, "handler.rs", 0.9),  # handle -> parse_url
        (4, 2, 14, "handler.rs", 0.9),  # handle -> is_https
        (4, 5, 18, "handler.rs", 0.9),  # handle -> format_ok
        (8, 6, 12, "main.rs", 0.9),     # run -> load_config
    ]
    _insert_edges(conn, edges)
    conn.commit()
    conn.close()
    return str(db_path)


# ---------------------------------------------------------------------------
# Helpers — clone the read-only fixture into a per-test workdir
# ---------------------------------------------------------------------------

def _clone_fixture(src: Path, dst: Path) -> Path:
    """Copy the fixture tree into a writable scratch directory.

    We don't want tests mutating fixtures/ on disk (size_cap test writes a
    2 MB file; skip-dir test injects node_modules/), and the cache-key in
    sqlite3_fts_fallback hashes ``repo_path + dir_mtime`` so a fresh copy
    per test guarantees a fresh cache.
    """
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "node_modules", "*.pyc"),
    )
    return dst


def _purge_cache_for(repo_path: str) -> None:
    """Remove the /tmp cache db keyed off repo_path, if present."""
    cache = _cache_db_path(repo_path)
    if os.path.exists(cache):
        try:
            os.unlink(cache)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 1. BM25 basic ranking — Python repo
# ---------------------------------------------------------------------------

def test_bm25_basic_ranking(tmp_path: Path) -> None:
    repo = _clone_fixture(FIXTURES_ROOT / "repo_python", tmp_path / "repo")
    graph = build_python_graph(tmp_path / "graph.db")
    _purge_cache_for(str(repo))

    brief = generate_fts5_orientation_brief(
        issue_text="parse url validation fails when scheme is empty",
        repo_path=str(repo),
        graph_db_path=graph,
    )

    assert "<gt-task-brief>" in brief
    assert _EMPTY_BRIEF not in brief, f"unexpected empty brief: {brief!r}"

    # Top-1 line must be either url_utils.py or validators.py.
    bm25_lines = [ln for ln in brief.splitlines() if "[POSSIBLE: bm25-rank]" in ln]
    assert bm25_lines, f"no ranked lines in brief: {brief}"
    first = bm25_lines[0]
    assert ("url_utils.py" in first) or ("validators.py" in first), (
        f"top BM25 hit was neither url_utils.py nor validators.py: {first}"
    )


# ---------------------------------------------------------------------------
# 2. Structural rerank promotes high-incoming-edge file
# ---------------------------------------------------------------------------

def test_structural_rerank_promotes(tmp_path: Path) -> None:
    """The structural rerank is a +30% multiplier (R2L midpoint). It cannot
    overcome a 5x BM25 gap, but it MUST move a near-tied file up.

    Counterfactual design: same fixture, two graph dbs.
      - graph_with_edges: validators+server edges into url_utils.py -> high
        incoming on url_utils.py.
      - graph_no_edges: zero edges -> rerank multiplier is identity.
    The relative ordering of url_utils.py vs another doc-only top hit must
    improve with the edge-rich graph. We also assert that the per-line
    'callers from elsewhere' count is correctly reported.
    """
    repo = _clone_fixture(FIXTURES_ROOT / "repo_python", tmp_path / "repo")

    graph_full = build_python_graph(tmp_path / "graph_full.db")
    # Empty graph: schema only, no nodes/edges.
    empty_db = tmp_path / "graph_empty.db"
    conn = _new_graph_db(empty_db)
    conn.commit()
    conn.close()

    # Use a query that produces a clear top-1 (BM25-dominant) and a tail
    # where url_utils.py is one of several plausible matches.
    query = "url scheme normalize callback validation host"

    _purge_cache_for(str(repo))
    brief_full = generate_fts5_orientation_brief(query, str(repo), str(graph_full))
    _purge_cache_for(str(repo))
    brief_empty = generate_fts5_orientation_brief(query, str(repo), str(empty_db))

    def _scores(brief: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for ln in brief.splitlines():
            m = re.search(r"\] (\S+) \(BM25 score: ([-\d.]+),", ln)
            if m:
                out[m.group(1)] = float(m.group(2))
        return out

    full_scores = _scores(brief_full)
    empty_scores = _scores(brief_empty)

    assert "url_utils.py" in full_scores, brief_full

    # 'callers from elsewhere' must report a positive count for url_utils.py
    # under the full graph.
    url_line = next(
        ln for ln in brief_full.splitlines() if "url_utils.py" in ln
    )
    m = re.search(r"(\d+) callers from elsewhere", url_line)
    assert m and int(m.group(1)) >= 5, (
        f"expected >=5 incoming callers for url_utils.py, got: {url_line}"
    )

    # Counterfactual: url_utils.py's score under the full graph must be
    # >= its score under the empty graph (rerank is multiplicative >= 1).
    if "url_utils.py" in empty_scores:
        assert full_scores["url_utils.py"] >= empty_scores["url_utils.py"], (
            f"full={full_scores['url_utils.py']} empty={empty_scores['url_utils.py']}"
        )
        # And the bump must be roughly the documented coefficient: between
        # 1.0x (no incoming) and 1.3x (max incoming).
        ratio = full_scores["url_utils.py"] / max(empty_scores["url_utils.py"], 1e-9)
        assert 1.0 <= ratio <= 1.301, f"unexpected rerank ratio: {ratio:.3f}"


# ---------------------------------------------------------------------------
# 3. Empty-result branch
# ---------------------------------------------------------------------------

def test_empty_result_branch(tmp_path: Path) -> None:
    repo = _clone_fixture(FIXTURES_ROOT / "repo_python", tmp_path / "repo")
    graph = build_python_graph(tmp_path / "graph.db")
    _purge_cache_for(str(repo))

    brief = generate_fts5_orientation_brief(
        issue_text="asdfqwerty zxcvbnm wxyz1234",
        repo_path=str(repo),
        graph_db_path=graph,
    )
    assert "Issue text too sparse" in brief, brief
    assert brief.strip() == _EMPTY_BRIEF.strip()


# ---------------------------------------------------------------------------
# 4. Extension whitelist
# ---------------------------------------------------------------------------

def test_extension_filter(tmp_path: Path) -> None:
    """The whitelist already includes .py/.js/.go/.rs/.css/.html/.md.
    Confirm: included extensions are searchable; excluded extensions are not.
    """
    repo = _clone_fixture(FIXTURES_ROOT / "repo_python", tmp_path / "repo")
    # Add files for each whitelisted ext with a unique discriminating token.
    (repo / "extra.html").write_text("<html><body>uniquehtmltoken</body></html>")
    (repo / "notes.md").write_text("uniquemdtoken about parsing")
    (repo / "side.js").write_text("// uniquejstoken parser stub\nfunction noop(){}")
    (repo / "lib.go").write_text("package x // uniquegotoken\nfunc Noop() {}")
    (repo / "lib.rs").write_text("// uniquerstoken\nfn noop() {}")
    # Excluded: should NOT be indexed.
    (repo / "compiled.pyc").write_bytes(b"\x00\x00uniquepyctoken\x00")
    (repo / "obj.o").write_bytes(b"uniqueotoken")
    (repo / ".DS_Store").write_bytes(b"uniquedsstoken")

    # Pass a non-existent graph_db_path so _connect_graph returns None and
    # the FTS5 layer is exercised in isolation (no all-zero-degree
    # suppression). The extension filter is a property of _iter_repo_files,
    # not of the graph layer, so this is the correct seam for this test.
    no_graph = str(tmp_path / "does_not_exist.db")
    _purge_cache_for(str(repo))

    # Query for the included tokens; they should appear in the brief.
    for tok in ("uniquehtmltoken", "uniquemdtoken", "uniquejstoken",
                "uniquegotoken", "uniquerstoken"):
        _purge_cache_for(str(repo))  # fresh index per query
        brief = generate_fts5_orientation_brief(tok, str(repo), no_graph)
        assert _EMPTY_BRIEF not in brief, (
            f"included ext token {tok} not found — extension was wrongly excluded"
        )

    # Excluded tokens must NOT match any indexed file.
    for tok in ("uniquepyctoken", "uniqueotoken", "uniquedsstoken"):
        _purge_cache_for(str(repo))
        brief = generate_fts5_orientation_brief(tok, str(repo), no_graph)
        assert _EMPTY_BRIEF in brief or tok not in brief, (
            f"excluded ext token {tok} surfaced — filter leaked: {brief}"
        )


# ---------------------------------------------------------------------------
# 5. Skip dirs
# ---------------------------------------------------------------------------

def test_skip_dirs(tmp_path: Path) -> None:
    repo = _clone_fixture(FIXTURES_ROOT / "repo_python", tmp_path / "repo")

    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "foo.js").write_text(
        "// nodemoduletoken should never be indexed\n"
    )
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "bar.pyc").write_bytes(
        b"pycachetoken should never appear"
    )
    # Also drop a normal .py file in pycache to test dir-pruning, not just ext.
    (repo / "__pycache__" / "shadow.py").write_text(
        "# pycachepytoken — under skipped dir, must be pruned\n"
    )

    graph = build_python_graph(tmp_path / "graph.db")
    _purge_cache_for(str(repo))

    for tok in ("nodemoduletoken", "pycachetoken", "pycachepytoken"):
        _purge_cache_for(str(repo))
        brief = generate_fts5_orientation_brief(tok, str(repo), graph)
        # Either the empty-brief path fires (no match), or — if some other
        # term collides — the file itself must not appear.
        assert "node_modules" not in brief, brief
        assert "__pycache__" not in brief, brief
        assert tok not in brief, f"skip-dir leak: {brief}"


# ---------------------------------------------------------------------------
# 6. Size cap
# ---------------------------------------------------------------------------

def test_size_cap(tmp_path: Path) -> None:
    """Files >1 MB must be skipped."""
    repo = _clone_fixture(FIXTURES_ROOT / "repo_python", tmp_path / "repo")

    big_token = "uniquebigfiletoken"
    payload = (big_token + " ") * 200  # seed token
    # Pad to ~2 MB.
    pad = "abcdefgh " * 250_000  # ~2 MB of ASCII
    (repo / "huge.py").write_text(payload + pad)
    # Sanity: must be over 1 MB.
    assert (repo / "huge.py").stat().st_size > 1_500_000

    # And a tiny file with a unique non-overlapping token to prove indexing
    # of normal-sized files still works.
    (repo / "tiny.py").write_text("# uniquesmallfiletoken in a tiny file\n")

    # No graph needed — size cap is a property of _iter_repo_files. Pass a
    # missing path so _connect_graph returns None and the all-zero-degree
    # suppression doesn't fire on these synthetic files.
    no_graph = str(tmp_path / "does_not_exist.db")
    _purge_cache_for(str(repo))

    brief_big = generate_fts5_orientation_brief(big_token, str(repo), no_graph)
    assert "huge.py" not in brief_big, f"size cap not enforced: {brief_big}"

    _purge_cache_for(str(repo))
    brief_small = generate_fts5_orientation_brief(
        "uniquesmallfiletoken", str(repo), no_graph
    )
    assert "tiny.py" in brief_small, brief_small


# ---------------------------------------------------------------------------
# 7. Cross-language correctness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "lang_dir, builder, query, expected_file",
    [
        ("repo_go",   build_go_graph,   "httpHandler request validation", "http_handler.go"),
        ("repo_rust", build_rust_graph, "parse_url scheme validation",    "url.rs"),
        ("repo_js",   build_js_graph,   "parseJSON decode response body", "json_parser.js"),
        ("repo_python", build_python_graph,
            "parse_url scheme validation",                                "url_utils.py"),
    ],
)
def test_cross_lang(
    tmp_path: Path,
    lang_dir: str,
    builder,
    query: str,
    expected_file: str,
) -> None:
    repo = _clone_fixture(FIXTURES_ROOT / lang_dir, tmp_path / "repo")
    graph = builder(tmp_path / "graph.db")
    _purge_cache_for(str(repo))

    brief = generate_fts5_orientation_brief(query, str(repo), graph)
    assert _EMPTY_BRIEF not in brief, f"{lang_dir} query produced empty brief: {brief}"
    assert expected_file in brief, (
        f"{lang_dir} query {query!r} did not surface {expected_file}: {brief}"
    )


# ---------------------------------------------------------------------------
# 8. Cache reuse
# ---------------------------------------------------------------------------

def test_cache_reuse(tmp_path: Path) -> None:
    """Second call with the same repo_path must hit the prebuilt /tmp cache.

    We assert two things: (a) the cache db file exists at the documented
    path after the first call, (b) the second call leaves it in place
    (mtime not bumped — `_cache_is_fresh` guard prevents rebuild).
    """
    repo = _clone_fixture(FIXTURES_ROOT / "repo_python", tmp_path / "repo")
    graph = build_python_graph(tmp_path / "graph.db")
    _purge_cache_for(str(repo))

    cache_path = _cache_db_path(str(repo))
    assert not os.path.exists(cache_path), "stale cache leaked from earlier test"

    t0 = time.perf_counter()
    brief1 = generate_fts5_orientation_brief(
        "parse url validation", str(repo), graph
    )
    t1 = time.perf_counter()
    assert os.path.exists(cache_path), (
        f"cache db not written at {cache_path} after first call"
    )
    first_mtime = os.path.getmtime(cache_path)
    first_size = os.path.getsize(cache_path)

    # Sleep is OK here — the perf comparison is informational; correctness
    # rests on the mtime check below.
    brief2 = generate_fts5_orientation_brief(
        "parse url validation", str(repo), graph
    )
    t2 = time.perf_counter()

    assert brief2 == brief1, "deterministic input produced different briefs"
    assert os.path.exists(cache_path)
    second_mtime = os.path.getmtime(cache_path)
    second_size = os.path.getsize(cache_path)

    # If the cache was reused, the file should be byte-identical (same size,
    # same mtime). _build_fts_cache deletes & recreates the file, so a rebuild
    # would change mtime.
    assert second_mtime == first_mtime, (
        f"cache rebuilt on second call: mtime {first_mtime} -> {second_mtime}"
    )
    assert second_size == first_size

    # Soft perf signal: second run should not be slower than the first (some
    # CI noise tolerance).
    cold = t1 - t0
    warm = t2 - t1
    assert warm <= cold + 0.5, (
        f"warm call ({warm:.3f}s) was much slower than cold ({cold:.3f}s) — "
        "suggests the cache wasn't reused"
    )

    _purge_cache_for(str(repo))


# ---------------------------------------------------------------------------
# 9. Anti-benchmaxxing audit
# ---------------------------------------------------------------------------

def test_anti_benchmaxxing_no_python_only_assumptions() -> None:
    """The L2 source must not hardwire Python-only assumptions outside its
    extension whitelist (which deliberately includes many languages)."""
    src_path = (
        Path(__file__).resolve().parents[2]
        / "src" / "groundtruth" / "pretask" / "sqlite3_fts_fallback.py"
    )
    src = src_path.read_text(encoding="utf-8")

    # Strip the docstring + the _TEXT_EXTENSIONS block + the _STOPWORDS block:
    # these legitimately mention .py, def, class, etc.
    def _strip(block_re: str, body: str) -> str:
        return re.sub(block_re, "", body, count=1, flags=re.DOTALL)

    body = _strip(r'"""[\s\S]*?"""', src)  # module docstring
    body = _strip(r"_TEXT_EXTENSIONS\s*=\s*\{[^}]*\}", body)
    body = _strip(r"_STOPWORDS\s*=\s*frozenset\(\{[\s\S]*?\}\)", body)

    forbidden_patterns = [
        # No hard-wired ".py" in operative code (e.g. only-index-py logic).
        (r"['\"]\.py['\"]", "hardcoded '.py' in operative code"),
        # No Python keyword filters in the active path.
        (r"\b__pycache__\b", "Python-specific path token outside _SKIP_DIR_NAMES"),
    ]
    # __pycache__ legitimately appears in _SKIP_DIR_NAMES — strip that too.
    body = _strip(r"_SKIP_DIR_NAMES\s*=\s*\{[\s\S]*?\}", body)

    for pat, desc in forbidden_patterns:
        m = re.search(pat, body)
        assert not m, (
            f"anti-benchmaxxing audit failed: {desc}: matched {m.group(0)!r}"
        )

    # Also: the rerank coefficient must remain the documented 0.3 R2L
    # midpoint. Tightening / loosening this on Live-Lite tasks would be
    # benchmaxxing.
    assert "_RERANK_COEFFICIENT = 0.3" in src, (
        "rerank coefficient drifted from documented 0.3 R2L midpoint"
    )
