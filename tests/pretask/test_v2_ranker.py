"""Unit tests for Track B v2 ranker."""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from groundtruth.pretask.v2_ranker import (
    _bm25_score_global,
    _bm25_score_per_file,
    _find_callees_set,
    _rrf_score,
    _score_caller_prox,
    _tokenize,
    rank,
    rank_files,
    rank_functions,
)
from groundtruth.pretask.v2_types import (
    HighSignalToken,
    QueryObject,
    RankedFile,
    RankedFunction,
    RankedResults,
)


@pytest.fixture
def tiny_db(tmp_path: Path) -> str:
    """Minimal Go-indexer-shaped graph.db with a few functions and edges."""
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
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
    nodes = [
        (1, "Function", "parse_expr", None, "src/parser.py", 1, 5, "def parse_expr(tokens):", None, 1, 0, "python", None),
        (2, "Function", "tokenize", None, "src/tokens.py", 1, 5, "def tokenize(text):", None, 1, 0, "python", None),
        (3, "Function", "build_ast", None, "src/ast.py", 1, 5, "def build_ast(src):", None, 1, 0, "python", None),
        (4, "Class", "Token", None, "src/tokens.py", 7, 12, None, None, 1, 0, "python", None),
    ]
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path, "
        "start_line, end_line, signature, return_type, is_exported, "
        "is_test, language, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        nodes,
    )
    edges = [
        (1, 2, "CALLS", 3, "src/parser.py", "import", 0.9),
        (3, 1, "CALLS", 3, "src/ast.py", "import", 0.9),
        (3, 2, "CALLS", 4, "src/ast.py", "name_match", 0.3),
    ]
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence) VALUES (?,?,?,?,?,?,?)",
        edges,
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def tiny_repo(tmp_path: Path) -> str:
    """Layout matching tiny_db: src/parser.py, src/tokens.py, src/ast.py."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "parser.py").write_text(textwrap.dedent("""
        def parse_expr(tokens):
            t = tokenize(tokens)
            return Token(t)
    """))
    (src / "tokens.py").write_text(textwrap.dedent("""
        def tokenize(text):
            return text.split()

        class Token:
            pass
    """))
    (src / "ast.py").write_text(textwrap.dedent("""
        def build_ast(src):
            return parse_expr(src)
    """))
    return str(tmp_path)


def test_rank_files_empty_query(tiny_repo: str, tiny_db: str) -> None:
    q = QueryObject()
    result = rank_files(q, tiny_repo, tiny_db)
    assert result == []


def test_bm25_inline_smoke() -> None:
    docs = [
        _tokenize("parse_expr tokenize tokens"),
        _tokenize("build_ast node tree"),
        _tokenize("tokenize text split words"),
    ]
    query = [("parse_expr", 4.0), ("tokenize", 2.0)]
    scores = _bm25_score_per_file(docs, query)
    assert len(scores) == 3
    assert scores[0] == max(scores)
    assert max(scores) == 1.0


def test_bm25_empty_query() -> None:
    docs = [_tokenize("hello world")]
    assert _bm25_score_per_file(docs, []) == [0.0]


def test_bm25_zero_match_returns_zeros() -> None:
    docs = [_tokenize("alpha beta"), _tokenize("gamma delta")]
    scores = _bm25_score_per_file(docs, [("zzz_no_match", 1.0)])
    assert scores == [0.0, 0.0]


def test_caller_prox_uses_confidence_filter(tiny_db: str) -> None:
    # function id=2 (tokenize). callers: parser.py (conf 0.9, kept), ast.py (conf 0.3, dropped).
    file_to_rank = {"src/parser.py": 1, "src/ast.py": 2}
    score = _score_caller_prox(2, tiny_db, file_to_rank)
    assert score == 0.3

    # Now lower the high-confidence edge below the floor; expect no boost.
    conn = sqlite3.connect(tiny_db)
    conn.execute("UPDATE edges SET confidence=0.4 WHERE source_id=1 AND target_id=2")
    conn.commit()
    conn.close()
    assert _score_caller_prox(2, tiny_db, file_to_rank) == 0.0


def test_caller_prox_top10_vs_top50(tiny_db: str) -> None:
    # confidence=0.9 caller in rank 5 -> 0.3
    score_top10 = _score_caller_prox(2, tiny_db, {"src/parser.py": 5, "src/ast.py": 99})
    assert score_top10 == 0.3
    # caller in rank 25 -> 0.1
    score_top50 = _score_caller_prox(2, tiny_db, {"src/parser.py": 25, "src/ast.py": 99})
    assert score_top50 == 0.1
    # caller outside top-50 -> 0
    score_outside = _score_caller_prox(2, tiny_db, {"src/parser.py": 100, "src/ast.py": 99})
    assert score_outside == 0.0


def test_rank_functions_direct_hit_dominates(tiny_repo: str, tiny_db: str) -> None:
    files = [
        RankedFile(file="src/parser.py", score=0.5),
        RankedFile(file="src/tokens.py", score=0.5),
        RankedFile(file="src/ast.py", score=0.5),
    ]
    q = QueryObject(
        function_hints=["parse_expr"],
        high_signal_tokens=[HighSignalToken("tokenize", 4.0, "stack_trace")],
        raw_text="parse_expr crashes",
    )
    funcs = rank_functions(q, files, tiny_repo, tiny_db)
    assert funcs[0].function == "parse_expr"
    assert funcs[0].components["direct"] > 0
    for fn in funcs[1:]:
        assert fn.components["direct"] == 0.0
        assert fn.score < funcs[0].score


def test_rank_functions_top100_cap(tmp_path: Path) -> None:
    db = tmp_path / "big.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT,
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (label, name, file_path, start_line, end_line, signature, language) "
        "VALUES (?,?,?,?,?,?,?)",
        [("Function", f"func_{i}", "src/big.py", 1, 2, f"def func_{i}():", "python") for i in range(150)],
    )
    conn.commit()
    conn.close()

    files = [RankedFile(file="src/big.py", score=0.5)]
    q = QueryObject(
        raw_text="anything",
        high_signal_tokens=[HighSignalToken("func", 1.0, "snake_case")],
    )
    with patch("groundtruth.pretask.v2_ranker._read_body_snippet", return_value=""):
        funcs = rank_functions(q, files, str(tmp_path), str(db))
    assert len(funcs) == 100


def test_rank_returns_ranked_results(tiny_repo: str, tiny_db: str) -> None:
    q = QueryObject(
        file_hints=["parser.py"],
        function_hints=["parse_expr"],
        high_signal_tokens=[HighSignalToken("parse_expr", 4.0, "stack_trace")],
        raw_text="parse_expr crashes when given empty token list",
    )
    result = rank(q, tiny_repo, tiny_db)
    assert isinstance(result, RankedResults)
    assert len(result.files) > 0
    assert all(isinstance(f, RankedFile) for f in result.files)
    assert all(isinstance(fn, RankedFunction) for fn in result.functions)


def test_rank_files_passes_through_v74_paths(tiny_repo: str, tiny_db: str) -> None:
    q = QueryObject(
        function_hints=["parse_expr"],
        raw_text="parse_expr issue",
    )
    files = rank_files(q, tiny_repo, tiny_db)
    assert files
    paths = {f.file for f in files}
    assert any(p.endswith("parser.py") for p in paths)


def test_rrf_score_handles_all_zero_signals() -> None:
    signals = {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.0]}
    assert _rrf_score(signals) == [0.0, 0.0, 0.0]


def test_rrf_score_higher_signal_higher_rrf() -> None:
    signals = {"sig": [1.0, 2.0, 3.0]}
    out = _rrf_score(signals, k=60)
    assert out[2] > out[1] > out[0] > 0


def test_rrf_score_skips_zero_score_in_signal() -> None:
    signals = {"sig": [1.0, 0.0, 2.0]}
    out = _rrf_score(signals, k=60)
    assert out[1] == 0.0
    assert out[2] > out[0] > 0


def test_rrf_score_weights_amplify_signal() -> None:
    signals_unweighted = {"a": [1.0, 0.5], "b": [0.5, 1.0]}
    out_eq = _rrf_score(signals_unweighted, k=60, weights={"a": 1.0, "b": 1.0})
    assert abs(out_eq[0] - out_eq[1]) < 1e-9
    out_aw = _rrf_score(signals_unweighted, k=60, weights={"a": 2.0, "b": 1.0})
    assert out_aw[0] > out_aw[1]


def test_find_callees_set_filters_by_confidence(tiny_db: str) -> None:
    callees = _find_callees_set([1], tiny_db)
    assert callees == {2}
    callees = _find_callees_set([3], tiny_db)
    assert callees == {1}
    assert _find_callees_set([99], tiny_db) == set()
    assert _find_callees_set([], tiny_db) == set()


def test_callee_prop_signal_in_components(tiny_repo: str, tiny_db: str) -> None:
    files = [
        RankedFile(file="src/parser.py", score=0.5),
        RankedFile(file="src/tokens.py", score=0.5),
        RankedFile(file="src/ast.py", score=0.5),
    ]
    q = QueryObject(
        function_hints=["parse_expr"],
        high_signal_tokens=[HighSignalToken("parse_expr", 4.0, "stack_trace")],
        raw_text="parse_expr crashes when called",
    )
    funcs = rank_functions(q, files, tiny_repo, tiny_db)
    by_name = {f.function: f for f in funcs}
    assert "callee_prop" in by_name["parse_expr"].components
    assert by_name["tokenize"].components["callee_prop"] == 1.0
    assert by_name["build_ast"].components["callee_prop"] == 0.0


# -----------------------------------------------------------------------------
# v2.2-6 multi-hop callee BFS tests
# -----------------------------------------------------------------------------

def _make_chain_db(tmp_path: Path, edges: list[tuple[int, int, float]]) -> str:
    """Build a minimal graph.db with three Function nodes (ids 1/2/3) and the
    given edges. Each edge is (source_id, target_id, confidence)."""
    db = tmp_path / "chain.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT,
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, signature, language) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, "Function", "A", "src/m.py", 1, 2, "def A():", "python"),
            (2, "Function", "B", "src/m.py", 3, 4, "def B():", "python"),
            (3, "Function", "C", "src/m.py", 5, 6, "def C():", "python"),
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence) VALUES (?,?,?,?,?,?,?)",
        [(s, t, "CALLS", 1, "src/m.py", "import", c) for s, t, c in edges],
    )
    conn.commit()
    conn.close()
    return str(db)


def test_multihop_reach_depth1_2_3(tmp_path: Path) -> None:
    """3-node chain A->B->C, all edges confidence>=0.5.
    depth=1 returns {B}; depth=2 returns {B,C}; depth=3 returns {B,C}."""
    db = _make_chain_db(tmp_path, [(1, 2, 0.9), (2, 3, 0.9)])
    assert _find_callees_set([1], db, depth=1) == {2}
    assert _find_callees_set([1], db, depth=2) == {2, 3}
    assert _find_callees_set([1], db, depth=3) == {2, 3}


def test_multihop_confidence_gate_blocks_low_conf_hop(tmp_path: Path) -> None:
    """A->B (0.9), B->C (0.3). depth=2 must return {B} only — C is gated out."""
    db = _make_chain_db(tmp_path, [(1, 2, 0.9), (2, 3, 0.3)])
    assert _find_callees_set([1], db, depth=2) == {2}
    assert _find_callees_set([1], db, depth=3) == {2}


def test_multihop_cycle_does_not_infinite_loop(tmp_path: Path) -> None:
    """A->B, B->A cycle. depth=3 must terminate. Per spec, callees from
    source {A} must include {B} at minimum; A may also appear since the
    cycle bounces back. The critical property is termination + B reached."""
    db = _make_chain_db(tmp_path, [(1, 2, 0.9), (2, 1, 0.9)])
    callees = _find_callees_set([1], db, depth=3)
    assert 2 in callees  # B always reached
    assert callees.issubset({1, 2})  # nothing spurious; cycle terminated


def test_multihop_default_depth_is_single_hop(tmp_path: Path) -> None:
    """Depth defaults to 1 (no kwarg) — preserves v2.1 behavior."""
    db = _make_chain_db(tmp_path, [(1, 2, 0.9), (2, 3, 0.9)])
    assert _find_callees_set([1], db) == {2}


# -----------------------------------------------------------------------------
# v2.2-7 global function-level BM25 tests
# -----------------------------------------------------------------------------

def test_global_bm25_cross_file_ordering_short_doc_wins() -> None:
    """Two functions in two different files, both contain 'foo' once.
    Doc A has 5 tokens, Doc B has 50 tokens. Per-file normalization
    rates both 1.0; global BM25 with avgdl normalization gives shorter
    doc A a higher raw and normalized score than B."""
    short_doc = ["foo", "x", "y", "z", "w"]  # 5 tokens, "foo" once
    long_doc = ["foo"] + ["w"] * 49  # 50 tokens, "foo" once
    docs = [short_doc, long_doc]
    query = [("foo", 1.0)]

    # Per-file BM25 returns 1.0 for each (each file is its own normalization
    # universe in the per-file path).
    per_file_a = _bm25_score_per_file([short_doc], query)
    per_file_b = _bm25_score_per_file([long_doc], query)
    assert per_file_a == [1.0]
    assert per_file_b == [1.0]

    # Global BM25 ranks short_doc strictly above long_doc.
    global_scores = _bm25_score_global(docs, query)
    assert len(global_scores) == 2
    assert global_scores[0] > global_scores[1]
    # Max-normalization: best doc is exactly 1.0.
    assert global_scores[0] == 1.0


def test_global_bm25_empty_inputs() -> None:
    assert _bm25_score_global([], [("foo", 1.0)]) == []
    assert _bm25_score_global([["foo"]], []) == [0.0]


def test_global_bm25_zero_match_returns_zeros() -> None:
    docs = [_tokenize("alpha beta"), _tokenize("gamma delta")]
    assert _bm25_score_global(docs, [("zzz_no_match", 1.0)]) == [0.0, 0.0]

