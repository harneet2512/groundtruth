"""Layer 2.8 — L6 pre-submit VERIFIABLE consolidation (Option 2).

Fires once at the edit→review transition (not the dead finish handler).
Verifiable only: lists tests (from assertions table, verified links) that
cover the edited files. No semantic judgment, no caller-edit prescription.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))
import oh_gt_full_wrapper as w  # noqa: E402


def _make_db(with_assertion: bool = True) -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "label TEXT, is_test INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE assertions (id INTEGER PRIMARY KEY, test_node_id INT, "
        "target_node_id INT, kind TEXT, expression TEXT)"
    )
    # target function in src/app.py, test in tests/test_app.py
    conn.execute("INSERT INTO nodes (id, name, file_path, label, is_test) VALUES (1, 'foo', 'src/app.py', 'Function', 0)")
    conn.execute("INSERT INTO nodes (id, name, file_path, label, is_test) VALUES (2, 'test_foo', 'tests/test_app.py', 'Function', 1)")
    if with_assertion:
        conn.execute("INSERT INTO assertions (test_node_id, target_node_id, kind, expression) VALUES (2, 1, 'assertEqual', 'x==1')")
    conn.commit()
    conn.close()
    return path


class _Obs:
    def __init__(self):
        self.content = "agent output"


def _make_config(db: str, edited: set, last_edit_action: int, action_count: int):
    cfg = w.GTRuntimeConfig()
    cfg.graph_db = db
    cfg._host_graph_db = db
    cfg._presubmit_edited_files = set(edited)
    cfg._presubmit_last_edit_action = last_edit_action
    cfg.action_count = action_count
    cfg._presubmit_fired = False
    return cfg


def test_fires_at_review_transition_with_verified_test():
    db = _make_db(with_assertion=True)
    try:
        cfg = _make_config(db, {"src/app.py"}, last_edit_action=5, action_count=8)  # 3 since edit
        obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        # Should have fired and appended the GT_VERIFY consolidation
        assert cfg._presubmit_fired is True
        content = getattr(obs, "content", "")
        assert "[GT_VERIFY]" in content
        assert "tests/test_app.py::test_foo" in content
    finally:
        os.unlink(db)


def test_does_not_fire_before_review_transition():
    db = _make_db(with_assertion=True)
    try:
        cfg = _make_config(db, {"src/app.py"}, last_edit_action=5, action_count=6)  # only 1 since edit
        obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        assert cfg._presubmit_fired is False  # too soon — still editing
        assert "[GT_VERIFY]" not in getattr(obs, "content", "")
    finally:
        os.unlink(db)


def test_does_not_fire_with_no_edits():
    db = _make_db(with_assertion=True)
    try:
        cfg = _make_config(db, set(), last_edit_action=0, action_count=10)
        obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        assert cfg._presubmit_fired is False
        assert "[GT_VERIFY]" not in getattr(obs, "content", "")
    finally:
        os.unlink(db)


def test_silent_when_no_verified_test():
    """No verified test linkage → fire once but stay silent (no guess)."""
    db = _make_db(with_assertion=False)
    try:
        cfg = _make_config(db, {"src/app.py"}, last_edit_action=5, action_count=9)
        obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        assert cfg._presubmit_fired is True  # fired (won't retry)
        assert "[GT_VERIFY]" not in getattr(obs, "content", "")  # but silent
    finally:
        os.unlink(db)


def test_fires_only_once():
    db = _make_db(with_assertion=True)
    try:
        cfg = _make_config(db, {"src/app.py"}, last_edit_action=5, action_count=8)
        w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        assert cfg._presubmit_fired is True
        # Second call must be a no-op
        obs2 = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        assert "[GT_VERIFY]" not in getattr(obs2, "content", "")
    finally:
        os.unlink(db)


def test_no_semantic_judgment_only_verifiable():
    """Output must be verifiable test list — no 'incomplete'/'should edit' prose."""
    db = _make_db(with_assertion=True)
    try:
        cfg = _make_config(db, {"src/app.py"}, last_edit_action=5, action_count=8)
        obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        content = getattr(obs, "content", "").lower()
        assert "incomplete" not in content
        assert "should edit" not in content
        assert "do not submit" not in content
        assert "pytest" in content  # verifiable action only
    finally:
        os.unlink(db)
