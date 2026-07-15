from __future__ import annotations

import json
import sys

from scripts.swebench.gt_run_metrics import (
    _MANDATORY_METRICS,
    aggregate_run_metrics,
    main,
)


def _row(task: str, resolved: bool | None, cost: float | None) -> dict:
    token_efficiency = {}
    if cost is not None:
        token_efficiency["total_cost_usd"] = cost
    return {
        "task_id": task,
        "resolved": resolved,
        "performance": {"token_efficiency": token_efficiency},
    }


def _set_applicability(
    row: dict, section: str, metric: str, *, applicable: bool,
    predicate: str = "fixture_applicability", reason: str = "fixture reason",
) -> None:
    row.setdefault("metric_applicability", {}).setdefault(section, {})[metric] = {
        "applicable": applicable,
        "predicate": predicate,
        "reason": reason,
    }


def test_cost_per_resolved_uses_total_run_cost_and_resolved_count() -> None:
    result = aggregate_run_metrics([
        _row("a", True, 1.25),
        _row("b", False, 0.75),
        _row("c", True, 2.00),
    ])

    assert result["tasks"] == 3
    assert result["resolved"] == 2
    assert result["token_efficiency"]["total_cost_usd"] == 4.0
    assert result["token_efficiency"]["cost_per_resolved"] == 2.0
    ratio = result["mandatory_performance"]["token_efficiency"]["cost_per_resolved"]
    assert ratio["applicability"] == {
        "applicable": True,
        "predicate": "cost_collection_complete and resolved_count > 0",
        "reason": "complete run cost and at least one resolved task",
    }


def test_cost_per_resolved_is_null_when_no_task_resolved() -> None:
    result = aggregate_run_metrics([
        _row("a", False, 0.25),
        _row("b", None, 0.75),
    ])

    assert result["resolved"] == 0
    assert result["token_efficiency"]["total_cost_usd"] == 1.0
    assert result["token_efficiency"]["cost_per_resolved"] is None
    ratio = result["mandatory_performance"]["token_efficiency"]["cost_per_resolved"]
    assert ratio["status"] == "NOT_APPLICABLE"
    assert ratio["applicability"] == {
        "applicable": False,
        "predicate": "cost_collection_complete and resolved_count > 0",
        "reason": "complete run cost but no resolved tasks",
    }


def test_missing_cost_is_reported_and_not_fabricated_as_zero() -> None:
    result = aggregate_run_metrics([
        _row("a", True, 1.0),
        _row("b", False, None),
    ])

    assert result["token_efficiency"]["total_cost_usd"] is None
    assert result["token_efficiency"]["cost_per_resolved"] is None
    assert result["token_efficiency"]["cost_collection_complete"] is False
    assert result["token_efficiency"]["tasks_missing_cost"] == ["b"]
    ratio = result["mandatory_performance"]["token_efficiency"]["cost_per_resolved"]
    assert ratio["value"] is None
    assert ratio["status"] == "UNMEASURED"
    assert ratio["missing_tasks"] == ["b"]
    assert ratio["applicability"] is None


def test_empty_run_is_a_collection_failure_not_a_complete_zero_task_run() -> None:
    result = aggregate_run_metrics([])

    assert result["tasks"] == 0
    assert result["mandatory_performance_collection_complete"] is False
    assert result["mandatory_performance_collection_failures"] == ["no_task_records"]
    assert result["token_efficiency"]["total_cost_usd"] is None
    assert result["token_efficiency"]["cost_collection_complete"] is False
    assert result["mandatory_performance"]["localization"]["gold_rank"][
        "status"
    ] == "UNMEASURED"


def test_expected_population_rejects_missing_duplicate_and_unexpected_tasks() -> None:
    result = aggregate_run_metrics(
        [
            _row("a", True, 1.0),
            _row("a", False, 1.0),
            _row("c", False, 1.0),
        ],
        expected_task_ids=["a", "b"],
    )

    assert result["task_population"] == {
        "expected_count": 2,
        "observed_record_count": 3,
        "observed_unique_count": 2,
        "missing_tasks": ["b"],
        "duplicate_tasks": ["a"],
        "unexpected_tasks": ["c"],
        "invalid_task_records": [],
    }
    assert result["mandatory_performance_collection_complete"] is False
    assert result["mandatory_performance_collection_failures"][:3] == [
        "missing_expected_tasks",
        "duplicate_task_records",
        "unexpected_tasks",
    ]


def test_expected_population_rejects_duplicate_expected_ids() -> None:
    result = aggregate_run_metrics(
        [_row("a", True, 1.0)], expected_task_ids=["a", "a"]
    )
    assert result["mandatory_performance_collection_complete"] is False
    assert "duplicate_expected_tasks" in result[
        "mandatory_performance_collection_failures"
    ]


def test_contract_invalid_numbers_are_missing_not_aggregated() -> None:
    invalid = _row("invalid", False, 1.0)
    valid = _row("valid", True, 1.0)
    invalid["performance"]["localization"] = {
        "files_to_gold_view": -1,
        "gold_rank": 1.5,
        "localization_precision": 1.2,
        "exploration_ratio": 2.5,
    }
    valid["performance"]["localization"] = {
        "files_to_gold_view": 2,
        "gold_rank": 3,
        "localization_precision": 0.75,
        "exploration_ratio": 3.5,
    }

    localization = aggregate_run_metrics([invalid, valid])[
        "mandatory_performance"
    ]["localization"]

    for metric in ("files_to_gold_view", "gold_rank", "localization_precision"):
        assert localization[metric]["missing_tasks"] == ["invalid"]
        assert localization[metric]["measured_tasks"] == 1
    assert localization["files_to_gold_view"]["mean"] == 2.0
    assert localization["gold_rank"]["mean"] == 3.0
    assert localization["localization_precision"]["mean"] == 0.75
    assert localization["exploration_ratio"]["mean"] == 3.0
    assert localization["exploration_ratio"]["missing_tasks"] == []


def test_raw_null_and_absent_are_unmeasured_without_explicit_applicability() -> None:
    na = _row("na", False, 1.0)
    missing = _row("missing", False, 1.0)
    na["performance"]["localization"] = {"gold_rank": None}

    metric = aggregate_run_metrics([na, missing])["mandatory_performance"][
        "localization"
    ]["gold_rank"]

    assert metric["mean"] is None
    assert metric["status"] == "UNMEASURED"
    assert metric["not_applicable_tasks"] == []
    assert metric["unmeasured_tasks"] == ["missing", "na"]
    assert metric["missing_tasks"] == ["missing", "na"]


def test_explicit_false_applicability_is_the_only_null_to_be_not_applicable() -> None:
    row = _row("na", False, 1.0)
    row["performance"]["localization"] = {"gold_rank": None}
    _set_applicability(
        row, "localization", "gold_rank", applicable=False,
        predicate="gold_rank_denominator_present", reason="no rank denominator",
    )

    metric = aggregate_run_metrics([row])["mandatory_performance"][
        "localization"
    ]["gold_rank"]

    assert metric["status"] == "NOT_APPLICABLE"
    assert metric["not_applicable_tasks"] == ["na"]
    assert metric["missing_tasks"] == []
    assert metric["applicability"]["na"] == {
        "applicable": False,
        "predicate": "gold_rank_denominator_present",
        "reason": "no rank denominator",
    }


def test_malformed_or_contradictory_applicability_fails_closed() -> None:
    malformed = _row("malformed", False, 1.0)
    malformed["performance"]["localization"] = {"gold_rank": None}
    malformed["metric_applicability"] = {
        "localization": {"gold_rank": {"applicable": False, "reason": "no denominator"}}
    }
    contradiction = _row("contradiction", False, 1.0)
    contradiction["performance"]["localization"] = {"gold_rank": 2}
    _set_applicability(
        contradiction, "localization", "gold_rank", applicable=False,
        predicate="no_rank", reason="claims no rank",
    )

    metric = aggregate_run_metrics([malformed, contradiction])["mandatory_performance"][
        "localization"
    ]["gold_rank"]

    assert metric["status"] == "FAILED"
    assert metric["failed_tasks"] == ["contradiction", "malformed"]
    assert metric["missing_tasks"] == ["contradiction", "malformed"]


def test_applicable_but_uncollected_stays_unmeasured() -> None:
    row = _row("missing", False, 1.0)
    _set_applicability(
        row, "localization", "gold_rank", applicable=True,
        predicate="gold_rank_denominator_present", reason="rank should have been collected",
    )

    metric = aggregate_run_metrics([row])["mandatory_performance"][
        "localization"
    ]["gold_rank"]

    assert metric["status"] == "UNMEASURED"
    assert metric["unmeasured_tasks"] == ["missing"]
    assert metric["failed_tasks"] == []


def test_measured_and_explicit_not_applicable_aggregate_as_measured() -> None:
    measured = _row("measured", False, 1.0)
    measured["performance"]["localization"] = {"gold_rank": 4}
    na = _row("na", False, 1.0)
    na["performance"]["localization"] = {"gold_rank": None}
    _set_applicability(
        na, "localization", "gold_rank", applicable=False,
        predicate="gold_rank_denominator_present", reason="no rank denominator",
    )

    metric = aggregate_run_metrics([measured, na])["mandatory_performance"][
        "localization"
    ]["gold_rank"]

    assert metric["status"] == "MEASURED"
    assert metric["mean"] == 4.0
    assert metric["not_applicable_tasks"] == ["na"]
    assert metric["missing_tasks"] == []


def test_cli_returns_nonzero_after_writing_empty_run_failure_artifact(
    tmp_path, monkeypatch
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"resolved_ids": []}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "gt_run_metrics.py", str(tmp_path), str(baseline), "--run-id", "empty",
    ])

    assert main() == 1
    written = json.loads((tmp_path / "gt_run_metrics_v2_empty.json").read_text())
    assert written["mandatory_performance_collection_failures"] == ["no_task_records"]


def test_cli_returns_nonzero_for_present_but_incomplete_task(
    tmp_path, monkeypatch
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"resolved_ids": []}), encoding="utf-8")
    (tmp_path / "gt_deep_metrics_only.json").write_text(
        json.dumps(_row("only", False, 1.0)), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv", [
        "gt_run_metrics.py", str(tmp_path), str(baseline), "--run-id", "partial",
    ])

    assert main() == 1
    written = json.loads((tmp_path / "gt_run_metrics_v2_partial.json").read_text())
    assert written["mandatory_performance_collection_complete"] is False
    assert "missing_or_malformed_metrics" in written[
        "mandatory_performance_collection_failures"
    ]


def test_explicit_na_record_is_collection_complete(tmp_path, monkeypatch) -> None:
    row = _row("na", False, 1.0)
    row["performance"] = {}
    for section, metrics in _MANDATORY_METRICS.items():
        target = row if section == "behavioral_impact" else row["performance"]
        target[section] = {
            name: ({} if kind == "per_tag_rate_dict" else None)
            for name, kind in metrics
            if kind != "run_ratio"
        }
        for name, kind in metrics:
            if kind != "run_ratio":
                _set_applicability(
                    row, section, name, applicable=False,
                    predicate="fixture_no_denominator", reason="fixture has no denominator",
                )
    row["performance"]["token_efficiency"]["total_cost_usd"] = 1.0
    _set_applicability(
        row, "token_efficiency", "total_cost_usd", applicable=True,
        predicate="cost_record_present", reason="fixture carries recorded cost",
    )
    row["schema"] = "gt_deep_metrics.v2"
    row["precision_decimals"] = 8
    row["performance"]["schema"] = "gt_performance_metrics.v1"
    row["performance"]["token_efficiency"]["cost_per_resolved"] = None
    row["performance"]["token_efficiency"]["cost_per_resolved_scope"] = "run_aggregate"
    row["performance"]["metric_applicability"] = {
        section: dict(contracts)
        for section, contracts in row["metric_applicability"].items()
        if section != "behavioral_impact"
    }
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"resolved_ids": []}), encoding="utf-8")
    (tmp_path / "gt_deep_metrics_na.json").write_text(json.dumps(row), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "gt_run_metrics.py", str(tmp_path), str(baseline), "--run-id", "na",
    ])

    assert main() == 0
    written = json.loads((tmp_path / "gt_run_metrics_v2_na.json").read_text())
    assert written["mandatory_performance_collection_complete"] is True
    assert written["mandatory_performance_collection_failures"] == []


def test_all_58_mandatory_perf_metrics_are_published() -> None:
    result = aggregate_run_metrics([_row("a", True, 1.0)])

    sections = result["mandatory_performance"]
    assert result["mandatory_performance_metric_count"] == 58
    assert sum(len(metrics) for metrics in sections.values()) == 58
    assert set(sections) == {
        "localization",
        "edit_quality",
        "interface_preservation",
        "scope_completeness",
        "stuck_recovery",
        "verify_before_submit",
        "behavioral_impact",
        "gt_attribution",
        "token_efficiency",
    }
    assert {section: set(metrics) for section, metrics in sections.items()} == {
        "localization": {
            "gold_in_L1_top_k", "gold_rank", "files_to_gold_view",
            "steps_to_gold_view", "files_to_gold_edit", "steps_to_gold_edit",
            "localization_precision", "localization_recall", "false_file_rate",
            "exploration_ratio", "gold_view_precision", "wasted_views",
            "navigation_directness", "self_localization_needed",
        },
        "edit_quality": {
            "edit_attempts_per_gold", "rewrite_count", "compile_failures_after_edit",
            "edit_revert_rate", "first_edit_correctness", "patch_size", "patch_files",
        },
        "interface_preservation": {
            "contract_compliance_rate", "signature_changes_warned",
            "p2p_regression_rate", "caller_breakage_count",
        },
        "scope_completeness": {
            "scope_coverage", "scope_excess", "multi_file_discovery", "scope_gap_files",
        },
        "stuck_recovery": {
            "degenerate_loop_count", "steps_in_loops", "nudge_recovery_steps",
            "coherence_collapse_count", "stuck_duration",
        },
        "verify_before_submit": {
            "test_before_submit", "test_runs_total", "test_edit_ratio",
            "obligation_test_rate", "verify_gap",
        },
        "behavioral_impact": {
            "impact_rate", "per_tag_impact", "gt_tokens_injected",
            "gt_tokens_per_pivot", "nudge_compliance_rate",
        },
        "gt_attribution": {
            "L1_followed_rate", "contract_consulted_rate",
            "obligation_completion_rate", "nudge_action_rate", "scope_chain_followed",
        },
        "token_efficiency": {
            "total_steps", "total_tokens_in", "total_tokens_out", "total_cost_usd",
            "cache_hit_rate", "tokens_per_gold_edit", "cost_per_resolved",
            "gt_token_overhead", "wasted_token_rate",
        },
    }
    assert sections["localization"]["gold_rank"]["mean"] is None
    assert sections["localization"]["gold_rank"]["missing_tasks"] == ["a"]


def test_numeric_missing_and_boolean_values_have_explicit_semantics() -> None:
    a = _row("a", True, 1.0)
    b = _row("b", False, 3.0)
    c = _row("c", False, 5.0)
    a["performance"]["localization"] = {
        "gold_rank": 1,
        "gold_in_L1_top_k": True,
    }
    b["performance"]["localization"] = {
        "gold_rank": None,
        "gold_in_L1_top_k": False,
    }
    _set_applicability(
        b, "localization", "gold_rank", applicable=False,
        predicate="rank_denominator_present", reason="no rank denominator",
    )
    c["performance"]["localization"] = {
        "gold_rank": 5,
        "gold_in_L1_top_k": True,
    }

    result = aggregate_run_metrics([a, b, c])
    localization = result["mandatory_performance"]["localization"]

    assert localization["gold_rank"]["mean"] == 3.0
    assert localization["gold_rank"]["median"] == 3.0
    assert localization["gold_rank"]["measured_tasks"] == 2
    assert localization["gold_rank"]["missing_tasks"] == []
    assert localization["gold_rank"]["not_applicable_tasks"] == ["b"]
    top_k = localization["gold_in_L1_top_k"]
    assert top_k["value_type"] == "bool"
    assert top_k["semantics"] == "true=1,false=0; mean is true_rate"
    assert top_k["mean"] == 0.66666667
    assert top_k["median"] == 1.0


def test_first_edit_correctness_reduces_each_task_file_map_to_true_rate() -> None:
    a = _row("a", True, 1.0)
    b = _row("b", False, 1.0)
    c = _row("c", False, 1.0)
    a["performance"]["edit_quality"] = {
        "first_edit_correctness": {"one.py": True, "two.py": False},
    }
    b["performance"]["edit_quality"] = {
        "first_edit_correctness": {"three.py": True},
    }
    c["performance"]["edit_quality"] = {"first_edit_correctness": {}}
    _set_applicability(
        c, "edit_quality", "first_edit_correctness", applicable=False,
        predicate="edited_file_present", reason="no edited files",
    )

    metric = aggregate_run_metrics([a, b, c])["mandatory_performance"][
        "edit_quality"
    ]["first_edit_correctness"]

    assert metric["value_type"] == "bool_per_file"
    assert metric["semantics"] == "per-task fraction of file outcomes that are true"
    assert metric["mean"] == 0.75
    assert metric["median"] == 0.75
    assert metric["missing_tasks"] == []
    assert metric["not_applicable_tasks"] == ["c"]


def test_per_tag_impact_reports_task_distribution_and_pooled_denominator() -> None:
    a = _row("a", True, 1.0)
    b = _row("b", False, 1.0)
    c = _row("c", False, 1.0)
    d = _row("d", False, 1.0)
    a["behavioral_impact"] = {
        "per_tag_impact": {"recovery": {"total": 2, "pivots": 1, "rate": 0.5}},
    }
    b["behavioral_impact"] = {
        "per_tag_impact": {"recovery": {"total": 8, "pivots": 8, "rate": 1.0}},
    }
    c["behavioral_impact"] = {"per_tag_impact": {}}
    _set_applicability(
        c, "behavioral_impact", "per_tag_impact", applicable=False,
        predicate="gt_delivery_present", reason="no GT deliveries",
    )

    metric = aggregate_run_metrics([a, b, c, d])["mandatory_performance"][
        "behavioral_impact"
    ]["per_tag_impact"]
    recovery = metric["tags"]["recovery"]

    assert metric["value_type"] == "per_tag_rate_dict"
    assert recovery["mean"] == 0.75
    assert recovery["median"] == 0.75
    assert recovery["pooled_rate"] == 0.9
    assert recovery["total_deliveries"] == 10
    assert recovery["total_pivots"] == 9
    assert recovery["missing_tasks"] == ["d"]
    assert recovery["not_applicable_tasks"] == ["c"]
    assert metric["missing_tasks"] == ["d"]


def test_per_tag_impact_rejects_rate_that_disagrees_with_counts() -> None:
    row = _row("mismatched-rate", False, 1.0)
    row["behavioral_impact"] = {
        "per_tag_impact": {
            "recovery": {"total": 2, "pivots": 1, "rate": 1.0},
        },
    }

    metric = aggregate_run_metrics([row])["mandatory_performance"][
        "behavioral_impact"
    ]["per_tag_impact"]

    assert metric["status"] == "FAILED"
    assert metric["failed_tasks"] == ["mismatched-rate"]
    assert metric["tags"] == {}


def test_cost_per_resolved_is_one_run_ratio_not_task_mean() -> None:
    result = aggregate_run_metrics([
        _row("a", True, 1.0),
        _row("b", False, 3.0),
        _row("c", True, 4.0),
    ])

    metric = result["mandatory_performance"]["token_efficiency"][
        "cost_per_resolved"
    ]
    assert metric["aggregation"] == "ratio_of_run_total_cost_to_resolved_count"
    assert metric["value"] == 4.0
    assert metric["mean"] is None
    assert metric["median"] is None
