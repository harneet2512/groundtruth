"""Pin B5 (token bloat): a Lane-A FACT (brief/contract/evidence/scope/cochange) must
reach the agent ONCE per distinct content, even when the oracle behavioral-state
changes between turns. The content+state hash alone let the routing.py <gt-contract>
re-emit verbatim 10x (fastapi witness). Lane-A now also dedups on a content-only hash."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


def _silence_side_effects(monkeypatch):
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(g, "_record_hook_fire", lambda *a, **k: None)
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda *a, **k: None)
    monkeypatch.setattr(g, "_ledger_note_delivery", lambda *a, **k: None, raising=False)


def test_identical_fact_dedups_across_state_change(monkeypatch):
    _silence_side_effects(monkeypatch)
    block = ("l3.contract", "<gt-contract>def f(x: int) -> int</gt-contract>")

    monkeypatch.setattr(g, "_oracle_bstate", lambda: "turn=1|edits=1")
    o1: dict = {}
    g._lane_a_deliver(o1, "cmd", [block], krel="f.py", event=None)
    assert "<gt-contract>" in (o1.get("output") or "")  # first send delivers

    # state changed (new turn / more edits) — the SAME fact must NOT re-deliver
    monkeypatch.setattr(g, "_oracle_bstate", lambda: "turn=9|edits=9")
    o2: dict = {}
    g._lane_a_deliver(o2, "cmd", [block], krel="f.py", event=None)
    assert (o2.get("output") or "") == ""  # content-only dedup suppresses the re-send


def test_distinct_fact_still_delivers(monkeypatch):
    _silence_side_effects(monkeypatch)
    monkeypatch.setattr(g, "_oracle_bstate", lambda: "turn=1")
    o1: dict = {}
    g._lane_a_deliver(o1, "cmd", [("l3.contract", "<gt-contract>A</gt-contract>")], krel="f.py", event=None)
    assert "A" in (o1.get("output") or "")
    # a DIFFERENT fact (distinct content) must still reach the agent
    o2: dict = {}
    g._lane_a_deliver(o2, "cmd", [("l3.contract", "<gt-contract>B</gt-contract>")], krel="f.py", event=None)
    assert "B" in (o2.get("output") or "")
