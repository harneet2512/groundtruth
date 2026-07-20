"""D-O (run6 batch1, aiogram-1594 m37 vs m9): the consensus.scope completeness
block filtered un-edited in-scope files by BASENAME name-match against the issue
focus, dropping a verified graph-caller GT had ALREADY delivered as l3b
caller-contract (the gold caller fsm/scene.py) while flagging a name-match file.

The correct fix (an earlier "flag ALL _query_scope neighbours" attempt was
REVERTED because it broke the intentional correct-or-quiet contract of
test_live_scope_completeness_reroute): admit an un-edited member without a name
match ONLY when it is in `_l3b_delivered_caller_rels` — a file GT itself
delivered as a caller this run (evidence-backed relevance). An ambient neighbour
GT never delivered stays gated.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "artifact_deepswe") not in sys.path:
    sys.path.insert(0, str(_REPO / "artifact_deepswe"))

import gt_mini_patch as g  # noqa: E402


def _scope_setup(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(g, "_consensus_scope", {"fsm/machine.py", "fsm/scene.py"})
    monkeypatch.setattr(g, "_oracle_edited_rels", {"fsm/machine.py"})
    # scene.py is a VERIFIED 1-hop graph neighbour of the edited machine.py
    monkeypatch.setattr(g, "_query_scope",
                        lambda f: ["fsm/scene.py"] if "machine" in f else [])
    # focus names 'machine' only -> scene.py does NOT name-match
    monkeypatch.setattr(g, "_oracle_focus", lambda: {"machine"})
    monkeypatch.setattr(g, "_l3b_delivered_caller_rels", set())


def test_delivered_caller_flagged_without_name_match(monkeypatch):
    _scope_setup(monkeypatch)
    g._l3b_delivered_caller_rels.add(g._norm_rel("fsm/scene.py"))  # GT delivered it as a caller
    block = g._scope_completeness_block()
    assert block, "a strict-subset scope with a delivered caller must emit a block"
    assert "scene.py" in block, (
        "the graph-caller GT already delivered must be flagged even without a name match")


def test_correct_or_quiet_when_caller_not_delivered(monkeypatch):
    # SAME graph shape, but GT did NOT deliver scene.py as a caller and it does not
    # name-match -> the block stays quiet (the intentional contract the revert protected).
    _scope_setup(monkeypatch)   # registry left empty
    assert g._scope_completeness_block() == ""


def test_note_parses_caller_files_only_from_pure_payloads(monkeypatch):
    monkeypatch.setattr(g, "_l3b_pure_caller_hashes", set())
    monkeypatch.setattr(g, "_l3b_delivered_caller_rels", set())
    payload = ("[CALLERS] update() has 2 verified callers\n"
               "  update() in fsm/scene.py:88 `x`\n"
               "  handle called by -> fsm/router.py:12\n")
    # not yet marked pure -> nothing recorded (a callee/sibling/mixed payload)
    g._note_l3b_delivered_callers("l3b.evidence", payload)
    assert g._l3b_delivered_caller_rels == set()
    # mark pure (the build-time verdict) -> the caller files are recorded
    g._l3b_pure_caller_hashes.add(g._l3b_content_key(payload))
    g._note_l3b_delivered_callers("l3b.evidence", payload)
    assert g._norm_rel("fsm/scene.py") in g._l3b_delivered_caller_rels
    assert g._norm_rel("fsm/router.py") in g._l3b_delivered_caller_rels


def test_note_ignores_non_l3b_kind(monkeypatch):
    monkeypatch.setattr(g, "_l3b_pure_caller_hashes", set())
    monkeypatch.setattr(g, "_l3b_delivered_caller_rels", set())
    payload = "update() in fsm/scene.py:88"
    g._l3b_pure_caller_hashes.add(g._l3b_content_key(payload))
    g._note_l3b_delivered_callers("l3.contract", payload)   # wrong kind
    assert g._l3b_delivered_caller_rels == set()
