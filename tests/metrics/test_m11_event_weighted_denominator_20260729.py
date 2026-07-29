"""M11 must be denominator-honest: zero-event tasks are UNMEASURED, and the
published aggregate carries an event-weighted view alongside the per-task mean.

THE DEFECT THIS PINS OUT (compute_paired_metrics.py, pre-2026-07-29 fix).
Per-task M11 branches on brief confidence: medium/low means ZERO high-confidence
pins -- an EMPTY denominator -- yet the code wrote
``m11_inverted_confidence_rate = 0.0`` with ``m11_available = True``.  That
manufactured a perfect-calibration verdict out of no evidence, and every such
task entered ``_run_mean`` / ``_vals`` / ``m11_ic_deltas`` as a genuine 0.0,
dragging the run mean toward zero.  Separately, the aggregate was a plain mean
of per-task rates: a task with 1 pin and a task with 50 pins weighed equally,
and no per-event view existed anywhere.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "scripts" / "metrics",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import compute_paired_metrics as cpm  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_task(task_dir: Path, brief: str, edited: str = "pkg/mod.py") -> None:
    """Minimal task dir: trajectory whose submission edits `edited`, plus a brief."""
    task_dir.mkdir(parents=True, exist_ok=True)
    submission = f"--- a/{edited}\n+++ b/{edited}\n@@ -1 +1 @@\n-x\n+y\n"
    traj = {
        "info": {"submission": submission, "model_stats": {"api_calls": 3}},
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "issue text"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "bash",
                                  "arguments": json.dumps({"command": "ls"})}}
                ],
            },
            {"role": "tool", "content": "ok"},
        ],
    }
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps(traj), encoding="utf-8"
    )
    art = task_dir / "gt_artifacts"
    art.mkdir(exist_ok=True)
    (art / "brief.txt").write_text(brief, encoding="utf-8")


MEDIUM_BRIEF = (
    '<gt-localization confidence="medium">\nCandidate edit targets:\n'
    "  1. pkg/mod.py\n</gt-localization>"
)
HIGH_BRIEF_RIGHT = (
    '<gt-localization confidence="high">\nEdit target: pkg/mod.py :: fn\n'
    "</gt-localization>"
)
HIGH_BRIEF_WRONG = (
    '<gt-localization confidence="high">\nEdit target: other/wrong.py :: fn\n'
    "</gt-localization>"
)


def _m11_task(pins: float, wrong: float) -> cpm.TaskMetrics:
    """A TaskMetrics carrying explicit M11 event counts (post-fix semantics)."""
    m = cpm.TaskMetrics()
    m.m11_high_confidence_pins = pins
    m.m11_wrong_high_confidence_pins = wrong
    m.m11_inverted_confidence_rate = (wrong / pins) if pins > 0 else math.nan
    m.m11_available = pins > 0
    return m


# ---------------------------------------------------------------------------
# Per-task: an empty denominator must never read 0.0
# ---------------------------------------------------------------------------

def test_zero_event_task_is_unmeasured_not_zero(tmp_path: Path) -> None:
    """Medium/low confidence = ZERO high pins: the rate has no denominator."""
    td = tmp_path / "task_medium"
    _write_task(td, MEDIUM_BRIEF)

    m = cpm.compute_task_metrics("task_medium", td)
    assert m is not None
    assert m.m11_high_confidence_pins == 0.0
    # THE FIX: NaN + a named reason, never a manufactured 0.0.
    assert math.isnan(m.m11_inverted_confidence_rate)
    assert m.m11_available is False
    assert m.m11_unmeasured_reason == "zero_high_confidence_pins"


def test_high_pin_task_is_still_measured(tmp_path: Path) -> None:
    """The fix must not swallow the genuine one-pin measurements."""
    td_right = tmp_path / "task_high_right"
    _write_task(td_right, HIGH_BRIEF_RIGHT)
    td_wrong = tmp_path / "task_high_wrong"
    _write_task(td_wrong, HIGH_BRIEF_WRONG)

    right = cpm.compute_task_metrics("task_high_right", td_right)
    wrong = cpm.compute_task_metrics("task_high_wrong", td_wrong)
    assert right is not None and wrong is not None
    assert right.m11_high_confidence_pins == 1.0
    assert right.m11_inverted_confidence_rate == 0.0
    assert right.m11_available is True
    assert right.m11_unmeasured_reason == ""
    assert wrong.m11_inverted_confidence_rate == 1.0


def test_high_confidence_unparseable_pin_names_its_reason(tmp_path: Path) -> None:
    """HIGH confidence but no recognisable edit-target line: unmeasured, named."""
    td = tmp_path / "task_high_noparse"
    _write_task(
        td,
        '<gt-localization confidence="high">\nno target line here\n</gt-localization>',
    )
    m = cpm.compute_task_metrics("task_high_noparse", td)
    assert m is not None
    assert math.isnan(m.m11_inverted_confidence_rate)
    assert m.m11_unmeasured_reason == "high_confidence_pin_unparsed"


# ---------------------------------------------------------------------------
# Aggregation: zero-event tasks must not pollute the mean, and an
# event-weighted view must exist alongside the per-task mean
# ---------------------------------------------------------------------------

def test_zero_event_tasks_do_not_pollute_the_per_task_mean(tmp_path: Path) -> None:
    """End-to-end: 1 wrong HIGH pin + 3 medium tasks. Old mean = 0.25; true = 1.0."""
    tasks = {}
    for name, brief in [
        ("t_wrong", HIGH_BRIEF_WRONG),
        ("t_med1", MEDIUM_BRIEF),
        ("t_med2", MEDIUM_BRIEF),
        ("t_med3", MEDIUM_BRIEF),
    ]:
        td = tmp_path / name
        _write_task(td, brief)
        tm = cpm.compute_task_metrics(name, td)
        assert tm is not None
        tasks[name] = tm

    report = cpm.compute_paired_report(tasks, tasks, "base", "oracle")
    agg = report["aggregate"]["M11"]
    # Only the one measured task carries a rate; the three zero-event tasks are out.
    assert agg["baseline_mean"] == 1.0
    assert agg["oracle_mean"] == 1.0


def test_event_weighted_aggregate_exists_with_its_ns() -> None:
    """A 50-pin task and a 1-pin task must NOT weigh equally in the event view."""
    baseline = {"t_many": _m11_task(pins=50.0, wrong=10.0),
                "t_one": _m11_task(pins=1.0, wrong=1.0),
                "t_zero": _m11_task(pins=0.0, wrong=0.0)}
    oracle = {"t_many": _m11_task(pins=50.0, wrong=5.0),
              "t_one": _m11_task(pins=1.0, wrong=0.0),
              "t_zero": _m11_task(pins=0.0, wrong=0.0)}

    agg = cpm.compute_paired_report(baseline, oracle, "base", "oracle")["aggregate"]["M11"]

    # The old field survives, marked as the per-task view.
    assert agg["baseline_mean"] == pytest.approx((10.0 / 50.0 + 1.0) / 2.0)
    assert "per_task_mean" in agg["aggregation_note"]

    ew = agg["event_weighted"]
    assert ew["baseline_pins"] == 51.0
    assert ew["baseline_wrong_pins"] == 11.0
    assert ew["baseline_rate"] == pytest.approx(11.0 / 51.0)
    assert ew["oracle_rate"] == pytest.approx(5.0 / 51.0)
    assert ew["n_tasks_with_events_baseline"] == 2
    assert ew["n_tasks_zero_event_baseline"] == 1


def test_event_weighted_rate_is_null_when_no_events_anywhere() -> None:
    """All-zero-event runs: the event-weighted rate must be null, never 0.0."""
    arm = {"t1": _m11_task(0.0, 0.0), "t2": _m11_task(0.0, 0.0)}
    agg = cpm.compute_paired_report(dict(arm), dict(arm), "b", "o")["aggregate"]["M11"]
    ew = agg["event_weighted"]
    assert ew["baseline_rate"] is None
    assert ew["oracle_rate"] is None
    assert ew["baseline_pins"] == 0.0


def test_regression_guard_carries_the_event_weighted_view() -> None:
    """Per-task mean can hide an event-weighted regression; both flags must exist."""
    # Baseline: 10 pins 1 wrong (0.1) + 1 pin 1 wrong (1.0) -> task mean 0.55, event 2/11
    # Oracle:   10 pins 5 wrong (0.5) + 1 pin 0 wrong (0.0) -> task mean 0.25, event 5/11
    baseline = {"ta": _m11_task(10.0, 1.0), "tb": _m11_task(1.0, 1.0)}
    oracle = {"ta": _m11_task(10.0, 5.0), "tb": _m11_task(1.0, 0.0)}

    guards = cpm.compute_paired_report(baseline, oracle, "b", "o")["regression_guards"]

    # Per-task mean DROPPED (0.55 -> 0.25) but per-event rate ROSE (2/11 -> 5/11).
    assert guards["m11_inverted_confidence_increased"] is False
    assert guards["m11_inverted_confidence_increased_event_weighted"] is True
    assert guards["any_regression_triggered"] is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
