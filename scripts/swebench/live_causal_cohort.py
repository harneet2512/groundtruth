#!/usr/bin/env python3
"""Live opportunity-level causal analysis for GT shadow assignments.

This module consumes *live* randomized opportunity outcomes.  It does not replay,
simulate, or promote fixture evidence.  Each record is one mutually exclusive
DELIVER/HOLDOUT assignment keyed by an immutable opportunity id.  Repeated
opportunities are clustered by task because earlier GT doses change later history.

The endpoint is declared by the caller (for example ``registered_receipt`` or a
durable-state outcome).  A positive receipt effect is behavioral influence, not
automatically task help or a solve.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from statistics import NormalDist
from typing import Any, Iterable


SCHEMA = "gt.live_causal_cohort.v1"


@dataclass(frozen=True)
class LiveOpportunity:
    task_id: str
    opportunity_id: str
    fact_class: str
    assignment: str
    assignment_probability: float
    endpoint: bool | None
    endpoint_name: str
    live_witness: bool
    assignment_index: int
    outcome_index: int | None
    contaminated: bool = False


def required_sample_size(
    *, minimum_detectable_effect: float, assignment_probability: float,
    alpha: float = 0.05, power: float = 0.80,
) -> int:
    """Conservative Bernoulli risk-difference sample size across both arms."""
    delta = float(minimum_detectable_effect)
    probability = float(assignment_probability)
    if not 0.0 < delta < 1.0:
        raise ValueError("minimum_detectable_effect must be in (0,1)")
    if not 0.0 < probability < 1.0:
        raise ValueError("assignment_probability must be in (0,1)")
    if not 0.0 < alpha < 1.0 or not 0.0 < power < 1.0:
        raise ValueError("alpha and power must be in (0,1)")
    normal = NormalDist()
    z_alpha = normal.inv_cdf(1.0 - alpha / 2.0)
    z_power = normal.inv_cdf(power)
    worst_variance = 0.25 * (
        1.0 / probability + 1.0 / (1.0 - probability)
    )
    return math.ceil(worst_variance * (z_alpha + z_power) ** 2 / delta ** 2)


def evaluate_live_cohort(
    opportunities: Iterable[LiveOpportunity],
    *,
    minimum_detectable_effect: float,
    alpha: float = 0.05,
    power: float = 0.80,
) -> dict[str, Any]:
    """Estimate the randomized opportunity effect with task-clustered uncertainty.

    Uses the Horvitz-Thompson contrast, which remains valid when recorded assignment
    probabilities differ.  The cluster-robust standard error treats a task/attempt as
    the independent unit.  Invalid, non-live, contaminated, censored, duplicate, or
    post-outcome assignments fail closed rather than being silently dropped.
    """
    rows = list(opportunities)
    failures: list[str] = []
    if not rows:
        failures.append("no_opportunities")
    seen: set[str] = set()
    endpoint_names = {row.endpoint_name for row in rows}
    fact_classes = {row.fact_class for row in rows}
    probabilities: list[float] = []
    for row in rows:
        if row.opportunity_id in seen:
            failures.append(f"duplicate_opportunity_id:{row.opportunity_id}")
        seen.add(row.opportunity_id)
        if not _hex(row.opportunity_id, 64):
            failures.append(f"invalid_opportunity_id:{row.opportunity_id}")
        if not row.task_id:
            failures.append("missing_task_id")
        if not row.fact_class:
            failures.append("missing_fact_class")
        if not row.endpoint_name:
            failures.append("missing_endpoint_name")
        if row.assignment not in {"DELIVER", "HOLDOUT"}:
            failures.append(f"invalid_assignment:{row.opportunity_id}")
        if not 0.0 < row.assignment_probability < 1.0:
            failures.append(f"invalid_assignment_probability:{row.opportunity_id}")
        else:
            probabilities.append(row.assignment_probability)
        if row.endpoint is None:
            failures.append(f"endpoint_unmeasured:{row.opportunity_id}")
        if not row.live_witness:
            failures.append(f"not_live:{row.opportunity_id}")
        if row.contaminated:
            failures.append(f"contaminated:{row.opportunity_id}")
        if (
            row.outcome_index is None
            or row.assignment_index < 0
            or row.outcome_index <= row.assignment_index
        ):
            failures.append(f"assignment_not_pre_outcome:{row.opportunity_id}")
    if len(endpoint_names) != 1:
        failures.append("mixed_endpoints")
    if len(fact_classes) != 1:
        failures.append("mixed_fact_classes")
    arms = {row.assignment for row in rows}
    if arms != {"DELIVER", "HOLDOUT"}:
        failures.append("both_arms_required")

    try:
        # Propensities may differ across opportunities.  Power against the easiest
        # (average) allocation would overstate readiness, so use the largest required
        # sample size across every recorded allocation probability.
        required_n = max(
            required_sample_size(
                minimum_detectable_effect=minimum_detectable_effect,
                assignment_probability=probability,
                alpha=alpha,
                power=power,
            )
            for probability in (probabilities or [0.5])
        )
    except ValueError as exc:
        failures.append(f"power_contract_invalid:{exc}")
        required_n = 0

    base = {
        "schema": SCHEMA,
        "fact_class": next(iter(fact_classes), ""),
        "endpoint": next(iter(endpoint_names), ""),
        "n_opportunities": len(rows),
        "n_tasks": len({row.task_id for row in rows if row.task_id}),
        "n_deliver": sum(row.assignment == "DELIVER" for row in rows),
        "n_holdout": sum(row.assignment == "HOLDOUT" for row in rows),
        "minimum_detectable_effect": minimum_detectable_effect,
        "required_n": required_n,
        "alpha": alpha,
        "power": power,
        "failures": sorted(set(failures)),
    }
    if failures:
        return {
            **base,
            "status": "INVALID",
            "identification_strength": "NONE",
            "effect_estimate": None,
            "confidence_interval": None,
            "fair_probe_supported": None,
        }

    contributions: list[tuple[str, float]] = []
    for row in rows:
        y = 1.0 if row.endpoint else 0.0
        p = row.assignment_probability
        contribution = (
            y / p if row.assignment == "DELIVER" else -y / (1.0 - p)
        )
        contributions.append((row.task_id, contribution))
    n = len(contributions)
    estimate = sum(value for _, value in contributions) / n
    cluster_scores: dict[str, float] = {}
    cluster_sizes: dict[str, int] = {}
    for task_id, value in contributions:
        cluster_scores[task_id] = cluster_scores.get(task_id, 0.0) + value
        cluster_sizes[task_id] = cluster_sizes.get(task_id, 0) + 1
    for task_id in cluster_scores:
        cluster_scores[task_id] -= estimate * cluster_sizes[task_id]
    clusters = len(cluster_scores)
    if clusters < 2:
        standard_error = None
        interval = None
    else:
        variance = (
            clusters / (clusters - 1)
            * sum(score * score for score in cluster_scores.values())
            / (n * n)
        )
        standard_error = math.sqrt(max(0.0, variance))
        z = NormalDist().inv_cdf(1.0 - alpha / 2.0)
        interval = [estimate - z * standard_error, estimate + z * standard_error]

    powered = n >= required_n and interval is not None
    if not powered:
        status = "INSUFFICIENT_POWER"
        supported = None
    elif interval[0] > 0.0:
        status = "POSITIVE_EFFECT"
        supported = True
    elif interval[1] < 0.0:
        status = "HARMFUL_EFFECT"
        supported = False
    else:
        status = "NO_DETECTED_EFFECT"
        supported = False
    return {
        **base,
        "status": status,
        "identification_strength": (
            "COHORT_CAUSAL" if powered else "NONE"
        ),
        "effect_estimate": round(estimate, 8),
        "standard_error": (
            round(standard_error, 8) if standard_error is not None else None
        ),
        "confidence_interval": (
            [round(value, 8) for value in interval] if interval else None
        ),
        "fair_probe_supported": supported,
        "interpretation": (
            "effect on the declared live behavioral endpoint; not automatically "
            "HELPED_PROGRESS or HELPED_SOLVE"
        ),
    }


def opportunity_from_dict(raw: dict[str, Any]) -> LiveOpportunity:
    try:
        probability = float(raw.get("assignment_probability"))
    except (TypeError, ValueError):
        probability = float("nan")
    assignment_index = (
        raw.get("assignment_index")
        if type(raw.get("assignment_index")) is int else -1
    )
    return LiveOpportunity(
        task_id=str(raw.get("task_id") or ""),
        opportunity_id=str(raw.get("opportunity_id") or ""),
        fact_class=str(raw.get("fact_class") or ""),
        assignment=str(raw.get("assignment") or ""),
        assignment_probability=probability,
        endpoint=raw.get("endpoint") if type(raw.get("endpoint")) is bool else None,
        endpoint_name=str(raw.get("endpoint_name") or ""),
        live_witness=raw.get("live_witness") is True,
        assignment_index=assignment_index,
        outcome_index=(
            int(raw["outcome_index"])
            if type(raw.get("outcome_index")) is int else None
        ),
        contaminated=(
            raw.get("contaminated") is True
            or bool(raw.get("failures"))
        ),
    )


def _hex(value: str, width: int) -> bool:
    return len(value) == width and all(char in "0123456789abcdef" for char in value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate preregistered live GT randomized opportunities.")
    parser.add_argument("input", help="JSON list of live opportunity records")
    parser.add_argument("--minimum-detectable-effect", type=float, required=True)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--power", type=float, default=0.80)
    args = parser.parse_args(argv)
    with open(args.input, encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, list):
        raise SystemExit("input must be a JSON list")
    result = evaluate_live_cohort(
        [opportunity_from_dict(item) for item in raw if isinstance(item, dict)],
        minimum_detectable_effect=args.minimum_detectable_effect,
        alpha=args.alpha,
        power=args.power,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] != "INVALID" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LiveOpportunity",
    "SCHEMA",
    "evaluate_live_cohort",
    "opportunity_from_dict",
    "required_sample_size",
]
