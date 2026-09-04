"""P7 — trajectory scorecard on paired TaskMetrics."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("scipy", reason="scipy is not installed")

_ROOT = Path(__file__).resolve().parents[1]
_METRICS_PATH = _ROOT / "scripts" / "metrics" / "compute_paired_metrics.py"


def _load_metrics():
    spec = importlib.util.spec_from_file_location("cpm_p7", _METRICS_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cpm_p7"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cpm():
    return _load_metrics()


def _task(cpm, tid: str, *, resolved: bool, behavioral: float, m21: float):
    t = cpm.TaskMetrics(task_id=tid, resolved=resolved)
    t.m05_consumption_rate_behavioral = behavioral
    t.m21_patch_completeness = m21
    t.m19_test_evidence_consumed = 1.0 if behavioral > 0 else 0.0
    return t


def test_scorecard_counts_gt_caused_flip(cpm):
    base = {"a": _task(cpm, "a", resolved=False, behavioral=0, m21=0)}
    ora = {
        "a": _task(cpm, "a", resolved=True, behavioral=0.5, m21=0.8),
        "b": _task(cpm, "b", resolved=True, behavioral=0.0, m21=0.2),
    }
    card = cpm.compute_trajectory_scorecard(base, ora, ["a", "b"])
    assert card["flip_count"] == 2
    assert card["gt_caused_heuristic_flips"] == 1
    assert card["flip_tasks"][0]["gt_caused_heuristic"] is True


def test_scorecard_regression_count(cpm):
    base = {"x": _task(cpm, "x", resolved=True, behavioral=0, m21=1)}
    ora = {"x": _task(cpm, "x", resolved=False, behavioral=0.5, m21=0.5)}
    card = cpm.compute_trajectory_scorecard(base, ora, ["x"])
    assert card["regression_count"] == 1
    assert card["flip_count"] == 0
