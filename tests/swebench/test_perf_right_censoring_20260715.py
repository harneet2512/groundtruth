from __future__ import annotations

import json
from pathlib import Path

from scripts.swebench.gt_performance_metrics import (
    build_metric_applicability,
    compute_performance_metrics,
)
from scripts.swebench.gt_run_metrics import aggregate_run_metrics


def _applicable_row(task: str) -> dict:
    return {
        "task_id": task,
        "resolved": False,
        "performance": {
            "localization": {},
            "token_efficiency": {"total_cost_usd": 1.0},
        },
        "metric_applicability": {"localization": {}},
    }


def _set_censor(
    row: dict, metric: str, lower_bound: int, *,
    event: str = "first_gold_view", clock: str = "unique_files",
    terminal_status: str = "Submitted",
) -> None:
    row["performance"]["localization"][metric] = None
    row["metric_applicability"]["localization"][metric] = {
        "applicable": True,
        "predicate": "n_gold_files > 0",
        "reason": "true gold file denominator is available",
        "observation": {
            "state": "RIGHT_CENSORED",
            "event": event,
            "clock": clock,
            "lower_bound": lower_bound,
            "terminal_horizon": lower_bound,
            "terminal_status": terminal_status,
        },
    }


def test_producer_emits_right_censor_contract_only_for_valid_terminal() -> None:
    performance = {
        "n_gold_files": 1,
        "gold_source": "dataset_gold",
        "terminal_status": "Submitted",
        "localization": {
            "files_to_gold_view": None,
            "steps_to_gold_view": None,
            "files_to_gold_edit": None,
            "steps_to_gold_edit": None,
            "_unique_viewed": 3,
            "_unique_edited": 2,
            "_gold_viewed_count": 0,
            "_gold_edited_count": 0,
            "_terminal_step": 40,
        },
        "token_efficiency": {"tokens_per_gold_edit": None},
    }

    contracts = build_metric_applicability(performance)

    assert contracts["localization"]["files_to_gold_view"]["observation"] == {
        "state": "RIGHT_CENSORED",
        "event": "first_gold_view",
        "clock": "unique_files",
        "lower_bound": 3,
        "terminal_horizon": 3,
        "terminal_status": "Submitted",
    }
    assert contracts["localization"]["steps_to_gold_edit"]["observation"] == {
        "state": "RIGHT_CENSORED",
        "event": "first_gold_edit",
        "clock": "assistant_steps",
        "lower_bound": 40,
        "terminal_horizon": 40,
        "terminal_status": "Submitted",
    }
    assert contracts["token_efficiency"]["tokens_per_gold_edit"] == {
        "applicable": False,
        "predicate": "true_gold_available and gold_files_edited > 0",
        "reason": "no true-gold file was edited",
    }

    performance["token_efficiency"]["_n_gold_edited"] = 1
    positive_denominator = build_metric_applicability(performance)
    assert positive_denominator["token_efficiency"]["tokens_per_gold_edit"] == {
        "applicable": True,
        "predicate": "true_gold_available and gold_files_edited > 0",
        "reason": "one or more true-gold files were edited",
    }

    performance["terminal_status"] = "RuntimeError"
    invalid = build_metric_applicability(performance)
    for metric in (
        "files_to_gold_view", "steps_to_gold_view",
        "files_to_gold_edit", "steps_to_gold_edit",
    ):
        assert "observation" not in invalid["localization"][metric]


def test_real_trajectory_plumbs_terminal_horizon_without_faking_event(
    tmp_path: Path,
) -> None:
    trajectory = {
        "messages": [
            {
                "role": "assistant", "content": "inspect",
                "tool_calls": [{"function": {"arguments": json.dumps({
                    "command": "view", "path": "src/other.py",
                })}}],
            },
            {"role": "tool", "content": "source"},
        ],
        "info": {
            "exit_status": "Submitted", "submission": "",
            "model_stats": {"api_calls": 1},
        },
    }
    trajectory_path = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")

    result = compute_performance_metrics(
        str(trajectory_path), str(tmp_path), gold_files=["src/gold.py"],
        consumption_ledger={"schema": "gt.consumption_ledger.v2", "entries": []},
    )

    assert result["terminal_status"] == "Submitted"
    assert result["localization"]["files_to_gold_view"] is None
    assert result["localization"]["steps_to_gold_view"] is None
    assert result["metric_applicability"]["localization"][
        "files_to_gold_view"
    ]["observation"]["terminal_horizon"] == 1
    assert result["metric_applicability"]["localization"][
        "steps_to_gold_view"
    ]["observation"]["terminal_horizon"] == 1


def test_exact_event_remains_numeric_and_has_no_censor_observation() -> None:
    performance = {
        "n_gold_files": 1,
        "gold_source": "dataset_gold",
        "terminal_status": "Submitted",
        "localization": {
            "files_to_gold_view": 0,
            "steps_to_gold_view": 0,
            "files_to_gold_edit": 1,
            "steps_to_gold_edit": 2,
            "_unique_viewed": 1,
            "_unique_edited": 2,
            "_gold_viewed_count": 1,
            "_gold_edited_count": 1,
            "_terminal_step": 3,
        },
        "token_efficiency": {"tokens_per_gold_edit": 100.0, "_n_gold_edited": 1},
    }

    contracts = build_metric_applicability(performance)

    assert performance["localization"]["files_to_gold_view"] == 0
    assert performance["localization"]["steps_to_gold_view"] == 0
    for metric in (
        "files_to_gold_view", "steps_to_gold_view",
        "files_to_gold_edit", "steps_to_gold_edit",
    ):
        assert "observation" not in contracts["localization"][metric]


def test_tokens_per_gold_edit_uses_real_denominator_semantics() -> None:
    zero = _applicable_row("zero")
    zero["performance"]["token_efficiency"]["tokens_per_gold_edit"] = None
    zero["metric_applicability"]["token_efficiency"] = {
        "tokens_per_gold_edit": {
            "applicable": False,
            "predicate": "true_gold_available and gold_files_edited > 0",
            "reason": "no true-gold file was edited",
        }
    }
    positive = _applicable_row("positive")
    positive["performance"]["token_efficiency"]["tokens_per_gold_edit"] = 120.0
    positive["metric_applicability"]["token_efficiency"] = {
        "tokens_per_gold_edit": {
            "applicable": True,
            "predicate": "true_gold_available and gold_files_edited > 0",
            "reason": "one or more true-gold files were edited",
        }
    }
    missing_tokens = _applicable_row("missing-tokens")
    missing_tokens["performance"]["token_efficiency"]["tokens_per_gold_edit"] = None
    missing_tokens["metric_applicability"]["token_efficiency"] = {
        "tokens_per_gold_edit": {
            "applicable": True,
            "predicate": "true_gold_available and gold_files_edited > 0",
            "reason": "one or more true-gold files were edited",
        }
    }

    metric = aggregate_run_metrics([zero, positive, missing_tokens])[
        "mandatory_performance"
    ]["token_efficiency"]["tokens_per_gold_edit"]

    assert metric["status"] == "UNMEASURED"
    assert metric["not_applicable_tasks"] == ["zero"]
    assert metric["measured_tasks"] == 1
    assert metric["unmeasured_tasks"] == ["missing-tokens"]


def test_mixed_exact_and_censored_distribution_is_explicit_lower_bound() -> None:
    exact = _applicable_row("b-exact")
    exact["performance"]["localization"]["files_to_gold_view"] = 2
    censored = _applicable_row("a-censored")
    _set_censor(censored, "files_to_gold_view", 6)

    metric = aggregate_run_metrics([exact, censored])["mandatory_performance"][
        "localization"
    ]["files_to_gold_view"]

    assert metric["status"] == "MEASURED"
    assert metric["aggregation"] == "event_or_terminal_horizon_distribution"
    assert metric["summary_target"] == "observed_effort_until_event_or_terminal_horizon"
    assert metric["mean"] == 4.0
    assert metric["median"] == 4.0
    assert metric["event_time_mean_lower_bound"] == 4.0
    assert metric["event_time_median_lower_bound"] == 4.0
    assert metric["event_observed_tasks"] == ["b-exact"]
    assert metric["right_censored_tasks"] == ["a-censored"]
    assert metric["censor_rate"] == 0.5
    assert metric["missing_tasks"] == []


def test_all_censored_distribution_has_no_fabricated_task_event_time() -> None:
    a = _applicable_row("a")
    b = _applicable_row("b")
    _set_censor(a, "steps_to_gold_view", 4, clock="assistant_steps")
    _set_censor(b, "steps_to_gold_view", 8, clock="assistant_steps")

    metric = aggregate_run_metrics([a, b])["mandatory_performance"][
        "localization"
    ]["steps_to_gold_view"]

    assert a["performance"]["localization"]["steps_to_gold_view"] is None
    assert b["performance"]["localization"]["steps_to_gold_view"] is None
    assert metric["mean"] == 6.0
    assert metric["median"] == 6.0
    assert metric["event_observed_tasks"] == []
    assert metric["right_censored_tasks"] == ["a", "b"]
    assert metric["censor_rate"] == 1.0


def test_censor_contract_rejects_wrong_shape_terminal_and_raw_contradiction() -> None:
    wrong_event = _applicable_row("wrong-event")
    _set_censor(wrong_event, "files_to_gold_view", 3, event="first_gold_edit")
    invalid_terminal = _applicable_row("invalid-terminal")
    _set_censor(invalid_terminal, "files_to_gold_view", 3, terminal_status="Crashed")
    contradiction = _applicable_row("contradiction")
    _set_censor(contradiction, "files_to_gold_view", 3)
    contradiction["performance"]["localization"]["files_to_gold_view"] = 2

    metric = aggregate_run_metrics([wrong_event, invalid_terminal, contradiction])[
        "mandatory_performance"
    ]["localization"]["files_to_gold_view"]

    assert metric["status"] == "FAILED"
    assert metric["failed_tasks"] == [
        "contradiction", "invalid-terminal", "wrong-event",
    ]


def test_censored_aggregation_is_byte_identical_under_task_reordering() -> None:
    a = _applicable_row("a")
    b = _applicable_row("b")
    _set_censor(a, "files_to_gold_edit", 5, event="first_gold_edit")
    _set_censor(b, "files_to_gold_edit", 7, event="first_gold_edit")

    forward = aggregate_run_metrics([a, b])["mandatory_performance"][
        "localization"
    ]["files_to_gold_edit"]
    reverse = aggregate_run_metrics([b, a])["mandatory_performance"][
        "localization"
    ]["files_to_gold_edit"]

    assert json.dumps(forward, sort_keys=True, separators=(",", ":")) == json.dumps(
        reverse, sort_keys=True, separators=(",", ":")
    )
