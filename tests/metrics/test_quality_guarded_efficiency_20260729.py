"""E2 — the QUALITY GUARD: an efficiency number is citable ONLY if quality held.

THE DEFECT THIS PINS OUT (2026-07-29). CLAUDE.md §5 says "never call faster-but-worse
efficient" in PROSE and nothing enforced it. `compute_paired_report` emitted
`net_tokens_mean/median` and the M03 step delta as free-standing headline numbers, so a
run that got FASTER while getting WORSE (harm rate up, hidden tests down, resolves down,
a regression guard tripped) still produced a citable-looking efficiency headline. Worse,
a quality signal that was simply ABSENT read as "no regression" — missing defaulted to
held, which is the fail-OPEN direction.

`quality_guarded_efficiency` makes the efficiency numbers conditional and fail-closed:
missing quality means UNMEASURED with the signal NAMED, never a win.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "scripts" / "metrics",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import compute_paired_metrics as cpm  # noqa: E402


def _task(
    resolved: bool = False,
    tokens_in: float = 1000.0,
    tokens_out: float = 200.0,
    steps: float = 10.0,
):
    m = cpm.TaskMetrics()
    m.resolved = resolved
    m.llm_tokens_in = tokens_in
    m.llm_tokens_out = tokens_out
    m.m03_total_steps = steps
    return m


def _report(baseline, oracle):
    return cpm.compute_paired_report(baseline, oracle, "base_run", "oracle_run")


def _qge(baseline, oracle):
    return _report(baseline, oracle)["quality_guarded_efficiency"]


def _faster_arms():
    """GT-on spends fewer tokens and fewer steps on both paired tasks."""
    baseline = {
        "t1": _task(tokens_in=2000.0, tokens_out=500.0, steps=12.0),
        "t2": _task(tokens_in=1800.0, tokens_out=400.0, steps=11.0),
    }
    oracle = {
        "t1": _task(tokens_in=1500.0, tokens_out=400.0, steps=9.0),
        "t2": _task(tokens_in=1400.0, tokens_out=300.0, steps=8.0),
    }
    return baseline, oracle


# ---------------------------------------------------------------------------
# (a) faster + quality held -> WIN
# ---------------------------------------------------------------------------

def test_faster_with_quality_held_is_the_only_citable_win():
    baseline, oracle = _faster_arms()

    qge = _qge(baseline, oracle)

    assert qge["verdict"] == "EFFICIENCY_WIN_QUALITY_HELD"
    assert qge["citable_efficiency"] is True
    assert qge["efficiency_improved"] is True
    assert qge["quality_held"] is True
    assert qge["missing_signals"] == []
    assert qge["quality_regressed_signals"] == []

    # efficiency signals are REPORT-only numbers, both directions named
    eff = qge["efficiency_signals"]
    assert eff["net_tokens"]["measured"] is True
    assert eff["net_tokens"]["median"] == 550.0     # median of 600 (2500-1900), 500 (2200-1700)
    assert eff["net_tokens"]["mean"] == 550.0
    assert eff["net_tokens"]["improved"] is True
    assert eff["net_tokens"]["direction"] == "positive_means_gt_saved_tokens"
    assert eff["steps_m03"]["median"] == -3.0            # oracle used fewer steps
    assert eff["steps_m03"]["improved"] is True

    # quality signals: pp measured at 0, guards clean, M20 unmeasured WITH a reason
    q = qge["quality_signals"]
    assert q["absolute_resolution_pp"]["value"] == 0.0
    assert q["absolute_resolution_pp"]["regressed"] is False
    assert q["regression_guards"]["any_regression_triggered"] is False
    assert q["m20_hidden_tests"]["measured"] is False
    assert q["m20_hidden_tests"]["unmeasured_with_named_reason"] is True
    assert (
        q["m20_hidden_tests"]["missing_reason"]
        == "no_hidden_test_telemetry_in_either_arm"
    )

    # basis names the signals AND the values behind the verdict
    basis = qge["basis"]
    assert basis["values"]["net_tokens_median"] == 550.0
    assert basis["values"]["steps_m03_median"] == -3.0
    assert basis["values"]["absolute_resolution_pp"] == 0.0
    assert basis["values"]["any_regression_triggered"] is False
    assert "net_tokens" in basis["signals_used"]
    assert "absolute_resolution_pp" in basis["signals_used"]

    # and the headline itself carries the gate next to the numbers it governs
    headline = _report(baseline, oracle)["headline"]
    assert headline["quality_guarded_efficiency_verdict"] == "EFFICIENCY_WIN_QUALITY_HELD"
    assert headline["citable_efficiency"] is True


# ---------------------------------------------------------------------------
# (b) faster + a regressed quality signal -> FASTER_BUT_WORSE, NON-citable
# ---------------------------------------------------------------------------

def test_faster_but_harm_rate_regressed_makes_efficiency_non_citable():
    baseline, oracle = _faster_arms()
    for tid in baseline:
        baseline[tid].m05_harm_rate = 0.10
        oracle[tid].m05_harm_rate = 0.50   # GT-on harmed MORE

    qge = _qge(baseline, oracle)

    assert qge["verdict"] == "FASTER_BUT_WORSE"
    # the whole point: the efficiency numbers may not be cited
    assert qge["citable_efficiency"] is False
    assert qge["efficiency_improved"] is True
    assert qge["quality_held"] is False
    assert "regression_guards" in qge["quality_regressed_signals"]
    assert qge["quality_signals"]["regression_guards"]["any_regression_triggered"] is True
    assert "harm_rate_increased" in qge["quality_signals"]["regression_guards"]["triggered"]
    # the numbers are still REPORTED — they are just not a win
    assert qge["efficiency_signals"]["net_tokens"]["median"] == 550.0
    assert qge["basis"]["values"]["regression_guards_triggered"] == ["harm_rate_increased"]

    headline = _report(baseline, oracle)["headline"]
    assert headline["citable_efficiency"] is False


def test_faster_but_hidden_tests_regressed_is_faster_but_worse():
    """M20 is the PRIMARY code-correctness metric: fewer hidden tests pass = worse."""
    baseline, oracle = _faster_arms()
    for tid in baseline:
        baseline[tid].m20_available = True
        baseline[tid].m20_hidden_tests_pass = 8.0
        oracle[tid].m20_available = True
        oracle[tid].m20_hidden_tests_pass = 5.0

    qge = _qge(baseline, oracle)

    assert qge["verdict"] == "FASTER_BUT_WORSE"
    assert qge["citable_efficiency"] is False
    assert qge["quality_regressed_signals"] == ["m20_hidden_tests"]
    assert qge["quality_signals"]["m20_hidden_tests"]["median"] == -3.0


def test_faster_but_resolution_regressed_is_faster_but_worse():
    baseline, oracle = _faster_arms()
    baseline["t1"].resolved = True   # GT-on LOST a resolve while going faster
    oracle["t1"].resolved = False

    qge = _qge(baseline, oracle)

    assert qge["verdict"] == "FASTER_BUT_WORSE"
    assert qge["citable_efficiency"] is False
    assert "absolute_resolution_pp" in qge["quality_regressed_signals"]
    assert qge["quality_signals"]["absolute_resolution_pp"]["value"] == -50.0


# ---------------------------------------------------------------------------
# (c) faster + M20 missing WITHOUT a named reason -> UNMEASURED, fail-closed
# ---------------------------------------------------------------------------

def test_faster_with_unexplained_missing_m20_is_unmeasured_never_a_win():
    """Both arms claim hidden-test telemetry yet no number exists: fail CLOSED."""
    baseline, oracle = _faster_arms()
    for tid in baseline:
        baseline[tid].m20_available = True
        oracle[tid].m20_available = True
        # ...but m20_hidden_tests_pass stays NaN — the hole the guard must catch
        assert math.isnan(baseline[tid].m20_hidden_tests_pass)

    qge = _qge(baseline, oracle)

    assert qge["verdict"] == "UNMEASURED"
    assert qge["verdict"] != "EFFICIENCY_WIN_QUALITY_HELD"
    assert qge["citable_efficiency"] is False
    assert qge["missing_signals"] == ["m20_hidden_tests"]
    m20 = qge["quality_signals"]["m20_hidden_tests"]
    assert m20["measured"] is False
    assert m20["missing_reason"] is None
    assert m20["unmeasured_with_named_reason"] is False
    # a missing signal must never be reported as "held"
    assert m20["regressed"] is None
    assert "fail-closed" in qge["basis"]["rule"]


def test_m20_measured_in_one_arm_only_is_a_named_reason_not_a_hole():
    """The named-reason escape hatch is narrow and explicit."""
    baseline, oracle = _faster_arms()
    for tid in oracle:
        oracle[tid].m20_available = True
        oracle[tid].m20_hidden_tests_pass = 7.0

    qge = _qge(baseline, oracle)

    m20 = qge["quality_signals"]["m20_hidden_tests"]
    assert m20["measured"] is False
    assert m20["missing_reason"] == "hidden_tests_measured_in_one_arm_only"
    assert m20["unmeasured_with_named_reason"] is True
    assert qge["verdict"] == "EFFICIENCY_WIN_QUALITY_HELD"


# ---------------------------------------------------------------------------
# (d) slower -> NO_EFFICIENCY_GAIN
# ---------------------------------------------------------------------------

def test_slower_and_more_tokens_is_no_efficiency_gain():
    baseline = {
        "t1": _task(tokens_in=1000.0, tokens_out=200.0, steps=10.0),
        "t2": _task(tokens_in=1000.0, tokens_out=200.0, steps=10.0),
    }
    oracle = {
        "t1": _task(tokens_in=1600.0, tokens_out=300.0, steps=14.0),
        "t2": _task(tokens_in=1500.0, tokens_out=300.0, steps=13.0),
    }

    qge = _qge(baseline, oracle)

    assert qge["verdict"] == "NO_EFFICIENCY_GAIN"
    assert qge["citable_efficiency"] is False
    assert qge["efficiency_improved"] is False
    assert qge["efficiency_signals"]["net_tokens"]["improved"] is False
    assert qge["efficiency_signals"]["steps_m03"]["improved"] is False
    assert qge["efficiency_signals"]["net_tokens"]["median"] == -650.0
    assert qge["efficiency_signals"]["steps_m03"]["median"] == 3.5


def test_no_efficiency_signal_measured_at_all_is_unmeasured():
    """Both arms have zero token telemetry and identical steps is NOT the case here:
    with an EMPTY paired population nothing is measured, so 'faster' is unknown."""
    qge = _qge({"only_off": _task()}, {"only_on": _task()})

    assert qge["verdict"] == "UNMEASURED"
    assert qge["citable_efficiency"] is False
    assert qge["efficiency_improved"] is None
    assert qge["quality_held"] is None
    assert "net_tokens" in qge["missing_signals"]
    assert "steps_m03" in qge["missing_signals"]
    assert "absolute_resolution_pp" in qge["missing_signals"]


# ---------------------------------------------------------------------------
# (e) determinism
# ---------------------------------------------------------------------------

def test_quality_guard_is_deterministic():
    baseline, oracle = _faster_arms()
    baseline["t2"].m05_harm_rate = 0.2
    oracle["t2"].m05_harm_rate = 0.2

    first = json.dumps(_qge(dict(baseline), dict(oracle)), sort_keys=True)
    second = json.dumps(_qge(dict(baseline), dict(oracle)), sort_keys=True)

    assert first == second
    # and stable under task-insertion order
    reordered_base = {k: baseline[k] for k in reversed(list(baseline))}
    reordered_oracle = {k: oracle[k] for k in reversed(list(oracle))}
    third = json.dumps(_qge(reordered_base, reordered_oracle), sort_keys=True)
    assert first == third
