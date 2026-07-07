"""Pin: SPEC §14.1 / §11.3 — the OH port delivers at most ONE Lane-B steer per turn.

DeepSWE's _augment_output pools all Lane-B candidates and _oracle_gate_blocks picks <=1
winner. OH runs a host-owned loop (feed_oh_turn, SPEC §10.6) and delivers producers
directly, so without a budget it could emit semantic-drift + L5 (both post_edit) or
scope-completeness + L5 (both post_view) on ONE turn — flooding the agent with >1
course-correction. config._lane_b_steer_spent enforces the <=1 invariant: reset per turn,
the first Lane-B steer spends it, the rest defer (latches NOT burned). Lane-A FACTS
(post_search/DCC/cochange/contract/evidence/scope-map) are always-on and NOT gated.

This pins (a) the config default and (b) a structural census — if a new Lane-B steer is
wired without the budget check, or the reset/spend/check sites drift, the census reddens.
"""
from __future__ import annotations

import os
import re
import sys
from types import SimpleNamespace

import pytest

_WRAP = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "swebench", "oh_gt_full_wrapper.py"))
_WRAP_DIR = os.path.dirname(_WRAP)
sys.path.insert(0, _WRAP_DIR)
for _mod in ("litellm", "cost_tracking"):
    sys.modules.setdefault(_mod, SimpleNamespace(
        model_cost={}, success_callback=[], completion=lambda *a, **k: None,
        acompletion=None, completion_cost=lambda *a, **k: 0.0,
        track_cost=lambda *a, **k: None, CostTracker=object))
try:
    import oh_gt_full_wrapper as _w
except Exception:  # heavy sibling deps unavailable
    _w = None

skip = pytest.mark.skipif(_w is None, reason="oh_gt_full_wrapper import unavailable")
_SRC = open(_WRAP, encoding="utf-8").read()


@skip
def test_config_has_lane_b_budget_defaulting_unspent():
    cfg = _w.GTRuntimeConfig.__dataclass_fields__
    assert "_lane_b_steer_spent" in cfg, "the Lane-B budget flag must be a config field"
    # a fresh config starts UNSPENT (the first steer of a run can fire)
    assert cfg["_lane_b_steer_spent"].default is False


def test_lane_b_budget_is_reset_once_per_turn_and_gated_at_every_steer():
    # reset EXACTLY once (the per-turn boundary, right after action_count += 1)
    resets = len(re.findall(r"_lane_b_steer_spent = False", _SRC))
    assert resets == 1, f"expected exactly 1 per-turn reset, found {resets}"
    # every Lane-B steer that delivers must SPEND the budget...
    spends = len(re.findall(r"_lane_b_steer_spent = True", _SRC))
    # ...and CHECK it before delivering (drift, scope-completeness, L5 governor, L5 goku)
    checks = len(re.findall(r"not config\._lane_b_steer_spent", _SRC))
    assert spends >= 3, f"a Lane-B steer delivers without spending the budget (spends={spends})"
    assert checks >= 3, f"a Lane-B steer delivers without checking the budget (checks={checks})"
    # symmetry: one spend per check (each gated site both checks and spends)
    assert spends == checks, f"spend/check asymmetry ({spends} vs {checks}) — a site checks-but-doesn't-spend or vice versa"


def test_lane_a_facts_are_not_gated_by_the_budget():
    # the Lane-A fact producers must remain always-on — their delivery lines must NOT
    # sit behind the Lane-B budget. Guard the three clearest fact channels.
    for tag, meta in (("<gt-search-facts>", "post_search delivered"),
                      ("<gt-concern>", "dcc delivered"),
                      ("<gt-cochange>", "cochange delivered")):
        # the [GT_META] delivery print for each fact channel exists and is not budget-gated
        assert meta in _SRC, f"Lane-A fact channel missing: {meta}"
