"""Layer 2.8 — L6 pre-submit VERIFIABLE contract reminder (Option 2, SANITIZED).

Fires ONCE at the edit→review transition (>=3 actions since the last source edit).
TEST-BLIND by design: surfaces the edited file's behavioral CONTRACT (the `properties`
table, is_test=0) plus a "run the project's own test suite" reminder — NEVER a test name
or a ``pytest <test>`` command (that leaked the grader on 7/9 tasks; gt_gt: the assertions
table is OFF-LIMITS). These tests lock that legitimacy + the once-only latch.

(Updated: the SUT was redesigned from listing tests -> surfacing contracts; the old tests
asserted the removed leak and used an `assertions`-table fixture the SUT no longer reads.)
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))
import oh_gt_full_wrapper as w  # noqa: E402


def _make_db(with_contract: bool = True) -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "label TEXT, is_test INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE properties (id INTEGER PRIMARY KEY, node_id INT, kind TEXT, value TEXT)"
    )
    conn.execute(
        "INSERT INTO nodes (id, name, file_path, label, is_test) "
        "VALUES (1, 'foo', 'src/app.py', 'Function', 0)"
    )
    if with_contract:
        conn.execute(
            "INSERT INTO properties (node_id, kind, value) VALUES (1, 'return_shape', 'Optional[User]')"
        )
    conn.commit()
    conn.close()
    return path


class _Obs:
    def __init__(self):
        self.content = "agent output"


def _make_config(db, edited, last_edit_action, action_count):
    cfg = w.GTRuntimeConfig()
    cfg.graph_db = db
    cfg._host_graph_db = db
    cfg._presubmit_edited_files = set(edited)
    cfg._presubmit_last_edit_action = last_edit_action
    cfg.action_count = action_count
    cfg._presubmit_fired = False
    return cfg


def _assert_no_test_leak(content: str):
    low = content.lower()
    assert "pytest" not in low, content  # no test command
    assert "::test_" not in content, content  # no test-node path
    assert "test_foo" not in content, content  # no test name


def test_fires_at_review_transition_with_contract():
    db = _make_db(with_contract=True)
    try:
        cfg = _make_config(db, {"src/app.py"}, last_edit_action=5, action_count=8)  # 3 since edit
        obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        content = getattr(obs, "content", "")
        assert cfg._presubmit_fired is True
        assert "[GT_VERIFY]" in content
        assert "return_shape" in content  # the behavioral CONTRACT is surfaced
        _assert_no_test_leak(content)  # but NEVER a test name/command
    finally:
        os.unlink(db)


def test_generic_reminder_when_no_contract():
    db = _make_db(with_contract=False)
    try:
        cfg = _make_config(db, {"src/app.py"}, last_edit_action=5, action_count=9)
        obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        content = getattr(obs, "content", "")
        assert cfg._presubmit_fired is True  # fires at the transition (won't retry)
        assert "[GT_VERIFY]" in content
        _assert_no_test_leak(content)  # generic reminder, no test leak
    finally:
        os.unlink(db)


def test_does_not_fire_before_review_transition():
    db = _make_db()
    try:
        cfg = _make_config(
            db, {"src/app.py"}, last_edit_action=5, action_count=6
        )  # only 1 since edit
        obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        assert cfg._presubmit_fired is False
        assert "[GT_VERIFY]" not in getattr(obs, "content", "")
    finally:
        os.unlink(db)


def test_does_not_fire_with_no_edits():
    db = _make_db()
    try:
        cfg = _make_config(db, set(), last_edit_action=0, action_count=10)
        obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        assert cfg._presubmit_fired is False
        assert "[GT_VERIFY]" not in getattr(obs, "content", "")
    finally:
        os.unlink(db)


def test_fires_only_once():
    db = _make_db()
    try:
        cfg = _make_config(db, {"src/app.py"}, last_edit_action=5, action_count=8)
        w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        assert cfg._presubmit_fired is True
        obs2 = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)  # second call = no-op
        assert "[GT_VERIFY]" not in getattr(obs2, "content", "")
    finally:
        os.unlink(db)


def test_no_test_name_or_pytest_leaked():
    db = _make_db(with_contract=True)
    try:
        cfg = _make_config(db, {"src/app.py"}, last_edit_action=5, action_count=8)
        obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
        content = getattr(obs, "content", "")
        low = content.lower()
        assert "incomplete" not in low
        assert "should edit" not in low
        _assert_no_test_leak(content)  # legitimacy: test-blind
    finally:
        os.unlink(db)
