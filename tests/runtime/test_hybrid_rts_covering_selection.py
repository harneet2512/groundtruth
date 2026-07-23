"""Hybrid RTS covering selection — FACT floor stays; convention admits when FACT empty.

Tranche-1 (2026-07-23): live covering_red / submit use select_targeted_tests so
path-convention tests admit when det+conf≥0.7 CALLS are absent. name_match never
enters the FACT lever. Attribution remains mandatory for DELIVER/BLOCK.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS
from groundtruth.runtime.verification_plan import (
    _convention_candidates,
    select_targeted_tests,
)

_DET = sorted(DETERMINISTIC_RESOLUTION_METHODS)[0]


def _make_graph(path: str, nodes: list[dict], edges: list[dict]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, "
        "signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER, "
        "language TEXT, parent_id INTEGER)"
    )
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT, "
        "confidence REAL, metadata TEXT)"
    )
    for n in nodes:
        con.execute(
            "INSERT INTO nodes (id,label,name,file_path,is_test,language) VALUES (?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             int(n.get("is_test", 0)), n.get("language", "python")),
        )
    for e in edges:
        con.execute(
            "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
            "VALUES (?,?,?,?,?)",
            (e["source_id"], e["target_id"], e.get("type", "CALLS"),
             e.get("resolution_method", _DET), e.get("confidence", 1.0)),
        )
    con.commit()
    con.close()


def test_convention_admits_when_fact_empty(tmp_path):
    """FACT-empty + tests/test_<stem>.py on disk → test_dir_convention basis."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "users.py").write_text("def get_user():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_users.py").write_text(
        "def test_get_user():\n    assert True\n", encoding="utf-8")
    db = str(tmp_path / "graph.db")
    # Entity in graph; ONLY a name_match CALLS edge (FACT must exclude it).
    _make_graph(
        db,
        nodes=[
            {"id": 1, "name": "get_user", "file_path": "pkg/users.py"},
            {"id": 2, "name": "test_get_user", "file_path": "tests/test_users.py", "is_test": 1},
        ],
        edges=[{
            "source_id": 2, "target_id": 1,
            "resolution_method": "name_match", "confidence": 0.95,
        }],
    )
    sel = select_targeted_tests(db, str(repo), ["get_user"], limit=2)
    assert sel, "convention must admit when FACT is empty"
    assert sel[0]["file"] == "tests/test_users.py"
    assert sel[0]["selection_basis"] == "test_dir_convention"


def test_name_match_still_excluded_from_fact_lever(tmp_path):
    """Without a real convention file on disk, name_match alone stays []."""
    db = str(tmp_path / "nm.db")
    _make_graph(
        db,
        nodes=[
            {"id": 1, "name": "edited", "file_path": "pkg/m.py"},
            {"id": 2, "name": "test_nm", "file_path": "tests/test_m.py", "is_test": 1},
        ],
        edges=[{
            "source_id": 2, "target_id": 1,
            "resolution_method": "name_match", "confidence": 0.9,
        }],
    )
    # Empty repo_root → convention cannot walk → [].
    assert select_targeted_tests(db, "", ["edited"]) == []


def test_mutation_disable_convention_returns_empty(tmp_path, monkeypatch):
    """Biting mutation: kill convention admit → FACT-empty selection goes dark."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "dates.py").write_text("def parse():\n    pass\n", encoding="utf-8")
    (repo / "tests" / "test_dates.py").write_text("def test_parse():\n    pass\n", encoding="utf-8")
    db = str(tmp_path / "graph.db")
    _make_graph(
        db,
        nodes=[{"id": 1, "name": "parse", "file_path": "pkg/dates.py"}],
        edges=[],  # no CALLS at all
    )
    honest = select_targeted_tests(db, str(repo), ["parse"], limit=2)
    assert any(r["selection_basis"] == "test_dir_convention" for r in honest)

    monkeypatch.setattr(
        "groundtruth.runtime.verification_plan._convention_candidates",
        lambda *a, **k: [],
    )
    mutant = select_targeted_tests(db, str(repo), ["parse"], limit=2)
    assert mutant == [], "disabling convention must empty FACT-empty selection"


def test_mutation_lower_fact_floor_would_admit_name_match(tmp_path, monkeypatch):
    """Biting mutation in the opposite direction: the FACT floor must stay ≥0.7.

    If someone lowers the covering_runner floor, name_match@0.9 would leak into
    fact_covering. This mutation proves the pin still bites.
    """
    from groundtruth.runtime import covering_runner as cr

    db = str(tmp_path / "floor.db")
    _make_graph(
        db,
        nodes=[
            {"id": 1, "name": "walk", "file_path": "pkg/account.py"},
            {"id": 2, "name": "test_walk", "file_path": "tests/test_account.py", "is_test": 1},
        ],
        edges=[{
            "source_id": 2, "target_id": 1,
            "resolution_method": "name_match", "confidence": 0.9,
        }],
    )
    assert cr.select_covering_tests(db, {"walk"}) == []

    # Mutant: pretend name_match is deterministic — selection must still be empty
    # because we do NOT change DETERMINISTIC_METHODS in production; this mutation
    # temporarily expands the set to show the test would catch a launder.
    original = set(cr._DETERMINISTIC_METHODS)
    monkeypatch.setattr(
        cr, "_DETERMINISTIC_METHODS", frozenset(original | {"name_match"}))
    leaked = cr.select_covering_tests(db, {"walk"})
    assert leaked and leaked[0]["file"] == "tests/test_account.py"
    # Restore path: production methods must not include name_match.
    assert "name_match" not in original
