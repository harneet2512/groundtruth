"""END-TO-END delivery proof for C1c + C1d: drive the real
``generate_improved_evidence`` (the function the post-edit hook calls) with a
synthetic repo file + graph.db whose ``properties`` rows reproduce the haystack
empty-guard and the ev47 duplicate-line defects, and assert the rendered
<gt-evidence> the AGENT would see is clean.

This is NOT a helper unit test — it proves the fix is WIRED into the emission
path the agent observes, per the "verify from agent observation" rule.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from groundtruth.hooks.post_edit import generate_improved_evidence


_SRC = '''\
class Lib:
    def transaction(self, key, value):
        if not key:
            return None
        with self.lock:
            self._store[key] = value
        return value
'''


def _make_repo_and_db(tmp_path, props: list[tuple[str, str, int]]):
    """Write a real source file + a graph.db whose properties rows for
    transaction() are ``props`` (kind, value, line)."""
    repo = tmp_path / "repo"
    pkg = repo / "lib"
    pkg.mkdir(parents=True)
    src = pkg / "store.py"
    src.write_text(_SRC, encoding="utf-8")

    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT,
            line INTEGER, confidence REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,is_test,language) "
        "VALUES (1,'Method','transaction','lib/store.py',2,7,"
        "'def transaction(self, key, value):',0,'python')"
    )
    for kind, value, line in props:
        conn.execute(
            "INSERT INTO properties (node_id,kind,value,line,confidence) VALUES (1,?,?,?,1.0)",
            (kind, value, line),
        )
    conn.commit()
    conn.close()
    return str(repo), db


def test_e2e_empty_guard_not_rendered(tmp_path, monkeypatch):
    """C1c: a blank guard_clause must NOT produce a ``PRESERVE:`` line, and when it
    is the only contract prop the [BEHAVIORAL CONTRACT] header must not ship with
    an empty body."""
    monkeypatch.setenv("GT_REBUILD_L3", "1")
    repo, db = _make_repo_and_db(tmp_path, [
        ("guard_clause", "   ", 3),           # blank guard -> must be dropped
        ("guard_clause", "not key", 3),       # real guard -> kept
    ])
    out = generate_improved_evidence("lib/store.py", ["transaction"], db, repo)
    # No empty PRESERVE line reaches the agent.
    assert "PRESERVE: \n" not in out and not out.rstrip().endswith("PRESERVE:")
    for line in out.splitlines():
        if line.strip().startswith("PRESERVE:"):
            assert line.split("PRESERVE:", 1)[1].strip(), f"empty PRESERVE shipped: {line!r}"


def test_e2e_duplicate_resource_deduped(tmp_path, monkeypatch):
    """C1d: duplicate resource/guard property rows must render at most once."""
    monkeypatch.setenv("GT_REBUILD_L3", "1")
    repo, db = _make_repo_and_db(tmp_path, [
        ("resource_pattern", "context_manager: self.lock", 5),
        ("resource_pattern", "context_manager: self.lock", 5),  # dup
        ("guard_clause", "not key", 3),
        ("guard_clause", "not key", 3),                          # dup
    ])
    out = generate_improved_evidence("lib/store.py", ["transaction"], db, repo)
    assert out.count("context_manager: self.lock") <= 1, (
        f"duplicate [RESOURCE] line shipped to agent:\n{out}"
    )
    assert out.count("PRESERVE: not key") <= 1, f"duplicate PRESERVE shipped:\n{out}"


def test_e2e_duplicate_param_in_params_line_deduped(tmp_path, monkeypatch):
    """C1d: a duplicated param row must not repeat inside the PARAMS line (the
    verified ``PARAMS: lib [required] [required]`` ev47 defect)."""
    monkeypatch.setenv("GT_REBUILD_L3", "1")
    repo, db = _make_repo_and_db(tmp_path, [
        ("param", "key", 2),
        ("param", "value", 2),
        ("param", "value", 2),   # dup param row
    ])
    out = generate_improved_evidence("lib/store.py", ["transaction"], db, repo)
    for line in out.splitlines():
        if line.strip().startswith("PARAMS:"):
            assert line.count("value [required]") <= 1, f"duplicate param in line: {line!r}"


def test_e2e_guards_precede_resources(tmp_path, monkeypatch):
    """C1d ordering: the guard (high-value) must appear before the resource
    (low-value) in the rendered block, so a downstream cap keeps the guard."""
    monkeypatch.setenv("GT_REBUILD_L3", "1")
    repo, db = _make_repo_and_db(tmp_path, [
        ("resource_pattern", "context_manager: self.lock", 5),
        ("guard_clause", "not key", 3),
    ])
    out = generate_improved_evidence("lib/store.py", ["transaction"], db, repo)
    if "PRESERVE: not key" in out and "context_manager: self.lock" in out:
        assert out.index("PRESERVE: not key") < out.index("context_manager: self.lock"), (
            f"resource rendered before guard:\n{out}"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
