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


def test_cost_per_resolved_is_explicitly_deferred_to_run_aggregate() -> None:
    result = pm._compute_token_efficiency({}, [], [])

    assert "cost_per_resolved" in result
    assert result["cost_per_resolved"] is None
    assert result["cost_per_resolved_scope"] == "run_aggregate"


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


def test_authoritative_quiet_ledger_does_not_fall_back_to_tag_heuristic() -> None:
    timeline = [
        {"role": "observation", "step": 0,
         "contract_content": "[CALLERS] apparent legacy contract"},
        {"role": "assistant", "step": 1, "is_edit": True,
         "edited_file": "src/x.py"},
    ]
    authoritative = {
        "runtime_ledger_path": "gt_runtime_ledger.jsonl",
        "entries": [{
            "source": "trajectory", "joined": True, "receipt": 1,
            "kind": "recovery", "msg_index": 0,
        }],
    }

    sealed = pm._compute_interface_preservation(timeline, authoritative)
    legacy = pm._compute_interface_preservation(timeline, None)

    assert sealed["_contract_warning_count"] == 0
    assert sealed["contract_compliance_rate"] is None
    assert legacy["_contract_warning_count"] == 1
    assert legacy["contract_compliance_rate"] == 1.0


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


def test_g1_no_gold_nulls_every_mandatory_gold_dependent_metric() -> None:
    """Blind runs report N/A; empty gold is not evidence of a perfect/failed score."""
    fixture = {
        "messages": [
            _assistant({"command": "grep -r 'thing' src/"}),
            {"role": "tool", "content": "src/other.py:def thing(): pass"},
            _assistant({"command": "view", "path": "src/other.py"}),
            {"role": "tool", "content": "def thing(): pass"},
        ],
        "info": {"submission": ""},
    }
    with tempfile.TemporaryDirectory() as tmp:
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(fixture, f)
        res = pm.compute_performance_metrics(tp, tmp, gold_files=None)

    assert res["gold_source"] == "none"
    mandatory_localization = {
        "gold_in_L1_top_k", "gold_rank", "files_to_gold_view",
        "steps_to_gold_view", "files_to_gold_edit", "steps_to_gold_edit",
        "localization_precision", "localization_recall", "false_file_rate",
        "exploration_ratio", "gold_view_precision", "wasted_views",
        "navigation_directness", "self_localization_needed",
    }
    assert all(res["localization"][name] is None for name in mandatory_localization)
    assert all(value is None for name, value in res["scope_completeness"].items()
               if not name.startswith("_"))
    assert res["edit_quality"]["edit_attempts_per_gold"] is None
    assert res["edit_quality"]["first_edit_correctness"] is None
    assert res["token_efficiency"]["tokens_per_gold_edit"] is None
    assert res["token_efficiency"]["wasted_token_rate"] is None


# ---------------------------------------------------------- applicability contract
def test_absent_denominators_emit_explicit_applicability_contracts() -> None:
    """Known-empty denominators are N/A only through an auditable predicate."""
    fixture = {"messages": [], "info": {"submission": ""}}
    with tempfile.TemporaryDirectory() as tmp:
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(fixture, f)
        result = pm.compute_performance_metrics(tp, tmp, gold_files=None)

    applicability = result["metric_applicability"]
    for section, metric in (
        ("localization", "steps_to_gold_view"),
        ("edit_quality", "first_edit_correctness"),
        ("interface_preservation", "contract_compliance_rate"),
        ("verify_before_submit", "obligation_test_rate"),
        ("gt_attribution", "nudge_action_rate"),
    ):
        contract = applicability[section][metric]
        assert contract["applicable"] is False
        assert contract["predicate"]
        assert contract["reason"]


def test_true_gold_never_reached_is_applicable_not_promoted_to_na() -> None:
    """Failure to reach known gold is not an absent denominator or collection miss."""
    fixture = {
        "messages": [_assistant({"command": "view", "path": "src/other.py"})],
        "info": {"submission": ""},
    }
    with tempfile.TemporaryDirectory() as tmp:
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(fixture, f)
        result = pm.compute_performance_metrics(
            tp, tmp, gold_files=["src/gold.py"]
        )

    assert result["localization"]["steps_to_gold_view"] is None
    contract = result["metric_applicability"]["localization"][
        "steps_to_gold_view"
    ]
    assert contract["applicable"] is True
    assert contract["predicate"] == "n_gold_files > 0"


def test_behavioral_applicability_is_denominator_and_collection_driven() -> None:
    performance = {"gold_source": "true_gold", "n_gold_files": 1}
    quiet = {
        "total_deliveries": 0,
        "total_pivots": 0,
        "per_tag_impact": {},
        "gt_tokens_per_pivot": None,
        "nudge_compliance_rate": None,
    }

    applicability = pm.build_metric_applicability(
        performance, quiet, behavioral_collection_succeeded=True
    )
    assert applicability["behavioral_impact"]["per_tag_impact"]["applicable"] is False
    assert applicability["behavioral_impact"]["gt_tokens_per_pivot"]["applicable"] is False
    assert applicability["behavioral_impact"]["nudge_compliance_rate"]["applicable"] is False

    failed = dict(quiet, collection_error="ledger_failed")
    failed_applicability = pm.build_metric_applicability(
        performance, failed, behavioral_collection_succeeded=False
    )
    assert "behavioral_impact" not in failed_applicability


def test_missing_trajectory_collection_remains_unmeasured() -> None:
    result = pm.compute_performance_metrics(
        "definitely-missing-trajectory.json", "", gold_files=None
    )

    assert result["status"] == "UNMEASURED"
    assert result["reason"] == "trajectory_unavailable_or_invalid"
    assert "metric_applicability" not in result
