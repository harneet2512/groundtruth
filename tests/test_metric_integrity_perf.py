"""Measurement-integrity pins for scripts/swebench/gt_performance_metrics.py.

Each test names the dishonest-metric bug it guards (G1/G2/G8/G14). Reverting the
fix reddens the named test. Loaded by path — scripts/ is not a package.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import tempfile
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "scripts" / "swebench" / "gt_performance_metrics.py"
_spec = importlib.util.spec_from_file_location("gt_performance_metrics", _MOD)
assert _spec and _spec.loader
pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm)


def _assistant(cmd_args: dict) -> dict:
    return {
        "role": "assistant",
        "content": "step",
        "tool_calls": [{"function": {"arguments": json.dumps(cmd_args)}}],
    }


# --------------------------------------------------------------------------- G14
def test_g14_d8_missing_is_null_not_zero() -> None:
    assert pm.d8(None) is None
    assert pm.d8(float("nan")) is None
    assert pm.d8(float("inf")) is None
    assert pm.d8("not-a-number") is None
    # a real number still rounds to 8 dp
    assert pm.d8(1 / 3) == round(1 / 3, 8)
    assert pm.d8(0) == 0.0  # a genuine measured zero is still 0.0, not None


# --------------------------------------------------------------------------- G8
def test_g8_contract_compliance_null_when_no_warnings() -> None:
    """0 warnings delivered -> NOT a perfect 1.0; the layer never fired -> null."""
    s3 = pm._compute_interface_preservation([])
    assert s3["contract_compliance_rate"] is None


def test_g8_obligation_and_nudge_rates_null_when_nothing_delivered() -> None:
    s6 = pm._compute_verify_before_submit([])
    assert s6["obligation_test_rate"] is None
    s8 = pm._compute_gt_attribution([], "")
    assert s8["nudge_action_rate"] is None


# --------------------------------------------------------------------------- G2
def test_g2_gold_never_reached_makes_to_gold_null_not_zero() -> None:
    """A run that never views gold must be distinguishable from one that hits it
    at step 1 — the *_to_gold fields are null, NOT 0 (which reads as perfect)."""
    fixture = {
        "messages": [
            _assistant({"command": "view", "path": "src/other.py"}),
            {"role": "tool", "content": "def other(): pass"},
            _assistant({"command": "str_replace", "path": "src/other.py",
                        "old_str": "a", "new_str": "b"}),
            {"role": "tool", "content": "File updated."},
        ],
        "info": {"submission": "diff --git a/src/other.py b/src/other.py\n+b\n-a"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(fixture, f)
        # TRUE gold that the agent never touches
        res = pm.compute_performance_metrics(tp, tmp, gold_files=["src/gold.py"])
    loc = res["localization"]
    assert loc["gold_never_reached"] is True
    assert loc["first_gold_view_step"] is None
    assert loc["files_to_gold_view"] is None
    assert loc["steps_to_gold_view"] is None
    assert loc["files_to_gold_edit"] is None


# --------------------------------------------------------------------------- G1
def test_g1_submission_proxy_gold_nulls_localization_metrics() -> None:
    """gold_files=None + a submission -> 'gold' is the agent's OWN diff (circular).
    gold_source must say so and every gold-derived metric must be null, not a
    fabricated score."""
    fixture = {
        "messages": [
            _assistant({"command": "view", "path": "src/foo.py"}),
            {"role": "tool", "content": "def foo(): pass"},
            _assistant({"command": "str_replace", "path": "src/foo.py",
                        "old_str": "a", "new_str": "b"}),
            {"role": "tool", "content": "File updated."},
        ],
        "info": {"submission": "diff --git a/src/foo.py b/src/foo.py\n+b\n-a"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(fixture, f)
        res = pm.compute_performance_metrics(tp, tmp, gold_files=None)
    assert res["gold_source"] == "submission_proxy"
    assert res["gold_is_proxy"] is True
    loc = res["localization"]
    assert loc["localization_precision"] is None
    assert loc["localization_recall"] is None
    assert loc["files_to_gold_view"] is None
    assert res["scope_completeness"]["scope_coverage"] is None
    assert res["token_efficiency"]["tokens_per_gold_edit"] is None


def test_g1_true_gold_is_not_nulled() -> None:
    """When gold is passed explicitly it is true_gold — metrics stay populated."""
    fixture = {
        "messages": [
            _assistant({"command": "view", "path": "src/foo.py"}),
            {"role": "tool", "content": "x"},
            _assistant({"command": "str_replace", "path": "src/foo.py",
                        "old_str": "a", "new_str": "b"}),
            {"role": "tool", "content": "File updated."},
        ],
        "info": {"submission": "diff --git a/src/foo.py b/src/foo.py\n+b\n-a"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(fixture, f)
        res = pm.compute_performance_metrics(tp, tmp, gold_files=["src/foo.py"])
    assert res["gold_source"] == "true_gold"
    assert res["gold_is_proxy"] is False
    # gold WAS edited -> precision is a real number, not null
    assert res["localization"]["localization_precision"] == 1.0
