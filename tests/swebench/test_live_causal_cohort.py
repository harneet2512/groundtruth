from __future__ import annotations

from scripts.swebench.live_causal_cohort import (
    LiveOpportunity,
    evaluate_live_cohort,
    opportunity_from_dict,
    required_sample_size,
)


def _row(
    i: int, *, arm: str, endpoint: bool, task: str | None = None,
    live: bool = True, contaminated: bool = False,
) -> LiveOpportunity:
    return LiveOpportunity(
        task_id=task or f"task-{i // 2}",
        opportunity_id=f"{i:064x}",
        fact_class="caller_contract",
        assignment=arm,
        assignment_probability=0.5,
        endpoint=endpoint,
        endpoint_name="registered_receipt",
        live_witness=live,
        assignment_index=2,
        outcome_index=4,
        contaminated=contaminated,
    )


def test_power_is_declared_not_conventional() -> None:
    assert required_sample_size(
        minimum_detectable_effect=0.2, assignment_probability=0.5,
    ) >= 190


def test_non_live_or_contaminated_rows_invalidate_cohort() -> None:
    result = evaluate_live_cohort(
        [
            _row(1, arm="DELIVER", endpoint=True, live=False),
            _row(2, arm="HOLDOUT", endpoint=False, contaminated=True),
        ],
        minimum_detectable_effect=0.5,
    )
    assert result["status"] == "INVALID"
    assert any(reason.startswith("not_live:") for reason in result["failures"])
    assert any(reason.startswith("contaminated:") for reason in result["failures"])


def test_both_mutually_exclusive_arms_are_required() -> None:
    result = evaluate_live_cohort(
        [_row(i, arm="DELIVER", endpoint=True) for i in range(8)],
        minimum_detectable_effect=0.5,
    )
    assert result["status"] == "INVALID"
    assert "both_arms_required" in result["failures"]


def test_small_valid_live_cohort_is_not_promoted() -> None:
    rows = [
        _row(i, arm=("DELIVER" if i % 2 == 0 else "HOLDOUT"),
             endpoint=(i % 2 == 0))
        for i in range(12)
    ]
    result = evaluate_live_cohort(rows, minimum_detectable_effect=0.2)
    assert result["status"] == "INSUFFICIENT_POWER"
    assert result["identification_strength"] == "NONE"
    assert result["fair_probe_supported"] is None


def test_powered_positive_behavior_effect_is_bounded_not_solve_credit() -> None:
    rows = [
        _row(
            i,
            arm=("DELIVER" if i % 2 == 0 else "HOLDOUT"),
            endpoint=(i % 2 == 0),
            task=f"task-{i}",
        )
        for i in range(220)
    ]
    result = evaluate_live_cohort(rows, minimum_detectable_effect=0.2)
    assert result["status"] == "POSITIVE_EFFECT"
    assert result["fair_probe_supported"] is True
    assert "not automatically" in result["interpretation"]


def test_malformed_export_is_fail_closed_not_a_parser_crash() -> None:
    row = opportunity_from_dict({
        "task_id": "task",
        "opportunity_id": "a" * 64,
        "fact_class": "caller_contract",
        "assignment": "DELIVER",
        "assignment_probability": None,
        "endpoint": True,
        "endpoint_name": "registered_receipt",
        "live_witness": True,
        "assignment_index": "bad",
        "outcome_index": 4,
        "failures": ["candidate_identity_invalid"],
    })
    result = evaluate_live_cohort([row], minimum_detectable_effect=0.5)
    assert result["status"] == "INVALID"
    assert any(reason.startswith("contaminated:") for reason in result["failures"])
