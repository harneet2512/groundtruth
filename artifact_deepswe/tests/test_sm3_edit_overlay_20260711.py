"""SM-3 (2026-07-11) — EditOverlay activation at the edit boundary.

The transactional edit primitive + PURE stale-envelope marking, behind ``GT_EDIT_OVERLAY``
(default OFF byte-identical). SM-3 wires the TRANSACTIONAL APPLY + STALE-MARKING only; the
overlay reindex is forced UNAVAILABLE so graph.db is NEVER rebuilt (SM-4 owns the live
graph-view composition). Host-side signal only.

PINNED HERE:
  1. Flag OFF -> ``_gt_overlay_last`` stays None and the file on disk is untouched.
  2. Flag ON over a structured str_replace that introduces a syntax error -> the overlay
     commits, reports exactly ONE NEW edit-attributed syntax error (the differential, via a
     brief pre-revert), and the agent's edit is preserved on disk.
  3. NO GRAPH REBUILD -> graph.db is not created/modified (reindex forced unavailable).
  4. STALE MARKING -> a gateway envelope whose ``graph_revision`` differs from the
     post-revision is marked stale.
  5. LEAK-0 -> the host telemetry never carries a test identity (there is none here).

Mutations bitten: drop the pre-revert (M1) -> #2's new-error count is 0; ignore the flag
(M2) -> #1 fails; drop mark_stale_envelopes (M3) -> #4's count is 0.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gt_mini_patch as g  # noqa: E402

_AFTER = "x = (\n"        # the agent's already-applied edit (a syntax error)
_BEFORE = "x = 1\n"       # reconstructed pre-edit (clean)


@pytest.fixture
def repo(monkeypatch, tmp_path):
    (tmp_path / "foo.py").write_text(_AFTER, encoding="utf-8")   # edit already on disk
    db = tmp_path / "graph.db"
    db.write_bytes(b"NOT-A-REAL-SQLITE-DB-BUT-A-STABLE-BLOB")     # non-empty -> hashable rev
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_db_path", lambda: str(db))
    g._gt_overlay_last = None
    return tmp_path, db


def _action():
    return {"command": "str_replace", "path": "foo.py",
            "old_str": "x = 1", "new_str": "x = ("}


# --------------------------------------------------------------------------- #
# 1. Flag OFF -> no-op (byte-identical).
# --------------------------------------------------------------------------- #
def test_flag_off_is_a_noop(repo, monkeypatch):
    tmp_path, _db = repo
    monkeypatch.delenv("GT_EDIT_OVERLAY", raising=False)
    g._gt_edit_overlay_transaction(_action())
    assert g._gt_overlay_last is None
    assert (tmp_path / "foo.py").read_text(encoding="utf-8") == _AFTER


# --------------------------------------------------------------------------- #
# 2/3. Flag ON -> commit, ONE new syntax error, edit preserved, no graph rebuild.
# --------------------------------------------------------------------------- #
def test_transaction_reports_new_error_and_preserves_edit(repo, monkeypatch):
    tmp_path, db = repo
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [], raising=False)
    db_bytes_before = db.read_bytes()

    g._gt_edit_overlay_transaction(_action())

    assert g._gt_overlay_last is not None
    res = g._gt_overlay_last["result"]
    assert res["committed"] is True
    # the differential: exactly one NEW, edit-attributed syntax error.
    assert len(res["diagnostics_delta"]) == 1
    assert res["diagnostics_delta"][0]["verdict"] == "syntax_error"
    # the agent's edit is preserved on disk (bulletproof restore).
    assert (tmp_path / "foo.py").read_text(encoding="utf-8") == _AFTER
    # NO graph rebuild: graph.db is byte-unchanged (reindex forced unavailable).
    assert db.read_bytes() == db_bytes_before
    assert res["capability_states"].get("graph") == "stale"


# --------------------------------------------------------------------------- #
# 4. Stale marking: a stale gateway envelope is flagged against the post-revision.
# --------------------------------------------------------------------------- #
def test_stale_envelope_marking(repo, monkeypatch):
    _tmp, _db = repo
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    # a delivered envelope whose freshness token predates this revision -> stale.
    monkeypatch.setattr(g, "_gt_gateway_deliveries",
                        [{"graph_revision": "OLD_STALE_REVISION"}], raising=False)
    g._gt_edit_overlay_transaction(_action())
    assert g._gt_overlay_last is not None
    assert g._gt_overlay_last["stale_marked"] == 1


# --------------------------------------------------------------------------- #
# 5. before/after reconstruction is correct for a structured str_replace.
# --------------------------------------------------------------------------- #
def test_before_after_reconstruction(repo):
    info = g._edit_overlay_before_after(_action())
    assert info is not None
    rel, before, after = info
    assert rel == "foo.py"
    assert after == _AFTER
    assert before == _BEFORE  # reverse-applied str_replace


def test_pre_revert_write_fault_never_corrupts_the_edit(repo, monkeypatch):
    """F1 (SM-3 LIPI): if the pre-revert ``open(...,"w").write(before)`` truncates the file
    then FAULTS mid-write (disk-full/EIO), the agent's ``after`` MUST still survive — the
    finally must be ARMED before the clobber. RED with the old order (file left ''), GREEN
    once ``reverted=True`` precedes the write."""
    import builtins
    tmp_path, _db = repo
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    target = os.path.abspath(str(tmp_path / "foo.py"))
    real_open = builtins.open
    state = {"fired": False}

    def faulty_open(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)  # a real "w" open TRUNCATES here
        if (not state["fired"] and "w" in mode
                and os.path.abspath(str(path)) == target):
            state["fired"] = True  # only the FIRST target "w" (the pre-revert) faults

            def _boom(*_a, **_k):
                raise OSError("ENOSPC (simulated disk-full mid-write)")
            fh.write = _boom
        return fh

    monkeypatch.setattr(builtins, "open", faulty_open)
    g._gt_edit_overlay_transaction(_action())
    monkeypatch.setattr(builtins, "open", real_open)  # restore before assertion read
    assert state["fired"], "the fault injection must have fired on the pre-revert write"
    # the agent's edit is NOT corrupted despite the mid-write fault.
    assert (tmp_path / "foo.py").read_text(encoding="utf-8") == _AFTER


def test_bash_edit_has_no_reconstructable_before(repo, monkeypatch):
    # a bash edit action carries no structured old/new -> before is None (no differential).
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    info = g._edit_overlay_before_after(
        {"command": "sed -i 's/a/b/' foo.py"})
    # classified as a bash post_edit? if so before is None; either way no crash + no revert.
    if info is not None:
        assert info[1] is None
