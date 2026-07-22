from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_dispatch_feature_preflight as preflight  # noqa: E402
import gt_feature_inventory as inventory  # noqa: E402
import gt_feature_metrics as feature_metrics  # noqa: E402
import gt_deep_metrics as deep_metrics  # noqa: E402
import gt_performance_metrics as performance  # noqa: E402
import gt_run_metrics as run_metrics  # noqa: E402
from groundtruth.runtime.rl_profile import PROFILE_MEMBERS  # noqa: E402


def _trajectory(tmp_path: Path, *, model_stats: dict | None = None) -> Path:
    path = tmp_path / "mini-swe-agent.trajectory.json"
    path.write_text(json.dumps({
        "messages": [], "info": {"model_stats": model_stats or {}, "submission": ""},
    }), encoding="utf-8")
    return path


def test_static_perf_authorities_name_real_producers() -> None:
    perf = {
        name: row for name, row in preflight.build_static_dispatch_manifest()["features"].items()
        if row["family"] == "PERF"
    }
    for name in (
        "impact_rate", "per_tag_impact", "gt_tokens_injected",
        "gt_tokens_per_pivot", "nudge_compliance_rate",
    ):
        assert perf[name]["producer_authority"] == (
            "gt_behavioral_impact.analyze_trajectory"
        )
    for name in ("p2p_regression_rate", "caller_breakage_count"):
        assert perf[name]["producer_authority"] == (
            "task_truth._build_verifier_truth"
        )


def test_missing_verifier_cost_and_behavioral_failure_are_not_measured(tmp_path: Path) -> None:
    result = performance.compute_performance_metrics(
        str(_trajectory(tmp_path)), str(tmp_path), gold_files=["src/x.py"],
        consumption_ledger={
            "schema": "gt.consumption_ledger.v2", "runtime_ledger_path": "ledger", "entries": [],
        },
    )
    assert result["interface_preservation"]["p2p_regression_rate"] is None
    assert result["interface_preservation"]["caller_breakage_count"] is None
    assert result["token_efficiency"]["total_cost_usd"] is None

    deep = {
        "schema": "gt_deep_metrics.v2", "precision_decimals": 8, "task_id": "probe",
        "performance": result,
        "behavioral_impact": {
            "impact_rate": 0.0, "per_tag_impact": {}, "gt_tokens_injected": None,
            "gt_tokens_per_pivot": None, "nudge_compliance_rate": None,
            "collection_error": "ProbeError",
        },
        "metric_applicability": result.get("metric_applicability", {}),
    }
    (tmp_path / "gt_deep_metrics_probe.json").write_text(json.dumps(deep), encoding="utf-8")
    rows, missing, _ = feature_metrics._performance_feature_records("probe", str(tmp_path))
    for name in (
        "p2p_regression_rate", "caller_breakage_count", "total_cost_usd", "impact_rate",
    ):
        assert rows[name]["status"] == "UNMEASURED"
        assert f"PERF.{name}" in missing


def test_verifier_interface_metrics_require_canonical_truth() -> None:
    assert deep_metrics._verifier_interface_metrics({}) == {
        "p2p_regression_rate": None, "caller_breakage_count": None,
    }
    assert deep_metrics._verifier_interface_metrics({
        "verifier_truth": {
            "schema": "gt.verifier_truth.v1", "valid": True,
            "p2p_total": 4, "p2p_failed": 1,
            "caller_breakage_count": 2,
        },
    }) == {"p2p_regression_rate": 0.25, "caller_breakage_count": 2}
    assert deep_metrics._verifier_interface_metrics({
        "verifier_truth": {
            "schema": "gt.verifier_truth.v1", "valid": True,
            "p2p_total": 0, "p2p_failed": 0,
            "caller_breakage_count": -1,
        },
    }) == {"p2p_regression_rate": None, "caller_breakage_count": None}


def test_behavioral_impact_rate_requires_delivery_denominator() -> None:
    contracts = performance.build_metric_applicability(
        {}, {"total_deliveries": 0, "total_pivots": 0},
        behavioral_collection_succeeded=True,
    )
    assert contracts["behavioral_impact"]["impact_rate"]["applicable"] is False


def test_verify_gap_counts_step_zero_edit_without_later_test() -> None:
    result = performance._compute_verify_before_submit([
        {"role": "assistant", "step": 0, "is_edit": True, "is_test": False},
    ])
    assert result["verify_gap"] == 1


def test_run_aggregator_rejects_noncanonical_deep_record() -> None:
    row = {"task_id": "forged", "resolved": False, "performance": {}, "metric_applicability": {}}
    for section, definitions in run_metrics._MANDATORY_METRICS.items():
        target = row if section == "behavioral_impact" else row["performance"]
        target[section] = {}
        for name, value_type in definitions:
            if value_type == "run_ratio":
                continue
            target[section][name] = {} if value_type in {"bool_per_file", "per_tag_rate_dict"} else None
            row["metric_applicability"].setdefault(section, {})[name] = {
                "applicable": False, "predicate": "forged", "reason": "forged",
            }
    row["performance"]["token_efficiency"]["total_cost_usd"] = 1.0
    row["metric_applicability"]["token_efficiency"]["total_cost_usd"] = {
        "applicable": True, "predicate": "forged", "reason": "forged",
    }

    result = run_metrics.aggregate_run_metrics([row], expected_task_ids=["forged"])
    assert result["mandatory_performance_collection_complete"] is False
    assert "invalid_deep_metric_records" in result["mandatory_performance_collection_failures"]


def _feature_task_record() -> dict:
    canonical = inventory.canonical_feature_inventory()
    ss_features = {
        name: {"family": family, "status": "MEASURED"}
        for family, names in canonical.items() for name in names
    }
    for name, row in ss_features.items():
        if row["family"] == "PERF":
            row.update({
                "source": "gt_deep_metrics", "source_artifact": "gt_deep_metrics_t.json",
                "value_type": "rate", "artifact_schema_valid": True,
                "precision_decimals": 8, "value_precision_valid": True,
                "metric_structure_valid": True, "formula_provenance": "MANDATORY_METRICS.md",
                "denominator_provenance": "mandatory_contract", "coverage_scope": "task",
            })
    features = {
        member: {
            "verdict": "DARK",
            "lifecycle": {
                "eligible": {"value": None}, "delivered": {"value": None},
                "state_changed": {"value": None},
            },
        }
        for member in PROFILE_MEMBERS["2"]
    }
    return {
        "task": "t", "ss_features": ss_features, "features": features,
        "ss_integrity": {"inventory_complete": True, "required_inputs_complete": True},
        "integrity": {"all_members_present": True, "leak_count": 0, "dose_violation_count": 0},
        "atomic_events": [], "behavioural_endpoints": {},
    }


def test_all_perf_aggregate_coverage_requires_canonical_run_artifact() -> None:
    result = feature_metrics.aggregate_run("run", [_feature_task_record()], run_metrics_artifact=None)
    perf = result["run_metrics"]["ss_features"]
    assert all(
        perf[name]["ss_readiness"]["gates"]["aggregate_coverage"] is False
        for name in inventory.canonical_feature_inventory()["PERF"]
    )
    assert result["ss_integrity"]["publishable"] is False


@pytest.mark.parametrize(
    ("section", "metric"),
    [
        ("edit_quality", "revert_commands_per_edit"),
        ("localization", "localization_precision"),
        ("localization", "false_file_rate"),
        ("localization", "exploration_ratio"),
        ("localization", "gold_view_precision"),
        ("edit_quality", "edit_attempts_per_gold"),
        ("scope_completeness", "scope_excess"),
        ("scope_completeness", "multi_file_discovery"),
        ("stuck_recovery", "nudge_recovery_steps"),
        ("verify_before_submit", "test_edit_ratio"),
        ("verify_before_submit", "verify_gap"),
        ("gt_attribution", "L1_followed_rate"),
        ("gt_attribution", "contract_consulted_rate"),
        ("gt_attribution", "obligation_completion_rate"),
        ("gt_attribution", "scope_chain_followed"),
    ],
)
def test_explicitly_absent_denominator_is_not_applicable(
    tmp_path: Path, section: str, metric: str,
) -> None:
    result = performance.compute_performance_metrics(
        str(_trajectory(tmp_path)), str(tmp_path), gold_files=["src/x.py"],
        consumption_ledger={
            "schema": "gt.consumption_ledger.v2", "runtime_ledger_path": "ledger", "entries": [],
        },
    )
    assert result[section][metric] is None
    assert result["metric_applicability"][section][metric]["applicable"] is False


def test_local_recompute_uses_diagnosis_expected_population() -> None:
    script = (ROOT / "scripts" / "swebench" / "run_local_deep_metrics.sh").read_text(encoding="utf-8")
    assert "ll-full-" in script
    assert 'grep -E "^(ll-full-|gt-diagnosis-$RUN_ID$)"' in script
    assert 'grep -E "^(task-' not in script
    assert "gt-diagnosis-$RUN_ID" in script
    assert "expected_tasks.json" in script
    run_metrics = '"$MET/gt_run_metrics_v2_${RUN_ID}.json"'
    assert f'RUN_METRICS={run_metrics}' in script
    assert script.count('--expected-tasks-file "$EXPECTED_TASKS"') == 2
    assert 'gt_feature_metrics.py" "$POP"' in script
    assert '--run-metrics-artifact "$RUN_METRICS"' in script
    assert script.count('ss_live_diagnosis.py" "$POP"') == 2
    assert '--run-metrics "$RUN_METRICS"' in script
    assert '--expected-tasks "$EXPECTED_TASKS"' in script
    assert 'ss_live_diagnosis_${RUN_ID}.json' in script
    assert 'ss_live_diagnosis_${RUN_ID}.md' in script
    assert "AGGREGATE.json" not in script

    run_stage = script.index('gt_run_metrics.py" "$POP"')
    feature_stage = script.index('gt_feature_metrics.py" "$POP"')
    diagnosis_stage = script.index('ss_live_diagnosis.py" "$POP"')
    assert run_stage < feature_stage < diagnosis_stage
