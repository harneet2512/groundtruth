"""Tests for v2.2 graph-aware QueryObject augmentation."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.query_augment import augment_query_with_graph
from groundtruth.pretask.v2_types import HighSignalToken, QueryObject


def _make_db(
    tmp_path: Path,
    rows: list[tuple[str, str, str]],
) -> str:
    db_path = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO nodes (label, name, file_path) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return str(db_path)


def test_augment_no_op_if_no_naturalword_tokens(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [("Function", "do_thing", "src/widget_core.py")])
    issue = "MyClass.do_thing raises RuntimeError in widget_core.py"
    q = QueryObject(
        file_hints=["src/widget_core.py"],
        function_hints=["do_thing"],
        class_hints=["MyClass"],
        high_signal_tokens=[
            HighSignalToken("do_thing", 4.0, "stack_trace"),
            HighSignalToken("MyClass", 3.0, "backtick"),
        ],
        raw_text=issue,
    )
    out = augment_query_with_graph(q, issue, db)
    assert out.file_hints == q.file_hints
    assert out.function_hints == q.function_hints
    assert out.class_hints == q.class_hints
    assert list(out.high_signal_tokens) == list(q.high_signal_tokens)


def test_augment_admits_prose_token_matching_file_basename(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [
            ("Function", "rewrite_asserts", "src/foo/rewrite.py"),
            ("Function", "other", "src/bar/other.py"),
        ],
    )
    issue = "The assertion rewrite logic breaks on nested calls."
    q = QueryObject(raw_text=issue)
    out = augment_query_with_graph(q, issue, db)
    # Augmenter writes to high_signal_tokens only — never to file_hints (which
    # would pollute v7.4's "Hints:" prefix). path_segment.py + the function
    # ranker consume high_signal_tokens directly.
    assert "rewrite" not in out.file_hints
    assert any(t.token == "rewrite" for t in out.high_signal_tokens)


def test_augment_admits_prose_token_matching_symbol_name(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [
            ("Function", "build", "src/builder/core.py"),
            ("Function", "unrelated", "src/other/core.py"),
        ],
    )
    issue = "We need to build the artifact correctly."
    q = QueryObject(raw_text=issue)
    out = augment_query_with_graph(q, issue, db)
    assert "build" not in out.function_hints
    assert any(t.token == "build" for t in out.high_signal_tokens)


def test_augment_unreadable_db_returns_input(tmp_path: Path) -> None:
    missing = str(tmp_path / "nope.db")
    issue = "rewrite the parser"
    q = QueryObject(raw_text=issue)
    out = augment_query_with_graph(q, issue, missing)
    assert out is q or (
        out.file_hints == q.file_hints
        and out.function_hints == q.function_hints
        and out.class_hints == q.class_hints
        and list(out.high_signal_tokens) == list(q.high_signal_tokens)
    )


def test_augment_does_not_duplicate_already_present_hints(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [("Function", "rewrite_asserts", "src/foo/rewrite.py")])
    issue = "rewrite logic is wrong"
    q = QueryObject(
        file_hints=["rewrite"],
        high_signal_tokens=[HighSignalToken("rewrite", 2.0, "snake_case")],
        raw_text=issue,
    )
    out = augment_query_with_graph(q, issue, db)
    matches = [t for t in out.high_signal_tokens if t.token == "rewrite"]
    assert len(matches) == 1


def test_augment_skips_stopwords(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [
            ("Function", "the_thing", "src/the.py"),
        ],
    )
    issue = "The the the value is wrong."
    q = QueryObject(raw_text=issue)
    out = augment_query_with_graph(q, issue, db)
    assert "the" not in out.file_hints
    assert not any(t.token == "the" for t in out.high_signal_tokens)


def test_augment_skips_partial_basename_match(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [
            ("Function", "verify_token", "src/users/auth_helpers.py"),
        ],
    )
    issue = "auth flow is broken"
    q = QueryObject(raw_text=issue)
    out = augment_query_with_graph(q, issue, db)
    assert "auth" not in out.file_hints
    assert not any(t.token == "auth" for t in out.high_signal_tokens)


def test_augment_idf_downweights_common_stems(tmp_path: Path) -> None:
    nodes = [("Function", f"f{i}", f"src/path_{i}.py") for i in range(20)]
    nodes.append(("Function", "rare_func", "src/uniquefile.py"))
    db = _make_db(tmp_path, nodes)
    issue = "uniquefile is broken and so is path"
    q = QueryObject(raw_text=issue)
    out = augment_query_with_graph(q, issue, db)
    rare = [t for t in out.high_signal_tokens if t.token == "uniquefile"]
    common = [t for t in out.high_signal_tokens if t.token == "path"]
    assert rare and rare[0].weight > 0.5
    assert (not common) or common[0].weight < rare[0].weight
