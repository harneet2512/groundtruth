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
from statistics import NormalDist, pstdev
from typing import Any, Iterable


SCHEMA = "gt.live_causal_cohort.v1"

# The two endpoint families. BOOLEAN is the pristine behavioral endpoint (did the agent take
# up the fact); CONTINUOUS is a real-valued proximal efficiency endpoint (steps / searches /
# tokens saved) measured from the opportunity boundary to the next decision commit.
_ENDPOINT_KINDS = frozenset({"boolean", "continuous"})
# The declared endpoint sign. HIGHER_IS_BETTER matches the boolean receipt (uptake is good);
# LOWER_IS_BETTER marks a cost metric where a DELIVER-induced REDUCTION is the beneficial
# direction (e.g. steps-to-next-decision, native searches, wrong-branch views).
_ENDPOINT_SIGNS = frozenset({"higher_is_better", "lower_is_better"})


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
    # Continuous-endpoint extension (all defaulted so a boolean opportunity is byte-identical).
    # ``endpoint`` carries the boolean value; ``endpoint_value`` carries the real-valued one.
    endpoint_kind: str = "boolean"
    endpoint_value: float | None = None
    endpoint_sign: str = "higher_is_better"


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


def required_sample_size_continuous(
    *, minimum_detectable_effect: float, assignment_probability: float,
    outcome_std: float, alpha: float = 0.05, power: float = 0.80,
) -> int:
    """Two-sample difference-of-means sample size for a REAL-VALUED endpoint.

    Unlike the Bernoulli risk-difference form, the minimum detectable effect is on the raw
    metric scale (steps, searches, tokens) so it is NOT clamped to ``(0, 1)``; the per-arm
    variance is the declared / plug-in outcome standard deviation squared rather than the
    Bernoulli worst-case ``0.25``.  Unequal allocation is handled the same way (``1/p + 1/(1-p)``).
    """
    delta = float(minimum_detectable_effect)
    probability = float(assignment_probability)
    sd = float(outcome_std)
    if not delta > 0.0:
        raise ValueError("minimum_detectable_effect must be > 0 for a continuous endpoint")
    if not 0.0 < probability < 1.0:
        raise ValueError("assignment_probability must be in (0,1)")
    if not 0.0 < alpha < 1.0 or not 0.0 < power < 1.0:
        raise ValueError("alpha and power must be in (0,1)")
    if sd != sd or sd < 0.0 or sd in (float("inf"), float("-inf")):
        raise ValueError("outcome_std must be a finite non-negative real")
    normal = NormalDist()
    z_alpha = normal.inv_cdf(1.0 - alpha / 2.0)
    z_power = normal.inv_cdf(power)
    variance = sd * sd * (1.0 / probability + 1.0 / (1.0 - probability))
    return math.ceil(variance * (z_alpha + z_power) ** 2 / delta ** 2)


def _finite(value: Any) -> bool:
    """True iff ``value`` is a finite real number (an int/float that is not a bool, NaN, or inf)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value
        and value not in (float("inf"), float("-inf"))
    )


def evaluate_live_cohort(
    opportunities: Iterable[LiveOpportunity],
    *,
    minimum_detectable_effect: float,
    alpha: float = 0.05,
    power: float = 0.80,
    outcome_std: float | None = None,
) -> dict[str, Any]:
    """Estimate the randomized opportunity effect with task-clustered uncertainty.

    Uses the Horvitz-Thompson contrast ``tau_hat = mean(A*Y/p - (1-A)*Y/(1-p))``, which remains
    valid when recorded assignment probabilities differ and for a REAL-VALUED ``Y`` as well as a
    ``0/1`` receipt.  The cluster-robust standard error treats a task/attempt as the independent
    unit.  Invalid, non-live, contaminated, censored, duplicate, or post-outcome assignments fail
    closed rather than being silently dropped.

    The endpoint family is declared per row.  A BOOLEAN cohort (the default) is byte-identical to
    the pristine behavior.  A CONTINUOUS cohort estimates an efficiency effect on the declared
    real-valued endpoint and reports it interpretably per the declared ``endpoint_sign``
    (``lower_is_better`` => a DELIVER-induced reduction is BENEFICIAL).  ``outcome_std`` is an
    OPTIONAL preregistered outcome standard deviation for the continuous power gate; when absent
    the pooled empirical SD is used as a (conservative) plug-in.
    """
    rows = list(opportunities)
    failures: list[str] = []
    if not rows:
        failures.append("no_opportunities")
    seen: set[str] = set()
    endpoint_names = {row.endpoint_name for row in rows}
    fact_classes = {row.fact_class for row in rows}
    endpoint_kinds = {row.endpoint_kind for row in rows}
    endpoint_signs = {row.endpoint_sign for row in rows}
    is_continuous = endpoint_kinds == {"continuous"}
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
        # unmeasured check binds the field the row's kind actually uses: the boolean slot for a
        # boolean row (byte-identical), the continuous magnitude for a continuous row.
        if row.endpoint_kind == "continuous":
            if not _finite(row.endpoint_value):
                failures.append(f"endpoint_unmeasured:{row.opportunity_id}")
        elif row.endpoint is None:
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
    if any(kind not in _ENDPOINT_KINDS for kind in endpoint_kinds):
        failures.append("invalid_endpoint_kind")
    if any(sign not in _ENDPOINT_SIGNS for sign in endpoint_signs):
        failures.append("invalid_endpoint_sign")
    if len(endpoint_kinds) > 1:
        failures.append("mixed_endpoint_kind")
    if len(endpoint_signs) > 1:
        failures.append("mixed_endpoint_sign")
    arms = {row.assignment for row in rows}
    if arms != {"DELIVER", "HOLDOUT"}:
        failures.append("both_arms_required")

    try:
        # Propensities may differ across opportunities.  Power against the easiest
        # (average) allocation would overstate readiness, so use the largest required
        # sample size across every recorded allocation probability.
        if is_continuous:
            # Preregistered SD when supplied; otherwise the pooled empirical SD as a
            # conservative plug-in (it folds in the between-arm shift, so it never
            # UNDER-states the required n).
            finite_values = [
                float(row.endpoint_value)
                for row in rows
                if _finite(row.endpoint_value)
            ]
            if outcome_std is not None:
                pooled_std = float(outcome_std)
            elif len(finite_values) >= 2:
                pooled_std = pstdev(finite_values)
            else:
                pooled_std = 0.0
            required_n = max(
                required_sample_size_continuous(
                    minimum_detectable_effect=minimum_detectable_effect,
                    assignment_probability=probability,
                    outcome_std=pooled_std,
                    alpha=alpha,
                    power=power,
                )
                for probability in (probabilities or [0.5])
            )
        else:
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
        # Y is the real-valued magnitude for a continuous endpoint, the 0/1 receipt otherwise.
        y = float(row.endpoint_value) if is_continuous else (1.0 if row.endpoint else 0.0)
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
    if is_continuous:
        return _continuous_result(
            base=base, powered=powered, estimate=estimate,
            standard_error=standard_error, interval=interval,
            endpoint_sign=next(iter(endpoint_signs)),
            endpoint_name=base["endpoint"],
        )
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


def _continuous_result(
    *,
    base: dict[str, Any],
    powered: bool,
    estimate: float,
    standard_error: float | None,
    interval: list[float] | None,
    endpoint_sign: str,
    endpoint_name: str,
) -> dict[str, Any]:
    """Assemble the continuous-endpoint verdict, interpreting the sign so a 'lower is better'
    metric reports a DELIVER-induced reduction as BENEFICIAL rather than as a harmful decrease.

    ``tau_hat`` is the Horvitz-Thompson estimate of ``E[Y(deliver) - Y(holdout)]`` on the raw
    metric scale; the significance direction is taken from the task-clustered CI and combined
    with the declared sign to decide ``beneficial``.  Distinct status vocabulary
    (``BENEFICIAL_EFFECT`` / ``HARMFUL_EFFECT``) keeps a cost-metric result unambiguous.
    """
    beneficial_direction = -1.0 if endpoint_sign == "lower_is_better" else 1.0
    if not powered:
        status = "INSUFFICIENT_POWER"
        supported: bool | None = None
        beneficial: bool | None = None
    else:
        lo, hi = interval  # type: ignore[misc]  # powered => interval is not None
        if lo > 0.0:
            direction = 1.0          # metric increased under DELIVER
        elif hi < 0.0:
            direction = -1.0         # metric decreased under DELIVER
        else:
            direction = 0.0          # CI straddles zero
        if direction == 0.0:
            status = "NO_DETECTED_EFFECT"
            supported = False
            beneficial = False
        else:
            good = direction * beneficial_direction > 0.0
            beneficial = good
            supported = good
            status = "BENEFICIAL_EFFECT" if good else "HARMFUL_EFFECT"
    return {
        **base,
        "status": status,
        "identification_strength": "COHORT_CAUSAL" if powered else "NONE",
        "effect_estimate": round(estimate, 8),
        "standard_error": (
            round(standard_error, 8) if standard_error is not None else None
        ),
        "confidence_interval": (
            [round(value, 8) for value in interval] if interval else None
        ),
        "fair_probe_supported": supported,
        "endpoint_kind": "continuous",
        "endpoint_sign": endpoint_sign,
        "beneficial": beneficial,
        "interpretation": (
            f"effect on the declared continuous efficiency endpoint '{endpoint_name}' "
            f"({endpoint_sign}); a beneficial effect moves the metric in the declared "
            "direction; not automatically HELPED_PROGRESS or HELPED_SOLVE"
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
    # Continuous-endpoint fields. Absent keys default to the boolean family (so a legacy
    # observation dict parses byte-identically). An invalid kind/sign is PRESERVED verbatim
    # (like a malformed probability) so the estimator flags it rather than the parser hiding it.
    endpoint_kind = str(raw.get("endpoint_kind") or "boolean")
    endpoint_sign = str(raw.get("endpoint_sign") or "higher_is_better")
    raw_value = raw.get("endpoint_value")
    endpoint_value = (
        float(raw_value)
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool)
        else None
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
        endpoint_kind=endpoint_kind,
        endpoint_value=endpoint_value,
        endpoint_sign=endpoint_sign,
    )


def project_continuous_endpoint(
    observation: dict[str, Any], *, metric: str, sign: str = "higher_is_better",
) -> LiveOpportunity:
    """Re-key a recorded live-opportunity observation onto a CONTINUOUS endpoint named ``metric``.

    A recorded observation exports its boolean receipt PLUS a ``consequences`` map of measured
    continuous fields (steps / searches / edits, measured from the opportunity boundary to the
    next decision commit).  This projection selects one such field as the endpoint, declares its
    sign, and returns a ``LiveOpportunity`` ready for :func:`evaluate_live_cohort` — the explicit
    bridge that makes continuous efficiency endpoints estimable end-to-end from a real run's
    exported observations.  A missing metric yields ``endpoint_value=None`` (the estimator then
    fails closed with ``endpoint_unmeasured``)."""
    consequences = observation.get("consequences")
    value = consequences.get(metric) if isinstance(consequences, dict) else None
    projected = dict(observation)
    projected["endpoint_kind"] = "continuous"
    projected["endpoint_name"] = metric
    projected["endpoint_value"] = value
    projected["endpoint_sign"] = sign
    projected["endpoint"] = None  # the boolean receipt slot is unused for a continuous endpoint
    return opportunity_from_dict(projected)


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
    "project_continuous_endpoint",
    "required_sample_size",
    "required_sample_size_continuous",
]
