"""Regression: Layer-4b per-hook FIRE counter (auditability).

Found by the per-layer LIPI (wx2eywcer): the runtime ledger records DELIVERED/SUPPRESSED
only, and an empty correct-or-quiet producer is skipped BEFORE any record — so a
fired-but-quiet hook left no trace and "how many times did each hook fire?" was not
answerable from disk. _record_hook_fire counts every fire to a JSON; this asserts the
empty producer is counted.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


def test_fire_counter_counts_fired_but_quiet_hooks(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "_HOOK_FIRE_COUNTS", {})
    monkeypatch.setenv("GT_HOOK_FIRE_COUNTS", str(tmp_path / "fc.json"))
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "ledger.jsonl"))
    out: dict = {}
    # l3.contract FIRES but is EMPTY (correct-or-quiet, no delivery); l3b.evidence delivers.
    g._lane_a_deliver(
        out, "cmd",
        [("l3.contract", ""), ("l3b.evidence", "real evidence")],
        krel="a.py", event=None,
    )
    # BOTH fired -> both counted, including the fired-but-quiet one (the gap this closes).
    assert g._HOOK_FIRE_COUNTS.get("l3.contract") == 1
    assert g._HOOK_FIRE_COUNTS.get("l3b.evidence") == 1
    saved = json.loads((tmp_path / "fc.json").read_text(encoding="utf-8"))
    assert saved["l3.contract"] == 1 and saved["l3b.evidence"] == 1


def test_fire_counter_persists_across_multiple_fires(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "_HOOK_FIRE_COUNTS", {})
    monkeypatch.setenv("GT_HOOK_FIRE_COUNTS", str(tmp_path / "fc.json"))
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "ledger.jsonl"))
    for _ in range(3):
        g._lane_a_deliver(out := {}, "cmd", [("l3.contract", "")], krel="a.py", event=None)
    assert g._HOOK_FIRE_COUNTS.get("l3.contract") == 3
    assert json.loads((tmp_path / "fc.json").read_text(encoding="utf-8"))["l3.contract"] == 3
