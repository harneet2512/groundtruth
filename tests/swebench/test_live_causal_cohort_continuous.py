"""RED-first proof for CONTINUOUS proximal-outcome endpoints in the live cohort.

The pre-existing cohort estimator supports only a BOOLEAN endpoint (registered_receipt).
That proves behavioral INFLUENCE but cannot estimate EFFICIENCY (steps / searches / tokens
saved). These tests assert the SAME Horvitz-Thompson / task-clustered machinery estimates a
real-valued Y with a declared endpoint sign, WITHOUT disturbing the boolean path.

Artifact-first: every continuous assertion here FAILS on pristine HEAD (the schema has no
continuous field and the estimator hard-codes ``y = 1.0 if endpoint else 0.0``). The
boolean byte-identity test PASSES on pristine and MUST keep passing after the change.
"""
from __future__ import annotations

from scripts.swebench.live_causal_cohort import (
    LiveOpportunity,
    evaluate_live_cohort,
    opportunity_from_dict,
    project_continuous_endpoint,
)


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _cont_row(
    i: int,
    *,
    arm: str,
    value: float,
    sign: str = "lower_is_better",
    task: str | None = None,
    prob: float = 0.5,
    live: bool = True,
    contaminated: bool = False,
) -> LiveOpportunity:
    """One eligible opportunity carrying a CONTINUOUS consequence field."""
    return LiveOpportunity(
        task_id=task or f"task-{i}",
        opportunity_id=f"{i:064x}",
        fact_class="localization",
        assignment=arm,
        assignment_probability=prob,
        endpoint=None,
        endpoint_name="steps_to_next_decision",
        live_witness=live,
        assignment_index=2,
        outcome_index=4,
        contaminated=contaminated,
        endpoint_kind="continuous",
        endpoint_value=value,
        endpoint_sign=sign,
    )


def _bool_row(
    i: int, *, arm: str, endpoint: bool, task: str | None = None,
) -> LiveOpportunity:
    """A boolean opportunity identical in shape to the pristine test builder."""
    return LiveOpportunity(
        task_id=task or f"task-{i}",
        opportunity_id=f"{i:064x}",
        fact_class="caller_contract",
        assignment=arm,
        assignment_probability=0.5,
        endpoint=endpoint,
        endpoint_name="registered_receipt",
        live_witness=True,
        assignment_index=2,
        outcome_index=4,
        contaminated=False,
    )


def _efficiency_cohort(sign: str) -> list[LiveOpportunity]:
    """DELIVER lowers ``steps_to_next_decision`` (~5) vs HOLDOUT (~11): a genuine efficiency
    gain under ``lower_is_better``.  One opportunity per task -> non-degenerate task clustering."""
    rows: list[LiveOpportunity] = []
    for i in range(240):
        if i % 2 == 0:
            rows.append(_cont_row(i, arm="DELIVER", value=4.0 + (i % 3), sign=sign))
        else:
            rows.append(_cont_row(i, arm="HOLDOUT", value=10.0 + (i % 3), sign=sign))
    return rows


# --------------------------------------------------------------------------- #
# (a) the record carries the continuous field
# --------------------------------------------------------------------------- #
def test_opportunity_record_carries_continuous_field() -> None:
    row = _cont_row(1, arm="DELIVER", value=3.0)
    assert row.endpoint_kind == "continuous"
    assert row.endpoint_value == 3.0
    assert row.endpoint_sign == "lower_is_better"
    # the boolean slot is unused (and None) for a continuous endpoint.
    assert row.endpoint is None


# --------------------------------------------------------------------------- #
# (b) the estimator returns a real-valued tau_hat with a task-clustered CI
# --------------------------------------------------------------------------- #
def test_continuous_estimator_returns_real_tau_with_clustered_ci() -> None:
    result = evaluate_live_cohort(
        _efficiency_cohort("lower_is_better"), minimum_detectable_effect=2.0,
    )
    assert result["endpoint_kind"] == "continuous"
    tau = result["effect_estimate"]
    assert isinstance(tau, float)
    # a real-valued efficiency effect: DELIVER reduces the step metric (not a 0/1 receipt).
    assert tau < -1.0
    ci = result["confidence_interval"]
    assert isinstance(ci, list) and len(ci) == 2
    assert all(isinstance(bound, float) for bound in ci)
    assert ci[0] < tau < ci[1] or ci[0] <= tau <= ci[1]
    assert isinstance(result["standard_error"], float)
    assert result["standard_error"] > 0.0  # non-degenerate task-clustered SE
    assert result["n_tasks"] == 240


# --------------------------------------------------------------------------- #
# sign is load-bearing: mutating it flips the beneficial verdict on identical data
# --------------------------------------------------------------------------- #
def test_declared_sign_flips_beneficial_verdict() -> None:
    lower = evaluate_live_cohort(
        _efficiency_cohort("lower_is_better"), minimum_detectable_effect=2.0,
    )
    higher = evaluate_live_cohort(
        _efficiency_cohort("higher_is_better"), minimum_detectable_effect=2.0,
    )
    # identical Y, identical estimate — only the declared sign differs.
    assert lower["effect_estimate"] == higher["effect_estimate"]
    # under lower_is_better a step reduction is GOOD; under higher_is_better it is HARMFUL.
    assert lower["status"] == "BENEFICIAL_EFFECT"
    assert lower["beneficial"] is True
    assert lower["fair_probe_supported"] is True
    assert higher["status"] == "HARMFUL_EFFECT"
    assert higher["beneficial"] is False
    assert higher["fair_probe_supported"] is False


def test_invalid_endpoint_sign_is_rejected() -> None:
    rows = [
        _cont_row(i, arm=("DELIVER" if i % 2 == 0 else "HOLDOUT"),
                  value=5.0, sign="sideways")
        for i in range(8)
    ]
    result = evaluate_live_cohort(rows, minimum_detectable_effect=2.0)
    assert result["status"] == "INVALID"
    assert "invalid_endpoint_sign" in result["failures"]


def test_mixed_endpoint_kinds_are_rejected() -> None:
    rows = [
        _cont_row(0, arm="DELIVER", value=4.0),
        _bool_row(1, arm="HOLDOUT", endpoint=False),
    ]
    result = evaluate_live_cohort(rows, minimum_detectable_effect=2.0)
    assert result["status"] == "INVALID"
    assert "mixed_endpoint_kind" in result["failures"]


def test_continuous_missing_value_is_unmeasured() -> None:
    rows = [
        _cont_row(0, arm="DELIVER", value=4.0),
        LiveOpportunity(
            task_id="task-1", opportunity_id=f"{1:064x}", fact_class="localization",
            assignment="HOLDOUT", assignment_probability=0.5, endpoint=None,
            endpoint_name="steps_to_next_decision", live_witness=True,
            assignment_index=2, outcome_index=4, contaminated=False,
            endpoint_kind="continuous", endpoint_value=None,
            endpoint_sign="lower_is_better",
        ),
    ]
    result = evaluate_live_cohort(rows, minimum_detectable_effect=2.0)
    assert result["status"] == "INVALID"
    assert any(r.startswith("endpoint_unmeasured:") for r in result["failures"])


# --------------------------------------------------------------------------- #
# end-to-end: a recorded continuous observation flows through the parser + projection
# --------------------------------------------------------------------------- #
def test_continuous_observation_parses_end_to_end() -> None:
    obs = {
        "task_id": "task-x",
        "opportunity_id": "a" * 64,
        "fact_class": "localization",
        "assignment": "DELIVER",
        "assignment_probability": 0.5,
        "endpoint_kind": "continuous",
        "endpoint_name": "steps_to_next_decision",
        "endpoint_value": 6.0,
        "endpoint_sign": "lower_is_better",
        "live_witness": True,
        "assignment_index": 2,
        "outcome_index": 4,
    }
    row = opportunity_from_dict(obs)
    assert row.endpoint_kind == "continuous"
    assert row.endpoint_value == 6.0
    assert row.endpoint_sign == "lower_is_better"


def test_project_continuous_endpoint_from_consequences() -> None:
    obs = {
        "task_id": "task-y",
        "opportunity_id": "b" * 64,
        "fact_class": "localization",
        "assignment": "HOLDOUT",
        "assignment_probability": 0.5,
        # boolean receipt is still present; the projection selects a continuous metric.
        "endpoint": False,
        "endpoint_name": "registered_receipt",
        "live_witness": True,
        "assignment_index": 2,
        "outcome_index": 5,
        "consequences": {
            "native_search_views": 7.0,
            "steps_to_next_decision": 9.0,
            "edit_cycles": 2.0,
        },
    }
    row = project_continuous_endpoint(
        obs, metric="native_search_views", sign="lower_is_better"
    )
    assert row.endpoint_kind == "continuous"
    assert row.endpoint_name == "native_search_views"
    assert row.endpoint_value == 7.0
    assert row.endpoint_sign == "lower_is_better"
    assert row.assignment == "HOLDOUT"


# --------------------------------------------------------------------------- #
# (c) the boolean endpoint path is byte-identical to pristine HEAD
# --------------------------------------------------------------------------- #
# Captured from pristine HEAD (edf7ee6b5) evaluate_live_cohort on the 220-row powered
# boolean cohort below. Any drift in the boolean output breaks this snapshot.
_PRISTINE_BOOLEAN_POWERED = {
    "alpha": 0.05,
    "confidence_interval": [0.86755791, 1.13244209],
    "effect_estimate": 1.0,
    "endpoint": "registered_receipt",
    "fact_class": "caller_contract",
    "failures": [],
    "fair_probe_supported": True,
    "identification_strength": "COHORT_CAUSAL",
    "interpretation": (
        "effect on the declared live behavioral endpoint; not automatically "
        "HELPED_PROGRESS or HELPED_SOLVE"
    ),
    "minimum_detectable_effect": 0.2,
    "n_deliver": 110,
    "n_holdout": 110,
    "n_opportunities": 220,
    "n_tasks": 220,
    "power": 0.8,
    "required_n": 197,
    "schema": "gt.live_causal_cohort.v1",
    "standard_error": 0.06757374,
    "status": "POSITIVE_EFFECT",
}


def test_boolean_path_is_byte_identical_to_pristine() -> None:
    rows = [
        _bool_row(i, arm=("DELIVER" if i % 2 == 0 else "HOLDOUT"),
                  endpoint=(i % 2 == 0), task=f"task-{i}")
        for i in range(220)
    ]
    result = evaluate_live_cohort(rows, minimum_detectable_effect=0.2)
    # exact structural + value identity with the pristine output.
    assert result == _PRISTINE_BOOLEAN_POWERED
    # the continuous extension leaks NO new keys onto the boolean result.
    for continuous_only in ("endpoint_kind", "endpoint_sign", "beneficial"):
        assert continuous_only not in result
