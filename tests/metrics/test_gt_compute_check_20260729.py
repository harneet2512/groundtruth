"""Task #30 R5 (2026-07-29) — the §5 compute doctrine must be ENFORCEABLE, not prose.

`.agents/skills/gt-compute/` states the doctrine; `compute_paired_metrics.py` implements
it; nothing CHECKED a produced artifact against it. These tests build a real (tiny)
paired report through `compute_paired_report` — never a hand-written fixture, so the
validator is pinned to the analyzer's actual schema — assert it validates clean, then
break it three ways IN MEMORY and assert each break is caught BY NAME.

RED-first: before `scripts/metrics/gt_compute_check.py` existed, every test here failed
at import (`ModuleNotFoundError: gt_compute_check`).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

_METRICS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "metrics"
if str(_METRICS_DIR) not in sys.path:
    sys.path.insert(0, str(_METRICS_DIR))

import compute_paired_metrics as cpm  # noqa: E402
import gt_compute_check as gcc  # noqa: E402


def _tm(task_id: str, resolved: bool, tok_in: float, tok_out: float) -> "cpm.TaskMetrics":
    return cpm.TaskMetrics(
        task_id=task_id,
        resolved=resolved,
        llm_tokens_in=tok_in,
        llm_tokens_out=tok_out,
    )


@pytest.fixture(scope="module")
def report() -> dict:
    """A tiny but REAL paired report.

    t1/t2 pair (t1 flips, t2 resolved in both); t3 is baseline-only and t4 oracle-only,
    so the population block carries genuine named missingness rather than an empty dict.
    Both paired tasks carry token telemetry, so the net-token endpoint is measured on the
    FULL paired population — the §5-clean shape.
    """
    baseline = {
        "t1": _tm("t1", False, 1000.0, 100.0),
        "t2": _tm("t2", True, 2000.0, 200.0),
        "t3": _tm("t3", False, 500.0, 50.0),
    }
    oracle = {
        "t1": _tm("t1", True, 800.0, 80.0),
        "t2": _tm("t2", True, 1500.0, 150.0),
        "t4": _tm("t4", False, 10.0, 1.0),
    }
    return cpm.compute_paired_report(
        baseline_run=baseline,
        oracle_run=oracle,
        baseline_run_id="off-test",
        oracle_run_id="on-test",
    )


def _names(result: dict) -> list:
    return result["violations"]


def test_real_report_passes_every_doctrine_check(report):
    result = gcc.validate_report(report)
    assert result["violations"] == [], result["violations"]
    assert result["ok"] is True
    # Every check must be answerable on this artifact — an UNVERIFIABLE here would mean
    # the fixture is too thin to exercise the doctrine.
    assert result["n_unverifiable"] == 0, result["unverifiable"]
    assert result["n_pass"] == len(gcc.CHECKS)


def test_population_block_is_real_not_vacuous(report):
    """Guard the guard: the fixture must actually carry named missingness."""
    pop = report["population"]
    assert pop["n_union"] == 4
    assert pop["n_paired"] == 2
    assert set(pop["missing_tasks"]) == {"t3", "t4"}
    assert all(e["missing_reason"] for e in pop["missing_tasks"].values())


# ---------------------------------------------------------------------------
# Break 1 — a missing task loses its named reason
# ---------------------------------------------------------------------------

def test_dropped_missing_reason_is_caught_by_name(report):
    broken = copy.deepcopy(report)
    del broken["population"]["missing_tasks"]["t3"]["missing_reason"]

    result = gcc.validate_report(broken)

    assert result["ok"] is False
    assert "POPULATION_MISSING_REASON_ABSENT:t3" in _names(result)
    pop_check = next(c for c in result["checks"] if c["check"] == "population_locked_union")
    assert pop_check["status"] == gcc.FAIL


# ---------------------------------------------------------------------------
# Break 2 — the headline pp no longer matches the paired denominator
# ---------------------------------------------------------------------------

def test_corrupted_absolute_resolution_pp_is_caught_by_name(report):
    broken = copy.deepcopy(report)
    # 50.0 is the truth for (2-1)/2*100; a full-run-denominator or hand-edited number
    # is exactly the failure §5 forbids.
    assert broken["headline"]["absolute_resolution_pp"] == pytest.approx(50.0)
    broken["headline"]["absolute_resolution_pp"] = 33.33333333

    result = gcc.validate_report(broken)

    assert result["ok"] is False
    assert "HEADLINE_ABSOLUTE_PP_MISMATCH" in _names(result)
    pp_check = next(
        c for c in result["checks"] if c["check"] == "absolute_resolution_pp"
    )
    assert pp_check["status"] == gcc.FAIL
    assert "33.33333333" in json.dumps(pp_check["violations"])


# ---------------------------------------------------------------------------
# Break 3 — an aggregate loses its tie count
# ---------------------------------------------------------------------------

def test_dropped_n_tie_is_caught_by_name(report):
    broken = copy.deepcopy(report)
    target = None
    for name, agg in broken["aggregate"].items():
        if isinstance(agg, dict) and "n_tie" in agg and "delta_mean" in agg:
            target = name
            break
    assert target is not None, "fixture carries no delta aggregate with a sign split"
    del broken["aggregate"][target]["n_tie"]

    result = gcc.validate_report(broken)

    assert result["ok"] is False
    assert f"AGGREGATE_SIGN_SPLIT_ABSENT:{target}:n_tie" in _names(result)
    agg_check = next(
        c for c in result["checks"] if c["check"] == "aggregate_sign_split"
    )
    assert agg_check["status"] == gcc.FAIL


# ---------------------------------------------------------------------------
# The remaining doctrine clauses, each broken once
# ---------------------------------------------------------------------------

def test_fabricated_net_tokens_value_is_caught_by_name(report):
    """A per-task net-token number that also declares its telemetry missing."""
    broken = copy.deepcopy(report)
    broken["per_task"]["t1"]["delta"]["net_tokens_missing_reason"] = "no_token_telemetry_oracle"

    result = gcc.validate_report(broken)

    assert "NET_TOKENS_FABRICATED:t1" in _names(result)


def test_null_net_tokens_without_reason_is_caught_by_name(report):
    broken = copy.deepcopy(report)
    broken["per_task"]["t1"]["delta"]["net_tokens_off_minus_on"] = None
    broken["headline"]["net_tokens_n_measured"] = 1

    result = gcc.validate_report(broken)

    assert "NET_TOKENS_MISSING_REASON_ABSENT:t1" in _names(result)


def test_dropped_holm_block_is_caught_by_name(report):
    broken = copy.deepcopy(report)
    del broken["statistical_tests"]["_holm"]

    result = gcc.validate_report(broken)

    assert "STATS_HOLM_BLOCK_ABSENT" in _names(result)


def test_dropped_bootstrap_ci_is_caught_by_name(report):
    broken = copy.deepcopy(report)
    del broken["statistical_tests"]["m13_wilcoxon"]["bootstrap_ci_lo"]

    result = gcc.validate_report(broken)

    assert "STATS_BOOTSTRAP_CI_ABSENT:m13_wilcoxon:bootstrap_ci_lo" in _names(result)


def test_efficiency_conditioned_on_resolves_is_caught_by_name(report):
    """Measure net tokens ONLY on the tasks that resolved in both arms."""
    broken = copy.deepcopy(report)
    # t1 resolved in oracle only, t2 in both. Strip t1's measurement so the measured
    # population becomes exactly the both-arms-resolved set {t2}.
    broken["per_task"]["t1"]["delta"]["net_tokens_off_minus_on"] = None
    broken["per_task"]["t1"]["delta"]["net_tokens_missing_reason"] = "dropped_unresolved_task"
    broken["headline"]["net_tokens_n_measured"] = 1

    result = gcc.validate_report(broken)

    assert "EFFICIENCY_CONDITIONED_ON_RESOLVES" in _names(result)
    eff = next(
        c for c in result["checks"]
        if c["check"] == "efficiency_not_conditioned_on_resolves"
    )
    assert eff["status"] == gcc.FAIL


def test_missing_population_block_never_passes_silently(report):
    broken = copy.deepcopy(report)
    del broken["population"]

    result = gcc.validate_report(broken)

    assert "POPULATION_BLOCK_ABSENT" in _names(result)


# ---------------------------------------------------------------------------
# UNVERIFIABLE is a third status — never PASS
# ---------------------------------------------------------------------------

def test_unanswerable_check_reports_unverifiable_not_pass(report):
    """Strip the artifact of net-token evidence entirely: the efficiency-conditioning
    clause becomes unanswerable, and the validator must say so rather than pass it."""
    broken = copy.deepcopy(report)
    for row in broken["per_task"].values():
        row["delta"].pop("net_tokens_off_minus_on", None)
        row["delta"].pop("net_tokens_missing_reason", None)
    broken["headline"].pop("net_tokens_n_measured", None)

    result = gcc.validate_report(broken)

    assert result["violations"] == [], result["violations"]
    assert "EFFICIENCY_CONDITIONING_UNVERIFIABLE" in result["unverifiable"]
    assert "NET_TOKENS_UNVERIFIABLE" in result["unverifiable"]
    for name in ("net_tokens_missingness", "efficiency_not_conditioned_on_resolves"):
        check = next(c for c in result["checks"] if c["check"] == name)
        assert check["status"] == gcc.UNVERIFIABLE
        assert check["status"] != gcc.PASS


# ---------------------------------------------------------------------------
# CLI contract: exit 0 all-pass, exit 1 on violation, --json machine output
# ---------------------------------------------------------------------------

def test_cli_exit_codes_and_json_output(report, tmp_path, capsys):
    good = tmp_path / "paired_report_good.json"
    good.write_text(json.dumps(report), encoding="utf-8")
    assert gcc.main([str(good)]) == 0

    broken = copy.deepcopy(report)
    del broken["population"]["missing_tasks"]["t4"]["missing_reason"]
    bad = tmp_path / "paired_report_bad.json"
    bad.write_text(json.dumps(broken), encoding="utf-8")
    capsys.readouterr()
    assert gcc.main([str(bad), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "POPULATION_MISSING_REASON_ABSENT:t4" in payload["violations"]
    assert payload["ok"] is False

    assert gcc.main([str(tmp_path / "does_not_exist.json")]) == 2


def test_strict_unverifiable_flag_fails_on_unanswerable_artifact(report, tmp_path, capsys):
    broken = copy.deepcopy(report)
    for row in broken["per_task"].values():
        row["delta"].pop("net_tokens_off_minus_on", None)
    broken["headline"].pop("net_tokens_n_measured", None)
    path = tmp_path / "paired_report_unverifiable.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    capsys.readouterr()
    assert gcc.main([str(path)]) == 0
    assert gcc.main([str(path), "--strict-unverifiable"]) == 1
