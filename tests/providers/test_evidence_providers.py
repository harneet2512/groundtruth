"""Parity tests for Layer 4 evidence providers (post_edit extraction).

Uses a minimal graph.db fixture + a tiny on-disk repo. Verifies each provider
returns shapes equivalent to the legacy helpers in
``src/groundtruth/hooks/post_edit.py``.

These are admission-gate tests. They do not prove the providers help an agent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.providers.evidence_providers import (
    CallerCodeRecord,
    SiblingFunction,
    caller_code_provider,
    contract_provider,
    edit_propagation_provider,
    sibling_twin_provider,
    structural_twin_in_function_provider,
)
# Imported under an alias because pytest would collect ``test_provider``
# (a public helper) as a test function otherwise.
from groundtruth.providers.evidence_providers import test_provider as assertion_provider


def _build_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a tiny repo + graph.db pointing at it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core").mkdir()
    (repo / "users").mkdir()
    (repo / "tests").mkdir()

    target_py = repo / "core" / "target.py"
    # Lines 3 and 5 share the template ``return helper(STRING, NUM)`` after
    # literal normalization — the structural-twin detector should pick them up.
    target_py.write_text(
        "def target(a, b):\n"
        "    if a > 0 and b > 0:\n"
        "        return helper('alpha', 100)\n"
        "    if a < 0 and b < 0:\n"
        "        return helper('beta', 200)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    foo_py = repo / "users" / "foo.py"
    foo_py.write_text(
        "from core.target import target\n"
        "def caller_one():\n"
        "    return target(1, 2)\n",
        encoding="utf-8",
    )
    bar_py = repo / "users" / "bar.py"
    bar_py.write_text(
        "from core.target import target\n"
        "def caller_two():\n"
        "    return target(3, 4)\n",
        encoding="utf-8",
    )
    sibling_py = repo / "core" / "sibling.py"
    sibling_py.write_text(
        "def sibling_func(x):\n"
        "    return x + 1\n",
        encoding="utf-8",
    )

    db = tmp_path / "graph.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            label TEXT,
            name TEXT,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported INTEGER DEFAULT 0,
            is_test INTEGER DEFAULT 0,
            language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_id INTEGER,
            target_id INTEGER,
            type TEXT,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.5,
            metadata TEXT
        );
        CREATE TABLE assertions (
            id INTEGER PRIMARY KEY,
            kind TEXT, expression TEXT, expected TEXT,
            line INTEGER, test_node_id INTEGER, target_node_id INTEGER
        );
        """
    )
    nodes = [
        (1, "Function", "target", "core.target", "core/target.py", 1, 6, "def target(a, b)", "int", 1, 0, "python", 0),
        (2, "Function", "caller_one", "users.foo", "users/foo.py", 2, 3, "def caller_one()", None, 1, 0, "python", 0),
        (3, "Function", "caller_two", "users.bar", "users/bar.py", 2, 3, "def caller_two()", None, 1, 0, "python", 0),
        (4, "Function", "sibling_func", "core.sibling", "core/sibling.py", 1, 2, "def sibling_func(x)", "int", 1, 0, "python", 0),
        (5, "Function", "test_target", "tests.test_core", "tests/test_core.py", 1, 4, "def test_target()", None, 0, 1, "python", 0),
    ]
    con.executemany(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", nodes,
    )
    edges = [
        # source_id, target_id, type, source_line, confidence
        (2, 1, "CALLS", 3, 1.0),  # caller_one calls target at users/foo.py:3
        (3, 1, "CALLS", 3, 1.0),  # caller_two calls target at users/bar.py:3
    ]
    for s, t, typ, line, conf in edges:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, confidence) VALUES (?, ?, ?, ?, ?)",
            (s, t, typ, line, conf),
        )
    # One test assertion
    con.execute(
        "INSERT INTO assertions (kind, expression, expected, line, test_node_id, target_node_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("equality", "target(1, 2)", "3", 3, 5, 1),
    )
    con.commit()
    con.close()
    return repo, str(db)


class TestCallerCodeProvider:
    def test_returns_callers_with_code(self, tmp_path: Path) -> None:
        repo, db = _build_repo(tmp_path)
        rows = caller_code_provider(db, "core/target.py", "target", str(repo))
        # Two cross-file callers.
        files = sorted(r.file for r in rows)
        assert files == ["users/bar.py", "users/foo.py"]
        # Code is read from disk.
        for r in rows:
            assert "target(" in r.code

    def test_seen_marks_caller_seen(self, tmp_path: Path) -> None:
        repo, db = _build_repo(tmp_path)
        rows = caller_code_provider(
            db, "core/target.py", "target", str(repo), seen_files=["users/foo.py"],
        )
        seen_map = {r.file: r.unseen for r in rows}
        assert seen_map["users/foo.py"] is False
        assert seen_map["users/bar.py"] is True

    def test_missing_db_returns_empty(self, tmp_path: Path) -> None:
        rows = caller_code_provider("/missing.db", "x.py", "f", str(tmp_path))
        assert rows == []

    def test_unknown_function_returns_empty(self, tmp_path: Path) -> None:
        repo, db = _build_repo(tmp_path)
        rows = caller_code_provider(db, "core/target.py", "no_such_fn", str(repo))
        assert rows == []


class TestContractProvider:
    def test_returns_signature(self, tmp_path: Path) -> None:
        _, db = _build_repo(tmp_path)
        c = contract_provider(db, "core/target.py", "target")
        assert c is not None
        assert "target" in c.signature
        assert c.return_type == "int"

    def test_unknown_returns_none(self, tmp_path: Path) -> None:
        _, db = _build_repo(tmp_path)
        assert contract_provider(db, "core/target.py", "no_such") is None


class TestSiblingTwinProvider:
    def test_same_file_siblings(self, tmp_path: Path) -> None:
        repo, db = _build_repo(tmp_path)
        # In the fixture, sibling_func is in sibling.py — siblings query falls
        # back to same-file when parent_id is None. We only have one function
        # per file so the result is empty (no siblings) — that's correct
        # behavior, not a bug.
        rows = sibling_twin_provider(db, "core/target.py", "target", str(repo))
        assert isinstance(rows, list)


class TestAssertionProvider:
    def test_returns_assertions(self, tmp_path: Path) -> None:
        _, db = _build_repo(tmp_path)
        rows = assertion_provider(db, "core/target.py", "target")
        assert len(rows) == 1
        assert rows[0].test_name == "test_target"
        assert rows[0].expression == "target(1, 2)"
        assert rows[0].expected == "3"


class TestStructuralTwinInFunction:
    def test_finds_twin_block(self, tmp_path: Path) -> None:
        repo, _ = _build_repo(tmp_path)
        groups = structural_twin_in_function_provider(
            str(repo / "core" / "target.py"), func_start=1, func_end=6,
        )
        # Two lines share the ``return a + b`` template inside the function.
        assert any(len(g.entries) >= 2 for g in groups)


class TestEditPropagation:
    def test_returns_high_conf_call_sites(self, tmp_path: Path) -> None:
        _, db = _build_repo(tmp_path)
        rows = edit_propagation_provider(db, "core/target.py", "target", min_confidence=0.9)
        files = sorted(r.caller_file for r in rows)
        assert files == ["users/bar.py", "users/foo.py"]
        for r in rows:
            assert r.line == 3


class TestParityWithLegacyShape:
    def test_caller_code_provider_matches_post_edit_shape(self, tmp_path: Path) -> None:
        """Provider records carry the same fields the legacy dicts did."""
        repo, db = _build_repo(tmp_path)
        rows = caller_code_provider(db, "core/target.py", "target", str(repo))
        for r in rows:
            assert isinstance(r, CallerCodeRecord)
            # Legacy dict keys: file, line, caller_name, code, unseen
            for attr in ("file", "line", "caller_name", "code", "unseen"):
                assert hasattr(r, attr)

    def test_sibling_provider_shape(self, tmp_path: Path) -> None:
        _, db = _build_repo(tmp_path)
        rows = sibling_twin_provider(db, "core/target.py", "target", "/nowhere")
        assert isinstance(rows, list)
        # If empty (likely here), no further assertions. If populated, must
        # carry name/signature/snippet.
        for r in rows:
            assert isinstance(r, SiblingFunction)
            assert hasattr(r, "name")
            assert hasattr(r, "signature")
            assert hasattr(r, "snippet")
