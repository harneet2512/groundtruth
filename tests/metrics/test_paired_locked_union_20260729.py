"""The paired report's population is a locked UNION with named missingness — §5.

THE DEFECT THIS PINS OUT (2026-07-29). `compute_paired_report` derived its population
as `set(baseline) & set(oracle)` — a silent intersection. A task present in one arm
and absent from the other simply vanished: `n_tasks` shrank and nothing said so,
which breaks the locked-task-union law in the one line every downstream number
depends on. §5 also names E1 (absolute resolution pp, BEFORE relative lift) and E3
(task net tokens off−on) as primary endpoints; neither existed anywhere in the repo.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "scripts" / "metrics",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import compute_paired_metrics as cpm  # noqa: E402


def _task(resolved: bool, tokens_in: float = 1000.0, tokens_out: float = 200.0):
    m = cpm.TaskMetrics()
    m.resolved = resolved
    m.llm_tokens_in = tokens_in
    m.llm_tokens_out = tokens_out
    return m


def _report(baseline, oracle):
    return cpm.compute_paired_report(baseline, oracle, "base_run", "oracle_run")


def test_a_task_missing_from_one_arm_is_named_not_vanished():
    baseline = {"t1": _task(False), "t2": _task(True), "only_off": _task(False)}
    oracle = {"t1": _task(True), "t2": _task(True), "only_on": _task(False)}

    report = _report(baseline, oracle)

    pop = report["population"]
    assert pop["n_union"] == 4
    assert pop["n_paired"] == 2
    assert pop["n_baseline_only"] == 1
    assert pop["n_oracle_only"] == 1
    assert pop["missing_tasks"]["only_off"]["missing_reason"] == "absent_from_oracle_run"
    assert pop["missing_tasks"]["only_on"]["missing_reason"] == "absent_from_baseline_run"
    # pairing still happens only where both arms measured
    assert report["n_shared_tasks"] == 2
    assert set(report["per_task"]) == {"t1", "t2"}


def test_absolute_resolution_pp_uses_the_paired_denominator():
    """E1. The full-run counts can sit on different denominators; the pp may not."""
    baseline = {"t1": _task(False), "t2": _task(False),
                "off_extra_resolved": _task(True)}   # unpaired resolve: must not leak in
    oracle = {"t1": _task(True), "t2": _task(False)}

    headline = _report(baseline, oracle)["headline"]

    # paired population = {t1, t2}: baseline 0/2, oracle 1/2 -> +50.0 pp
    assert headline["resolved_baseline_paired"] == 0
    assert headline["resolved_oracle_paired"] == 1
    assert headline["absolute_resolution_pp"] == 50.0
    # the full-run count still reports the unpaired resolve — visibly, separately
    assert headline["resolved_baseline"] == 1


def test_net_tokens_is_off_minus_on_and_missing_telemetry_is_not_a_zero():
    """E3. Positive = GT saved tokens; a missing token field must never read as 'no savings'."""
    baseline = {
        "t1": _task(False, tokens_in=2000.0, tokens_out=500.0),
        "t2": _task(False, tokens_in=0.0, tokens_out=0.0),  # no telemetry
    }
    oracle = {
        "t1": _task(False, tokens_in=1500.0, tokens_out=400.0),
        "t2": _task(False, tokens_in=900.0, tokens_out=100.0),
    }

    report = _report(baseline, oracle)

    t1 = report["per_task"]["t1"]["delta"]
    assert t1["net_tokens_off_minus_on"] == 600.0  # 2500 - 1900
    t2 = report["per_task"]["t2"]["delta"]
    assert t2["net_tokens_off_minus_on"] is None
    assert t2["net_tokens_missing_reason"] == "no_token_telemetry_baseline"
    headline = report["headline"]
    assert headline["net_tokens_n_measured"] == 1
    assert headline["net_tokens_mean"] == 600.0
    stats_block = report["statistical_tests"]["net_tokens_wilcoxon"]
    assert stats_block["direction"] == "net_positive_means_gt_saved_tokens"


def test_aggregates_carry_sign_split_counts():
    """§5: median and +/−/tie counts, so a bimodal delta cannot hide behind a mean."""
    baseline = {f"t{i}": _task(False) for i in range(3)}
    oracle = {f"t{i}": _task(False) for i in range(3)}
    baseline["t0"].m03_total_steps = 10.0
    oracle["t0"].m03_total_steps = 8.0    # negative delta (oracle fewer)
    baseline["t1"].m03_total_steps = 10.0
    oracle["t1"].m03_total_steps = 12.0   # positive
    baseline["t2"].m03_total_steps = 10.0
    oracle["t2"].m03_total_steps = 10.0   # tie

    agg = _report(baseline, oracle)["aggregate"]["M03"]

    assert (agg["n_positive"], agg["n_negative"], agg["n_tie"]) == (1, 1, 1)
    assert agg["n_positive"] + agg["n_negative"] + agg["n_tie"] == agg["n"]


def test_identical_arms_produce_a_clean_null_report():
    """Determinism + null sanity: same tasks both arms -> 0 pp, all-tie, no missing."""
    arm = {f"t{i}": _task(i % 2 == 0) for i in range(4)}
    report = _report(dict(arm), dict(arm))

    assert report["population"]["missing_tasks"] == {}
    assert report["headline"]["absolute_resolution_pp"] == 0.0
    assert report["headline"]["flip_count"] == 0
    nt = report["headline"]["net_tokens_median"]
    assert nt == 0.0 or (isinstance(nt, float) and math.isnan(nt))
