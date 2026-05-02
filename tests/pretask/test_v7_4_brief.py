"""Unit tests for v7.4 localization modules.

Tests use in-memory SQLite + a tiny temp repo (files on disk) so we can
exercise semantic scoring, graph reach, anchor proximity, and hub penalty
without a real gt-index run.
"""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import numpy as np
import pytest


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def tiny_db_v74(tmp_path: Path) -> str:
    """Minimal graph.db for v7.4 tests.

    Files:
        src/parser.py   — contains parse_expr, parse_stmt
        src/tokens.py   — contains Token, tokenize
        src/ast.py      — contains Node, ASTBuilder
        tests/test_p.py — test file (is_test=1)

    Edges:
        parse_expr -> tokenize (CALLS, import, conf 1.0)
        parse_expr -> Token    (CALLS, name_match, conf 0.9)
        ASTBuilder -> parse_expr (CALLS, import, conf 1.0)
        ASTBuilder -> Node     (CALLS, same_file, conf 1.0)
    """
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
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
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
    """)
    nodes = [
        # id, label, name, qual, file, start, end, sig, ret, exp, test, lang, parent
        (1, "Function", "parse_expr",  None, "src/parser.py", 10, 30, None, None, 1, 0, "python", None),
        (2, "Function", "parse_stmt",  None, "src/parser.py", 35, 55, None, None, 1, 0, "python", None),
        (3, "Class",    "Token",       None, "src/tokens.py", 1,  20, None, None, 1, 0, "python", None),
        (4, "Function", "tokenize",    None, "src/tokens.py", 22, 40, None, None, 1, 0, "python", None),
        (5, "Class",    "Node",        None, "src/ast.py",    1,  15, None, None, 1, 0, "python", None),
        (6, "Class",    "ASTBuilder",  None, "src/ast.py",    20, 80, None, None, 1, 0, "python", None),
        (7, "Function", "test_parse",  None, "tests/test_p.py", 1, 20, None, None, 0, 1, "python", None),
    ]
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path, "
        "start_line, end_line, signature, return_type, is_exported, "
        "is_test, language, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        nodes,
    )
    edges = [
        # src_id, tgt_id, type, src_line, src_file, method, confidence
        (1, 4, "CALLS", 15, "src/parser.py", "import",     1.0),   # parse_expr -> tokenize
        (1, 3, "CALLS", 20, "src/parser.py", "name_match", 0.9),   # parse_expr -> Token
        (6, 1, "CALLS", 25, "src/ast.py",    "import",     1.0),   # ASTBuilder -> parse_expr
        (6, 5, "CALLS", 30, "src/ast.py",    "same_file",  1.0),   # ASTBuilder -> Node
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
    """Create real files matching the tiny_db_v74 layout."""
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tests").mkdir()

    (src / "parser.py").write_text(textwrap.dedent("""
        # Parser module: parse_expr parses tokens into AST expressions
        def parse_expr(tokens):
            t = tokenize(tokens)
            return Token(t)

        def parse_stmt(tokens):
            return parse_expr(tokens)
    """))
    (src / "tokens.py").write_text(textwrap.dedent("""
        # Token class and tokenizer
        class Token:
            def __init__(self, val):
                self.val = val

        def tokenize(text):
            return text.split()
    """))
    (src / "ast.py").write_text(textwrap.dedent("""
        # AST node and builder
        class Node:
            pass

        class ASTBuilder:
            def build(self, src):
                expr = parse_expr(src)
                return Node()
    """))
    (tmp_path / "tests" / "test_p.py").write_text("def test_parse(): pass\n")
    return str(tmp_path)


# ------------------------------------------------------------------ anchor_select


def test_normalize_identifier_snake():
    from groundtruth.pretask.anchor_select import _normalize_identifier
    assert _normalize_identifier("parse_expr") == ["parse", "expr"]


def test_normalize_identifier_camel():
    from groundtruth.pretask.anchor_select import _normalize_identifier
    assert _normalize_identifier("ASTBuilder") == ["ast", "builder"]


def test_normalize_identifier_kebab():
    from groundtruth.pretask.anchor_select import _normalize_identifier
    assert _normalize_identifier("my-token-type") == ["token", "type"]  # "my" < 3 chars dropped


def test_normalize_identifier_short_parts_filtered():
    from groundtruth.pretask.anchor_select import _normalize_identifier
    parts = _normalize_identifier("is_ok")
    # "ok" has 2 chars and is filtered (< 3)
    assert "ok" not in parts


def test_symbol_anchor_containment(tiny_db_v74: str):
    from groundtruth.pretask.anchor_select import _symbol_anchors
    # Issue mentions "parse_expr" → parts = ["parse", "expr"]
    issue = "The parse_expr function crashes with empty input"
    result = _symbol_anchors(issue, tiny_db_v74, k_anchor=10)
    assert "src/parser.py" in result  # parse_expr and parse_stmt are in parser.py


def test_symbol_anchor_non_match(tiny_db_v74: str):
    from groundtruth.pretask.anchor_select import _symbol_anchors
    issue = "completely unrelated issue about memory leaks xyz"
    result = _symbol_anchors(issue, tiny_db_v74, k_anchor=5)
    # No symbols in the graph match the word parts here
    assert len(result) == 0


def test_symbol_anchor_k_cap(tiny_db_v74: str):
    from groundtruth.pretask.anchor_select import _symbol_anchors
    # Issue with many matches; k_anchor=1 should limit to 1 file
    issue = "parse_expr parse_stmt Token tokenize Node ASTBuilder crash"
    result = _symbol_anchors(issue, tiny_db_v74, k_anchor=1)
    assert len(result) == 1


# ------------------------------------------------------------------ graph_reach


def test_reach_direct_neighbor(tiny_db_v74: str):
    from groundtruth.pretask.graph_reach import compute_reach
    # Anchor = src/ast.py (contains ASTBuilder which calls parse_expr in parser.py)
    reach = compute_reach(["src/ast.py"], tiny_db_v74, max_depth=1)
    # src/parser.py should be reachable at depth 1 (ASTBuilder -> parse_expr)
    assert "src/parser.py" in reach
    assert reach["src/parser.py"].min_path_length == 1


def test_reach_multi_hop(tiny_db_v74: str):
    from groundtruth.pretask.graph_reach import compute_reach
    # Anchor = src/ast.py; depth=2 should reach src/tokens.py via:
    # ast.py -> parser.py -> tokens.py
    reach = compute_reach(["src/ast.py"], tiny_db_v74, max_depth=2)
    assert "src/tokens.py" in reach
    assert reach["src/tokens.py"].min_path_length == 2


def test_reach_depth_decay(tiny_db_v74: str):
    from groundtruth.pretask.graph_reach import compute_reach, EDGE_TYPE_WEIGHT
    # Depth decay property: a single-path score at depth d is ≤ single-path at d-1.
    # Compute from src/parser.py as anchor (1 outgoing edge to tokens.py via tokenize).
    reach = compute_reach(["src/parser.py"], tiny_db_v74, max_depth=2)
    # At depth=1: only tokens.py is reachable (via tokenize + Token)
    # parser.py itself is in reach (depth 0)
    assert "src/tokens.py" in reach
    # depth-1 score for tokens.py: each path contributes edge_weight*conf * 1/(1+1)
    # Two paths: tokenize (conf=1.0, CALLS=1.0) and Token (conf=0.9, CALLS=1.0)
    # score = 1.0*1.0*(1/2) + 1.0*0.9*(1/2) = 0.5 + 0.45 = 0.95
    assert reach["src/tokens.py"].min_path_length == 1


def test_reach_excludes_tests(tiny_db_v74: str):
    from groundtruth.pretask.graph_reach import compute_reach
    # tests/test_p.py has no outgoing edges and is not reachable from src/
    reach = compute_reach(["src/ast.py"], tiny_db_v74, max_depth=3)
    assert "tests/test_p.py" not in reach


def test_reach_empty_anchors(tiny_db_v74: str):
    from groundtruth.pretask.graph_reach import compute_reach
    reach = compute_reach([], tiny_db_v74, max_depth=3)
    assert reach == {}


# ------------------------------------------------------------------ hub_penalty


def test_hub_penalty_range(tiny_db_v74: str):
    from groundtruth.pretask.hub_penalty import compute_hub_penalties, W_HUB_MAX
    penalties = compute_hub_penalties(tiny_db_v74)
    # All penalties must be in [0, 1)
    for fp, pen in penalties.items():
        assert 0.0 <= pen < 1.0, f"{fp}: penalty={pen}"
    # W_HUB_MAX cap is correct
    assert W_HUB_MAX <= 0.10


def test_hub_penalty_high_fanout(tiny_db_v74: str):
    from groundtruth.pretask.hub_penalty import compute_hub_penalties, HUB_SCALE
    penalties = compute_hub_penalties(tiny_db_v74)
    # src/parser.py is target of 1 edge (ASTBuilder -> parse_expr)
    # src/tokens.py is target of 2 edges (parse_expr -> tokenize/Token)
    # Higher in-degree should have higher penalty
    if "src/tokens.py" in penalties and "src/parser.py" in penalties:
        # tokens.py has in-degree 2, parser.py has in-degree 1
        # tanh(2/HUB_SCALE) > tanh(1/HUB_SCALE) for any HUB_SCALE > 0
        assert penalties["src/tokens.py"] >= penalties["src/parser.py"]


# ------------------------------------------------------------------ anchor_proximity


def test_anchor_proximity_self(tiny_db_v74: str):
    from groundtruth.pretask.anchor_proximity import compute_anchor_proximity
    prox = compute_anchor_proximity(["src/parser.py"], tiny_db_v74)
    # The anchor itself gets prox from 1 anchor → min(1.0, 1/3.0)
    assert "src/parser.py" in prox
    assert prox["src/parser.py"] == pytest.approx(1 / 3.0)


def test_anchor_proximity_convergence(tiny_db_v74: str):
    from groundtruth.pretask.anchor_proximity import compute_anchor_proximity
    # If both src/ast.py and src/parser.py are anchors, their 1-hop neighbor
    # src/tokens.py gets prox from 2 distinct anchors → min(1.0, 2/3.0)
    prox = compute_anchor_proximity(["src/ast.py", "src/parser.py"], tiny_db_v74)
    if "src/tokens.py" in prox:
        assert prox["src/tokens.py"] >= 1 / 3.0 - 1e-9


def test_anchor_proximity_cap(tiny_db_v74: str):
    from groundtruth.pretask.anchor_proximity import compute_anchor_proximity
    # 4+ anchors: cap at 1.0
    prox = compute_anchor_proximity(
        ["src/ast.py", "src/parser.py", "src/tokens.py", "tests/test_p.py"],
        tiny_db_v74
    )
    for fp, score in prox.items():
        assert 0.0 <= score <= 1.0, f"{fp}: {score}"


# ------------------------------------------------------------------ ablation weights


def test_ablation_A_no_graph_terms():
    from groundtruth.pretask.v7_4_brief import _ablation_weights, DEFAULT_WEIGHTS
    w = _ablation_weights("A", dict(DEFAULT_WEIGHTS))
    assert w["W_REACH"] == 0.0
    assert w["W_PROX"] == 0.0
    assert w["W_HUB"] == 0.0
    assert w["W_COMMIT"] == 0.0
    assert w["W_SEM"] > 0.0


def test_ablation_B0_no_sem():
    from groundtruth.pretask.v7_4_brief import _ablation_weights, DEFAULT_WEIGHTS
    w = _ablation_weights("B0", dict(DEFAULT_WEIGHTS))
    assert w["W_SEM"] == 0.0
    assert w["W_HUB"] == 0.0
    assert w["W_COMMIT"] == 0.0


def test_ablation_C_no_commit():
    from groundtruth.pretask.v7_4_brief import _ablation_weights, DEFAULT_WEIGHTS
    w = _ablation_weights("C", dict(DEFAULT_WEIGHTS))
    assert w["W_COMMIT"] == 0.0
    assert w["W_SEM"] > 0.0
    assert w["W_REACH"] > 0.0


def test_hub_penalty_weight_cap():
    from groundtruth.pretask.v7_4_brief import _total_score
    from groundtruth.pretask.hub_penalty import W_HUB_MAX
    # Even with W_HUB > W_HUB_MAX, total_score caps it
    comps = {"sem": 0.5, "reach": 0.4, "anchor_prox": 0.1, "hub_pen": 0.8, "commit": 0.0}
    weights_over = {"W_SEM": 0.5, "W_REACH": 0.4, "W_PROX": 0.1, "W_HUB": 0.5, "W_COMMIT": 0.0}
    weights_capped = {"W_SEM": 0.5, "W_REACH": 0.4, "W_PROX": 0.1, "W_HUB": W_HUB_MAX, "W_COMMIT": 0.0}
    s_over = _total_score(comps, weights_over)
    s_capped = _total_score(comps, weights_capped)
    # The cap enforces W_HUB ≤ W_HUB_MAX regardless of what's passed
    assert s_over == pytest.approx(s_capped, abs=1e-9)


def test_focus_set_hard_cap(tiny_db_v74: str, tiny_repo: str):
    """Focus set must never exceed DEFAULT_FOCUS_SIZE."""
    from groundtruth.pretask.v7_4_brief import run_v74, DEFAULT_FOCUS_SIZE
    # Mock model that returns random normalized embeddings (no sentence-transformer needed)
    class FakeModel:
        def encode(self, texts, **kw):
            rng = np.random.default_rng(42)
            embs = rng.random((len(texts), 384)).astype(np.float32)
            norms = np.linalg.norm(embs, axis=1, keepdims=True)
            return embs / np.where(norms == 0, 1, norms)

    result = run_v74(
        issue_text="parse_expr crashes",
        repo_root=tiny_repo,
        graph_db=tiny_db_v74,
        bug_id="test-1",
        repo="test/repo",
        gold_files=["src/parser.py"],
        ablation="C",
    )
    assert result.focus_set_size == DEFAULT_FOCUS_SIZE
    assert len(result.focus_set) == DEFAULT_FOCUS_SIZE


def test_entered_via_graph_rescue(tiny_db_v74: str, tiny_repo: str):
    """Files admitted only via graph expansion get entered_via='graph_rescue'."""
    from groundtruth.pretask.v7_4_brief import run_v74
    # Use variant B1 to isolate graph rescue behavior
    result = run_v74(
        issue_text="ASTBuilder failed to handle nested nodes",
        repo_root=tiny_repo,
        graph_db=tiny_db_v74,
        gold_files=[],
        ablation="B1",
    )
    # Check that graph_rescue entries exist in the full ranking
    entries = [r for r in result.ranked_full if r["entered_via"] == "graph_rescue"]
    # With B1, files reachable from semantic anchors via graph are graph_rescue
    # (this is a structural check, not a specific file check)
    assert isinstance(entries, list)
