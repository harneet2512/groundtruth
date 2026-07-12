"""SM-3 F5 (2026-07-11) — the EditOverlay seam must NOT alter the agent's file bytes.

Regression guard for the lossy text round-trip (F5): the seam read the just-edited file in
TEXT mode (`encoding="utf-8", errors="replace"`), so `apply_edit_transaction` rewrote the file
on COMMIT with CRLF collapsed to LF and every non-UTF-8 byte replaced by U+FFFD, and the
finally-restore compared in the same lossy mode so it never restored. With GT_EDIT_OVERLAY=1
(a Profile-2 member) this poisons the agent's real submission diff — strictly worse than GT-off.

INVARIANT PINNED: after a post_edit overlay transaction, the on-disk bytes equal EXACTLY what
the agent left on disk. CRLF is the common real case (git-committed CRLF source); the same
byte-exact read closes the non-UTF-8 case too.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gt_mini_patch as g  # noqa: E402

# The agent's already-applied edit, on disk, CRLF line endings. Valid Python -> the transaction
# COMMITS -> that is exactly when the lossy re-encode persisted.
_AFTER_BYTES = b"x = 1\r\ny = 3\r\n"


@pytest.fixture
def repo(monkeypatch, tmp_path):
    (tmp_path / "foo.py").write_bytes(_AFTER_BYTES)          # byte-exact agent edit on disk
    db = tmp_path / "graph.db"
    db.write_bytes(b"NOT-A-REAL-SQLITE-DB-BUT-A-STABLE-BLOB")  # non-empty -> hashable rev
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_db_path", lambda: str(db))
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [], raising=False)
    g._gt_overlay_last = None
    return tmp_path, db


def _action():
    # str_replace -> `before` reconstructs (pre-revert path exercised) AND stays valid, so the
    # transaction commits -> exactly the path where the corruption persisted.
    return {"command": "str_replace", "path": "foo.py",
            "old_str": "y = 2", "new_str": "y = 3"}


def test_overlay_preserves_agent_bytes_crlf(repo, monkeypatch):
    tmp_path, _db = repo
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    foo = tmp_path / "foo.py"
    agent_bytes = foo.read_bytes()
    assert agent_bytes == _AFTER_BYTES              # sanity: the agent's bytes are on disk

    g._gt_edit_overlay_transaction(_action())

    # THE INVARIANT: GT must never alter the agent's file bytes. Pre-fix the committed file is
    # b"x = 1\ny = 3\n" (CRLF collapsed) -> this assertion fails RED.
    assert foo.read_bytes() == agent_bytes, (
        "EditOverlay altered the agent's file bytes: "
        f"{foo.read_bytes()!r} != {agent_bytes!r}")
    # and the overlay actually ran (a real commit, not a skipped no-op)
    assert g._gt_overlay_last is not None


def test_overlay_preserves_agent_bytes_create_path(repo, monkeypatch):
    """The reverted=False path (a `create` action: no before-reconstruction, so the pre-revert
    write is skipped and the finally never fires). Byte-fidelity here rides ENTIRELY on the
    apply() candidate being raw bytes — this test guards the apply-call hunk, which the
    str_replace test (guarded by the byte-exact finally-restore) does not."""
    tmp_path, _db = repo
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    foo = tmp_path / "foo.py"
    agent_bytes = foo.read_bytes()

    g._gt_edit_overlay_transaction({"command": "create", "path": "foo.py"})

    assert foo.read_bytes() == agent_bytes, (
        "create-path: EditOverlay altered the agent's file bytes: "
        f"{foo.read_bytes()!r} != {agent_bytes!r}")
    assert g._gt_overlay_last is not None


def test_overlay_finally_restores_bytes_exact_on_fault(repo, monkeypatch):
    """Fault path (guards the finally-restore hunk): apply_edit_transaction throws AFTER the
    pre-revert write (reverted armed) -> the bulletproof finally must restore the agent's content
    BYTE-EXACT, not the lossy text `after` whose non-UTF-8 byte became U+FFFD."""
    tmp_path, _db = repo
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    foo = tmp_path / "foo.py"
    agent_bytes = b"y = 3  # caf\xe9\r\n"          # CRLF + a non-UTF-8 byte (\xe9)
    foo.write_bytes(agent_bytes)

    import groundtruth.runtime.edit_overlay as eo

    def _boom(*a, **k):
        raise RuntimeError("apply fault after pre-revert")

    monkeypatch.setattr(eo, "apply_edit_transaction", _boom)
    # str_replace so `before` reconstructs -> reverted=True -> pre-revert write -> then fault.
    g._gt_edit_overlay_transaction({"command": "str_replace", "path": "foo.py",
                                    "old_str": "y = 2", "new_str": "y = 3"})

    assert foo.read_bytes() == agent_bytes, (
        "fault-path finally did not byte-exactly restore the agent's file: "
        f"{foo.read_bytes()!r} != {agent_bytes!r}")
