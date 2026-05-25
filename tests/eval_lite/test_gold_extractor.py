"""Tests for scripts.eval_lite.gold_extractor."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.eval_lite.gold_extractor import (
    extract_gold,
    gold_files_from_patch,
    gold_functions_from_patch,
)


SIMPLE_DIFF = """diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py
+++ b/src/foo.py
@@ -10,3 +10,4 @@ def bar():
     pass
+    print("new")
"""

MULTI_FILE_DIFF = """diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,4 @@
 import os
+import sys
 def f():
     pass
diff --git a/src/bar.py b/src/bar.py
--- a/src/bar.py
+++ b/src/bar.py
@@ -1,2 +1,3 @@
 x = 1
+y = 2
 z = 3
"""

FUNCTION_DIFF = """diff --git a/src/auth/login.py b/src/auth/login.py
--- a/src/auth/login.py
+++ b/src/auth/login.py
@@ -10,5 +10,6 @@ def login_user(name):
     check(name)
     issue_token(name)
+    log(name)
     return True

"""


def test_gold_files_simple_diff() -> None:
    files = gold_files_from_patch(SIMPLE_DIFF)
    assert files == {"src/foo.py"}


def test_gold_files_multi_file() -> None:
    files = gold_files_from_patch(MULTI_FILE_DIFF)
    assert files == {"src/foo.py", "src/bar.py"}


def test_gold_files_strip_a_prefix_only() -> None:
    diff = (
        "diff --git a/apps/auth/login.py b/apps/auth/login.py\n"
        "--- a/apps/auth/login.py\n"
        "+++ b/apps/auth/login.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x\n"
        "+y\n"
    )
    files = gold_files_from_patch(diff)
    assert files == {"apps/auth/login.py"}


def _build_fixture_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
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
                is_exported INTEGER DEFAULT 0,
                is_test INTEGER DEFAULT 0,
                language TEXT NOT NULL,
                parent_id INTEGER
            );
            """
        )
        conn.executemany(
            "INSERT INTO nodes (label, name, file_path, start_line, end_line, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("Function", "login_user", "src/auth/login.py", 10, 20, "python"),
                ("Function", "logout_user", "src/auth/login.py", 22, 30, "python"),
                ("Function", "bar", "src/foo.py", 9, 14, "python"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_gold_functions_with_fixture_graph_db(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    _build_fixture_db(db)
    funcs = gold_functions_from_patch(FUNCTION_DIFF, str(db))
    assert ("src/auth/login.py", "login_user") in funcs


def test_gold_functions_simple_diff_hits_bar(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    _build_fixture_db(db)
    funcs = gold_functions_from_patch(SIMPLE_DIFF, str(db))
    assert ("src/foo.py", "bar") in funcs


def test_extract_gold_handles_missing_db() -> None:
    instance = {"patch": SIMPLE_DIFF}
    out = extract_gold(instance, None)
    assert out["files"] == {"src/foo.py"}
    assert out["functions"] == set()


def test_extract_gold_handles_unreadable_db(tmp_path: Path) -> None:
    instance = {"patch": SIMPLE_DIFF}
    out = extract_gold(instance, str(tmp_path / "does_not_exist.db"))
    assert out["files"] == {"src/foo.py"}
    assert out["functions"] == set()


@pytest.mark.parametrize("patch", [SIMPLE_DIFF, MULTI_FILE_DIFF, FUNCTION_DIFF])
def test_gold_files_deterministic(patch: str) -> None:
    a = gold_files_from_patch(patch)
    b = gold_files_from_patch(patch)
    assert a == b
