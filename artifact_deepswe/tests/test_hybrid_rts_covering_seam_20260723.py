"""Hybrid RTS seam wiring — gt_mini_patch._covering_tests_for_symbols.

Pins: convention admit when FACT empty; VERIFY_EXECUTE off stays byte-identical
(no covering work); unattributable convention RED does not submit-BLOCK.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import gt_mini_patch as g  # noqa: E402

from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS  # noqa: E402
from groundtruth.runtime.submit_gate import gate_verdict  # noqa: E402

_DET = sorted(DETERMINISTIC_RESOLUTION_METHODS)[0]


def _make_graph(path: Path, nodes, edges) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, "
        "signature TEXT, return_type TEXT, is_exported INT, is_test INT, "
        "language TEXT, parent_id INT)"
    )
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, "
        "confidence REAL, metadata TEXT)"
    )
    for nid, name, fpath, is_test in nodes:
        con.execute(
            "INSERT INTO nodes (id, label, name, file_path, is_test, language) "
            "VALUES (?,?,?,?,?,?)",
            (nid, "Function", name, fpath, is_test, "python"),
        )
    for src, tgt, method, conf in edges:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
            "VALUES (?,?,?,?,?)",
            (src, tgt, "CALLS", method, conf),
        )
    con.commit()
    con.close()


@pytest.fixture
def convention_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "users.py").write_text("def get_user():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_users.py").write_text(
        "def test_get_user():\n    assert True\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    # name_match only — FACT lever empty; convention must admit.
    _make_graph(
        db,
        nodes=[
            (1, "get_user", "pkg/users.py", 0),
            (2, "test_get_user", "tests/test_users.py", 1),
        ],
        edges=[(2, 1, "name_match", 0.95)],
    )
    return repo, db


def test_seam_convention_admit_when_fact_empty(monkeypatch, convention_repo):
    repo, db = convention_repo
    monkeypatch.setattr(g, "_db_path", lambda: str(db))
    monkeypatch.setattr(g, "_root", lambda: str(repo))
    monkeypatch.setattr(g, "_ss_exec_truth_on", lambda: False)
    rows = g._covering_tests_for_symbols({"get_user"})
    assert rows == [{
        "file": "tests/test_users.py",
        "confidence": 0.0,
        "selection_basis": "test_dir_convention",
    }]


def test_seam_mutation_kill_convention_goes_dark(monkeypatch, convention_repo):
    repo, db = convention_repo
    monkeypatch.setattr(g, "_db_path", lambda: str(db))
    monkeypatch.setattr(g, "_root", lambda: str(repo))
    monkeypatch.setattr(g, "_ss_exec_truth_on", lambda: False)
    assert g._covering_tests_for_symbols({"get_user"})
    monkeypatch.setattr(
        "groundtruth.runtime.verification_plan._convention_candidates",
        lambda *a, **k: [],
    )
    assert g._covering_tests_for_symbols({"get_user"}) == []


def test_verify_execute_off_candidate_is_none(monkeypatch, convention_repo):
    """Byte-identity: GT_VERIFY_EXECUTE unset → no covering work."""
    repo, db = convention_repo
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_db_path", lambda: str(db))
    monkeypatch.setattr(g, "_root", lambda: str(repo))
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("pkg/users.py")
    monkeypatch.setattr(g, "_edited_symbols_for_selection", lambda: {"get_user"})
    g._covering_exec_fired_syms.clear()
    g._covering_exec_pending["syms"] = set()
    assert g._executed_covering_candidate() is None


def test_unattributable_fail_does_not_block_submit():
    """Convention (or any) RED without attribution → gate stays clean ALLOW.

    Mirrors the submit seam: covering dropped to None before gate_verdict when
    is_red_attributable is False.
    """
    covering = {"verdict": "fail", "ran": ["tests/test_users.py"]}
    # Simulate attribution drop (seam sets covering=None).
    verdict = gate_verdict(covering=None, hygiene=None, bounce_count=0, max_bounces=1)
    assert verdict.allow is True
    assert verdict.reason == "clean"
    # Positive control: attributed fail WOULD block.
    blocked = gate_verdict(covering=covering, hygiene=None, bounce_count=0, max_bounces=1)
    assert blocked.allow is False
    assert blocked.reason == "covering_test_failed"
