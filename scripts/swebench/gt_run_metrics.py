#!/usr/bin/env python3
"""Aggregate the 58 mandatory per-task PERF metrics into a run record.

Missing and malformed task values remain explicit unavailable/failed entries and never enter
a distribution as zero. ``NOT_APPLICABLE`` requires an explicit per-task
``metric_applicability[section][metric]`` contract containing ``applicable``,
``predicate``, and ``reason``; raw null/absence is only ``UNMEASURED``. Cost is
additionally fail-closed across the whole run.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import statistics
from collections import Counter
from typing import Any, Iterable


# Exact sections 1-9 PERF contract from MANDATORY_METRICS.md.  Values are the
# producer's native shapes; reducers below make the two structured metrics
# explicit instead of coercing dictionaries into fabricated scalar zeroes.
_MANDATORY_METRICS: dict[str, tuple[tuple[str, str], ...]] = {
    "localization": (
        ("gold_in_L1_top_k", "bool"),
        ("gold_rank", "nonnegative_int"),
        ("files_to_gold_view", "nonnegative_int"),
        ("steps_to_gold_view", "nonnegative_int"),
        ("files_to_gold_edit", "nonnegative_int"),
        ("steps_to_gold_edit", "nonnegative_int"),
        ("localization_precision", "rate"),
        ("localization_recall", "rate"),
        ("false_file_rate", "rate"),
        ("exploration_ratio", "nonnegative_number"),
        ("gold_view_precision", "rate"),
        ("wasted_views", "nonnegative_int"),
        ("navigation_directness", "rate"),
        ("self_localization_needed", "nonnegative_int"),
    ),
    "edit_quality": (
        ("edit_attempts_per_gold", "nonnegative_number"),
        ("rewrite_count", "nonnegative_int"),
        ("compile_failures_after_edit", "nonnegative_int"),
        ("edit_revert_rate", "rate"),
        ("first_edit_correctness", "bool_per_file"),
        ("patch_size", "nonnegative_int"),
        ("patch_files", "nonnegative_int"),
    ),
    "interface_preservation": (
        ("contract_compliance_rate", "rate"),
        ("signature_changes_warned", "nonnegative_int"),
        ("p2p_regression_rate", "rate"),
        ("caller_breakage_count", "nonnegative_int"),
    ),
    "scope_completeness": (
        ("scope_coverage", "rate"),
        ("scope_excess", "rate"),
        ("multi_file_discovery", "bool"),
        ("scope_gap_files", "nonnegative_int"),
    ),
    "stuck_recovery": (
        ("degenerate_loop_count", "nonnegative_int"),
        ("steps_in_loops", "nonnegative_int"),
        ("nudge_recovery_steps", "nonnegative_int"),
        ("coherence_collapse_count", "nonnegative_int"),
        ("stuck_duration", "nonnegative_int"),
    ),
    "verify_before_submit": (
        ("test_before_submit", "bool"),
        ("test_runs_total", "nonnegative_int"),
        ("test_edit_ratio", "nonnegative_number"),
        ("obligation_test_rate", "rate"),
        ("verify_gap", "nonnegative_int"),
    ),
    "behavioral_impact": (
        ("impact_rate", "rate"),
        ("per_tag_impact", "per_tag_rate_dict"),
        ("gt_tokens_injected", "nonnegative_int"),
        ("gt_tokens_per_pivot", "nonnegative_number"),
        ("nudge_compliance_rate", "rate"),
    ),
    "gt_attribution": (
        ("L1_followed_rate", "rate"),
        ("contract_consulted_rate", "rate"),
        ("obligation_completion_rate", "rate"),
        ("nudge_action_rate", "rate"),
        ("scope_chain_followed", "rate"),
    ),
    "token_efficiency": (
        ("total_steps", "nonnegative_int"),
        ("total_tokens_in", "nonnegative_int"),
        ("total_tokens_out", "nonnegative_int"),
        ("total_cost_usd", "nonnegative_number"),
        ("cache_hit_rate", "rate"),
        ("tokens_per_gold_edit", "nonnegative_number"),
        ("cost_per_resolved", "run_ratio"),
        ("gt_token_overhead", "rate"),
        ("wasted_token_rate", "rate"),
    ),
}
_MANDATORY_METRIC_COUNT = sum(len(metrics) for metrics in _MANDATORY_METRICS.values())
assert _MANDATORY_METRIC_COUNT == 58


def _d8(value: float) -> float:
    return round(float(value), 8)


def _task_cost(row: dict) -> float | None:
    value = (
        (row.get("performance") or {})
        .get("token_efficiency", {})
        .get("total_cost_usd")
    )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _task_name(row: dict) -> str:
    return str(row.get("task_id") or "<unknown>")


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _contract_number(value: object, value_type: str) -> float | None:
    result = _finite_number(value)
    if result is None:
        return None
    if value_type == "rate":
        return result if 0.0 <= result <= 1.0 else None
    if value_type == "nonnegative_int":
        return result if result >= 0.0 and result.is_integer() else None
    if value_type == "nonnegative_number":
        return result if result >= 0.0 else None
    raise ValueError(f"unsupported mandatory metric value type: {value_type}")


def _normalized_per_tag_rate_dict(
    value: object,
) -> dict[str, tuple[int, int, float]] | None:
    """Validate the canonical per-tag impact mapping.

    The producer emits ``tag -> {total, pivots, rate}``. Counts own the
    denominator and ``rate`` must be their 8-decimal quotient; accepting either
    a scalar-only map or a disagreeing redundant rate would give the task and
    run collectors different meanings for the same mandatory metric.
    """
    if not isinstance(value, dict) or not value:
        return None
    normalized: dict[str, tuple[int, int, float]] = {}
    for tag, counts in value.items():
        if not isinstance(tag, str) or not tag or not isinstance(counts, dict):
            return None
        if not {"total", "pivots", "rate"}.issubset(counts):
            return None
        total = _finite_number(counts.get("total"))
        pivots = _finite_number(counts.get("pivots"))
        rate = _finite_number(counts.get("rate"))
        if (
            total is None
            or pivots is None
            or rate is None
            or not total.is_integer()
            or not pivots.is_integer()
            or total <= 0
            or pivots < 0
            or pivots > total
        ):
            return None
        expected_rate = _d8(pivots / total)
        if _d8(rate) != expected_rate:
            return None
        normalized[tag] = (int(total), int(pivots), expected_rate)
    return normalized


def _section(row: dict, section: str) -> dict:
    if section == "behavioral_impact":
        value = row.get(section)
    else:
        performance = row.get("performance")
        value = performance.get(section) if isinstance(performance, dict) else None
    return value if isinstance(value, dict) else {}


_ABSENT = object()

_RIGHT_CENSORED_METRICS: dict[tuple[str, str], tuple[str, str]] = {
    ("localization", "files_to_gold_view"): ("first_gold_view", "unique_files"),
    ("localization", "steps_to_gold_view"): ("first_gold_view", "assistant_steps"),
    ("localization", "files_to_gold_edit"): ("first_gold_edit", "unique_files"),
    ("localization", "steps_to_gold_edit"): ("first_gold_edit", "assistant_steps"),
}
_RIGHT_CENSOR_TERMINALS = {"Submitted", "LimitsExceeded"}


def normalized_right_censor_observation(
    section: str, metric: str, observation: object,
) -> dict[str, Any] | None:
    """Validate and normalize one non-event terminal-horizon observation."""
    expected = _RIGHT_CENSORED_METRICS.get((section, metric))
    if (
        expected is None
        or not isinstance(observation, dict)
        or observation.get("state") != "RIGHT_CENSORED"
        or observation.get("event") != expected[0]
        or observation.get("clock") != expected[1]
        or observation.get("terminal_status") not in _RIGHT_CENSOR_TERMINALS
    ):
        return None
    lower_bound = observation.get("lower_bound")
    terminal_horizon = observation.get("terminal_horizon")
    if (
        isinstance(lower_bound, bool)
        or not isinstance(lower_bound, int)
        or lower_bound < 0
        or isinstance(terminal_horizon, bool)
        or not isinstance(terminal_horizon, int)
        or terminal_horizon != lower_bound
    ):
        return None
    return {
        "state": "RIGHT_CENSORED",
        "event": expected[0],
        "clock": expected[1],
        "lower_bound": lower_bound,
        "terminal_horizon": terminal_horizon,
        "terminal_status": observation["terminal_status"],
    }


def _applicability_contract(
    row: dict, section: str, metric: str,
) -> tuple[str, dict[str, Any] | None]:
    """Return ``(absent|valid|invalid, normalized_contract)`` for one metric.

    The contract is intentionally additive. Existing producers that emit a
    valid measurement need no applicability metadata. A producer claiming
    non-applicability must provide all three machine-auditable fields; a
    malformed claim fails closed instead of turning missing data into N/A.
    """
    root = row.get("metric_applicability", _ABSENT)
    if root is _ABSENT:
        return "absent", None
    if not isinstance(root, dict):
        return "invalid", None
    section_contract = root.get(section, _ABSENT)
    if section_contract is _ABSENT:
        return "absent", None
    if not isinstance(section_contract, dict):
        return "invalid", None
    contract = section_contract.get(metric, _ABSENT)
    if contract is _ABSENT:
        return "absent", None
    if not isinstance(contract, dict):
        return "invalid", None
    applicable = contract.get("applicable")
    predicate = contract.get("predicate")
    reason = contract.get("reason")
    if (
        not isinstance(applicable, bool)
        or not isinstance(predicate, str) or not predicate.strip()
        or not isinstance(reason, str) or not reason.strip()
    ):
        return "invalid", None
    normalized: dict[str, Any] = {
        "applicable": applicable,
        "predicate": predicate,
        "reason": reason,
    }
    observation = contract.get("observation", _ABSENT)
    if observation is not _ABSENT:
        normalized_observation = normalized_right_censor_observation(
            section, metric, observation
        )
        if applicable is not True or normalized_observation is None:
            return "invalid", None
        normalized["observation"] = normalized_observation
    return "valid", normalized


def _metric_state(
    row: dict,
    section: str,
    metric: str,
    value_type: str,
) -> tuple[str, Any, dict[str, Any] | None]:
    """Classify one task value without inferring applicability from raw nulls.

    States are ``measured``, ``not_applicable``, ``unmeasured``, and ``failed``.
    Empty structured values carry no observation and therefore need the same
    explicit non-applicability contract as ``None`` or an absent key.
    """
    section_data = _section(row, section)
    present = metric in section_data
    raw = section_data.get(metric) if present else None
    contract_state, contract = _applicability_contract(row, section, metric)
    if contract_state == "invalid":
        return "failed", None, None

    no_observation = (
        not present
        or raw is None
        or (value_type in {"bool_per_file", "per_tag_rate_dict"}
            and isinstance(raw, dict) and not raw)
    )
    observation = contract.get("observation") if contract is not None else None
    if observation is not None:
        return (
            ("right_censored", float(observation["lower_bound"]), contract)
            if no_observation else ("failed", None, contract)
        )
    if contract is not None and contract["applicable"] is False:
        return (
            ("not_applicable", None, contract)
            if no_observation else ("failed", None, contract)
        )
    if no_observation:
        return "unmeasured", None, contract

    value: Any
    if value_type == "bool":
        value = float(raw) if isinstance(raw, bool) else None
    elif value_type == "bool_per_file":
        value = (
            sum(raw.values()) / len(raw)
            if isinstance(raw, dict)
            and raw
            and all(isinstance(item, bool) for item in raw.values())
            else None
        )
    elif value_type == "per_tag_rate_dict":
        value = _normalized_per_tag_rate_dict(raw)
    else:
        value = _contract_number(raw, value_type)
    return (
        ("measured", value, contract)
        if value is not None else ("failed", None, contract)
    )


def validate_task_performance_record(performance: object) -> list[str]:
    """Return semantic contract failures for one standalone PERF detail record.

    The run aggregator remains the authority for the exact mandatory inventory,
    value types, and applicability semantics.  Behavioral-impact rows live in
    their separate detail artifact and ``cost_per_resolved`` is intentionally a
    run-scope ratio; every other PERF value must be measured or carry a valid
    non-applicability contract.
    """
    if not isinstance(performance, dict):
        return ["record:not_object"]

    issues: list[str] = []
    if performance.get("schema") != "gt_performance_metrics.v1":
        issues.append("record:schema")
    if performance.get("error"):
        issues.append("record:error")
    if performance.get("status") == "UNMEASURED":
        issues.append("record:unmeasured")
    if not isinstance(performance.get("metric_applicability"), dict):
        issues.append("record:metric_applicability")

    row = {
        "performance": performance,
        "metric_applicability": performance.get("metric_applicability"),
    }
    for section, definitions in _MANDATORY_METRICS.items():
        if section == "behavioral_impact":
            continue
        section_data = performance.get(section)
        if not isinstance(section_data, dict):
            issues.append(f"{section}:section")
            continue
        for metric, value_type in definitions:
            if value_type == "run_ratio":
                if (
                    metric not in section_data
                    or section_data.get(metric) is not None
                    or section_data.get(metric + "_scope") != "run_aggregate"
                ):
                    issues.append(f"{section}.{metric}:run_scope_contract")
                continue
            state, _value, _contract = _metric_state(
                row, section, metric, value_type
            )
            if state not in {"measured", "not_applicable", "right_censored"}:
                issues.append(f"{section}.{metric}:{state}")
    return issues


def _deep_metric_record_issues(row: object) -> list[str]:
    """Validate the canonical deep wrapper and every task-scope producer section."""
    if not isinstance(row, dict):
        return ["record:not_object"]
    issues: list[str] = []
    if row.get("schema") != "gt_deep_metrics.v2":
        issues.append("record:deep_schema")
    if row.get("precision_decimals") != 8:
        issues.append("record:deep_precision")
    if not isinstance(row.get("task_id"), str) or not row["task_id"]:
        issues.append("record:task_id")
    performance = row.get("performance")
    issues.extend(
        f"performance:{issue}" for issue in validate_task_performance_record(performance)
    )
    behavioral = row.get("behavioral_impact")
    if not isinstance(behavioral, dict):
        issues.append("behavioral_impact:section")
    elif behavioral.get("collection_error"):
        issues.append("behavioral_impact:collection_error")
    if not isinstance(row.get("metric_applicability"), dict):
        issues.append("record:metric_applicability")
    for metric, value_type in _MANDATORY_METRICS["behavioral_impact"]:
        state, _value, _contract = _metric_state(
            row, "behavioral_impact", metric, value_type
        )
        if state not in {"measured", "not_applicable", "right_censored"}:
            issues.append(f"behavioral_impact.{metric}:{state}")
    return sorted(set(issues))


def _distribution(
    records: list[dict], section: str, metric: str, value_type: str
) -> dict:
    values: list[float] = []
    unmeasured: list[str] = []
    failed: list[str] = []
    not_applicable: list[str] = []
    applicability: dict[str, dict[str, Any]] = {}
    event_observed: list[str] = []
    right_censored: list[str] = []
    for row in records:
        task = _task_name(row)
        state, value, contract = _metric_state(
            row, section, metric, value_type
        )
        if contract is not None:
            applicability[task] = contract
        if state == "measured":
            assert value is not None
            values.append(value)
            event_observed.append(task)
        elif state == "right_censored":
            assert value is not None
            values.append(value)
            right_censored.append(task)
        elif state == "not_applicable":
            not_applicable.append(task)
        elif state == "failed":
            failed.append(task)
        else:
            unmeasured.append(task)

    missing = sorted(set(unmeasured + failed))
    result = {
        "value_type": value_type,
        "aggregation": (
            "event_or_terminal_horizon_distribution"
            if right_censored else "per_task_distribution"
        ),
        "status": (
            "FAILED" if failed
            else "UNMEASURED" if unmeasured
            else "MEASURED" if values
            else "NOT_APPLICABLE" if not_applicable
            else "UNMEASURED"
        ),
        "mean": _d8(statistics.fmean(values)) if values else None,
        "median": _d8(statistics.median(values)) if values else None,
        "measured_tasks": len(values),
        "missing_tasks": missing,
        "unmeasured_tasks": sorted(unmeasured),
        "failed_tasks": sorted(failed),
        "not_applicable_tasks": sorted(not_applicable),
        "event_observed_tasks": sorted(event_observed),
        "right_censored_tasks": sorted(right_censored),
        "applicability": dict(sorted(applicability.items())),
    }
    if right_censored:
        result.update({
            "summary_target": "observed_effort_until_event_or_terminal_horizon",
            "event_time_mean_lower_bound": result["mean"],
            "event_time_median_lower_bound": result["median"],
            "censor_rate": _d8(len(right_censored) / len(values)),
            "semantics": (
                "raw event times remain null for censored tasks; mean/median summarize "
                "exact events or observed terminal horizons and are lower bounds on "
                "the corresponding latent event-time summaries"
            ),
        })
    if value_type == "bool":
        result["semantics"] = "true=1,false=0; mean is true_rate"
    elif value_type == "bool_per_file":
        result["semantics"] = "per-task fraction of file outcomes that are true"
    return result


def _per_tag_distribution(records: list[dict], section: str, metric: str) -> dict:
    valid_by_task: list[
        tuple[str, bool, dict[str, tuple[int, int, float]]]
    ] = []
    outer_unmeasured: list[str] = []
    outer_failed: list[str] = []
    outer_not_applicable: list[str] = []
    applicability: dict[str, dict[str, Any]] = {}
    all_tags: set[str] = set()
    for row in records:
        task = _task_name(row)
        raw = _section(row, section).get(metric)
        task_values: dict[str, tuple[int, int, float]] = {}
        present = metric in _section(row, section)
        contract_state, contract = _applicability_contract(row, section, metric)
        if contract is not None:
            applicability[task] = contract
        no_observation = not present or raw is None or (isinstance(raw, dict) and not raw)
        if contract_state == "invalid":
            outer_failed.append(task)
            valid_by_task.append((task, False, task_values))
            continue
        if contract is not None and contract["applicable"] is False:
            if no_observation:
                outer_not_applicable.append(task)
            else:
                outer_failed.append(task)
            valid_by_task.append((task, False, task_values))
            continue
        if no_observation:
            outer_unmeasured.append(task)
            valid_by_task.append((task, False, task_values))
            continue

        normalized = _normalized_per_tag_rate_dict(raw)
        available = normalized is not None
        if normalized is not None:
            task_values = normalized
            all_tags.update(task_values)
        if not available:
            outer_failed.append(task)
        valid_by_task.append((task, available, task_values))

    tags: dict[str, dict] = {}
    outer_na_set = set(outer_not_applicable)
    for tag in sorted(all_tags):
        rates: list[float] = []
        total_deliveries = 0
        total_pivots = 0
        missing: list[str] = []
        not_applicable: list[str] = []
        for task, available, task_values in valid_by_task:
            if not available:
                if task in outer_na_set:
                    not_applicable.append(task)
                else:
                    missing.append(task)
                continue
            counts = task_values.get(tag)
            if counts is None:
                not_applicable.append(task)
                continue
            total, pivots, rate = counts
            total_deliveries += total
            total_pivots += pivots
            rates.append(rate)
        tags[tag] = {
            "mean": _d8(statistics.fmean(rates)),
            "median": _d8(statistics.median(rates)),
            "pooled_rate": _d8(total_pivots / total_deliveries),
            "total_deliveries": total_deliveries,
            "total_pivots": total_pivots,
            "measured_tasks": len(rates),
            "missing_tasks": sorted(missing),
            "unmeasured_tasks": sorted(
                task for task in missing if task in outer_unmeasured
            ),
            "failed_tasks": sorted(
                task for task in missing if task in outer_failed
            ),
            "not_applicable_tasks": sorted(not_applicable),
        }
    outer_missing = sorted(set(outer_unmeasured + outer_failed))
    return {
        "value_type": "per_tag_rate_dict",
        "aggregation": "per_tag_task_distribution_and_pooled_counts",
        "status": (
            "FAILED" if outer_failed
            else "UNMEASURED" if outer_unmeasured
            else "MEASURED" if tags
            else "NOT_APPLICABLE" if outer_not_applicable
            else "UNMEASURED"
        ),
        "mean": None,
        "median": None,
        "semantics": "means/medians are per-task tag rates; pooled_rate uses summed pivots/deliveries",
        "measured_tasks": len(records) - len(outer_missing) - len(outer_not_applicable),
        "missing_tasks": outer_missing,
        "unmeasured_tasks": sorted(outer_unmeasured),
        "failed_tasks": sorted(outer_failed),
        "not_applicable_tasks": sorted(outer_not_applicable),
        # per_tag_impact is a per-tag pivot RATE (pivots / deliveries). Every delivery's
        # pivot outcome is fully observed at task end — there is no latent time-to-event
        # with a censoring horizon (unlike survival distributions such as
        # steps_to_gold_edit), so NO task can ever be right-censored for this metric. The
        # honest value is therefore always the empty list. It is emitted (not omitted)
        # because the run-scope consumer contract (_run_distribution_feature_record)
        # requires every distribution metric to carry ``right_censored_tasks`` as a list;
        # a missing key reads as ``None`` and fails ``aggregate_partition_valid``.
        "right_censored_tasks": [],
        "applicability": dict(sorted(applicability.items())),
        "tags": tags,
    }


def _mandatory_performance(
    records: list[dict],
    cost_per_resolved: float | None,
    missing_cost: list[str],
    resolved_count: int,
) -> dict[str, dict[str, dict]]:
    result: dict[str, dict[str, dict]] = {}
    for section, metrics in _MANDATORY_METRICS.items():
        result[section] = {}
        for metric, value_type in metrics:
            if value_type == "per_tag_rate_dict":
                aggregate = _per_tag_distribution(records, section, metric)
            elif value_type == "run_ratio":
                cost_collection_complete = bool(records) and not missing_cost
                applicability = (
                    {
                        "applicable": resolved_count > 0,
                        "predicate": (
                            "cost_collection_complete and resolved_count > 0"
                        ),
                        "reason": (
                            "complete run cost and at least one resolved task"
                            if resolved_count > 0
                            else "complete run cost but no resolved tasks"
                        ),
                    }
                    if cost_collection_complete else None
                )
                aggregate = {
                    "value_type": "run_ratio",
                    "aggregation": "ratio_of_run_total_cost_to_resolved_count",
                    "status": (
                        "UNMEASURED" if not records or missing_cost
                        else "MEASURED" if resolved_count
                        else "NOT_APPLICABLE"
                    ),
                    "value": cost_per_resolved,
                    "mean": None,
                    "median": None,
                    "measured_tasks": len(records) - len(missing_cost),
                    "missing_tasks": sorted(missing_cost),
                    "applicability": applicability,
                    "semantics": "run total_cost_usd / resolved task count; not a mean of task ratios",
                }
            else:
                aggregate = _distribution(records, section, metric, value_type)
            result[section][metric] = aggregate
    return result


def aggregate_run_metrics(
    rows: Iterable[dict], baseline_resolved_ids: Iterable[str] = (),
    *, expected_task_ids: Iterable[str] | None = None,
) -> dict:
    """Return outcome and mandatory cost aggregates for a completed run."""
    records = list(rows)
    baseline = set(baseline_resolved_ids)
    expected_list = list(expected_task_ids) if expected_task_ids is not None else None
    expected_counts = Counter(expected_list or [])
    duplicate_expected = sorted(
        task for task, count in expected_counts.items() if count > 1
    )
    expected = set(expected_counts)
    observed_names: list[str] = []
    invalid_task_records: list[str] = []
    invalid_deep_metric_records: dict[str, list[str]] = {}
    for index, row in enumerate(records):
        task = row.get("task_id")
        if isinstance(task, str) and task:
            observed_names.append(task)
        else:
            invalid_task_records.append(f"record:{index}")
        deep_issues = _deep_metric_record_issues(row)
        if deep_issues:
            invalid_deep_metric_records[
                str(task) if isinstance(task, str) and task else f"record:{index}"
            ] = deep_issues
    observed_counts = Counter(observed_names)
    observed = set(observed_counts)
    duplicate_tasks = sorted(
        task for task, count in observed_counts.items() if count > 1
    )
    missing_tasks = sorted(expected - observed) if expected_list is not None else []
    unexpected_tasks = sorted(observed - expected) if expected_list is not None else []
    resolved_rows = [row for row in records if row.get("resolved") is True]
    flips = sorted(
        str(row.get("task_id")) for row in resolved_rows
        if row.get("task_id") not in baseline
    )
    regressions = sorted(
        str(row.get("task_id")) for row in records
        if row.get("resolved") is False and row.get("task_id") in baseline
    )

    missing_cost: list[str] = []
    costs: list[float] = []
    for row in records:
        cost = _task_cost(row)
        if cost is None:
            missing_cost.append(str(row.get("task_id") or "<unknown>"))
        else:
            costs.append(cost)
    cost_complete = bool(records) and not missing_cost
    total_cost = _d8(sum(costs)) if cost_complete else None
    cost_per_resolved = (
        _d8(total_cost / len(resolved_rows))
        if total_cost is not None and resolved_rows else None
    )

    mandatory = _mandatory_performance(
        records, cost_per_resolved, missing_cost, len(resolved_rows)
    )
    metrics_with_missing = sorted(
        f"{section}.{metric}"
        for section, metrics in mandatory.items()
        for metric, aggregate in metrics.items()
        if aggregate["missing_tasks"]
    )
    collection_failures: list[str] = []
    if missing_tasks:
        collection_failures.append("missing_expected_tasks")
    if duplicate_tasks:
        collection_failures.append("duplicate_task_records")
    if unexpected_tasks:
        collection_failures.append("unexpected_tasks")
    if duplicate_expected:
        collection_failures.append("duplicate_expected_tasks")
    if invalid_task_records:
        collection_failures.append("invalid_task_ids")
    if invalid_deep_metric_records:
        collection_failures.append("invalid_deep_metric_records")
    if not records:
        collection_failures.append("no_task_records")
    if metrics_with_missing:
        collection_failures.append("missing_or_malformed_metrics")

    return {
        "schema": "gt_run_metrics.v2",
        "precision_decimals": 8,
        "tasks": len(records),
        "resolved": len(resolved_rows),
        "flips_vs_baseline": flips,
        "flip_count": len(flips),
        "regressions": regressions,
        "regression_count": len(regressions),
        "baseline_resolved": len(baseline),
        "task_population": {
            "expected_count": len(expected_list) if expected_list is not None else None,
            "observed_record_count": len(records),
            "observed_unique_count": len(observed),
            "missing_tasks": missing_tasks,
            "duplicate_tasks": duplicate_tasks,
            "unexpected_tasks": unexpected_tasks,
            "invalid_task_records": invalid_task_records,
        },
        "invalid_deep_metric_records": dict(sorted(invalid_deep_metric_records.items())),
        "mandatory_performance_metric_count": _MANDATORY_METRIC_COUNT,
        "mandatory_performance_collection_complete": (
            bool(records) and not collection_failures
        ),
        "mandatory_performance_collection_failures": collection_failures,
        "mandatory_performance_metrics_with_missing_tasks": metrics_with_missing,
        "mandatory_performance": mandatory,
        "token_efficiency": {
            "total_cost_usd": total_cost,
            "cost_per_resolved": cost_per_resolved,
            "cost_collection_complete": cost_complete,
            "tasks_missing_cost": sorted(missing_cost),
        },
    }


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("metrics_dir")
    parser.add_argument("baseline", nargs="?")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-tasks-file")
    parser.add_argument("--output")
    args = parser.parse_args()

    paths = sorted(glob.glob(os.path.join(
        args.metrics_dir, "**", "gt_deep_metrics_*.json"
    ), recursive=True))
    rows = [_load_json(path) for path in paths]
    baseline = (
        _load_json(args.baseline).get("resolved_ids", []) if args.baseline else []
    )
    expected_tasks = None
    if args.expected_tasks_file:
        expected_tasks = _load_json(args.expected_tasks_file).get("task_ids")
        if not isinstance(expected_tasks, list) or not all(
            isinstance(task, str) and task for task in expected_tasks
        ):
            raise ValueError("expected-tasks file must contain a non-empty task_ids string list")
    result = aggregate_run_metrics(
        rows, baseline, expected_task_ids=expected_tasks
    )
    result["run_id"] = args.run_id

    output_path = args.output or os.path.join(
        args.metrics_dir, f"gt_run_metrics_v2_{args.run_id}.json"
    )
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))
    return 0 if result["mandatory_performance_collection_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
