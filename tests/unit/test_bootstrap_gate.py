"""Artifact-first TTD: bootstrap failure rate must gate smoke arms.

Frozen artifact: tests/fixtures/trajectory/nolsp_13453/ and nolsp_13579/
show 0-edit bootstrap crashes. The Qwen nolsp arm had 6/10 tasks with
this pattern (rate=0.60). An arm with >30% bootstrap failures is invalid
for baseline comparison — its resolve count is meaningless.

Expected behaviors from EXPECTED_BEHAVIOR_BOOTSTRAP_GATE.md:
  EB-GATE-2: rate >= 0.30 → arm invalid
  EB-GATE-3: rate <= 0.10 → arm passes

These tests call check_bootstrap_rate() which does NOT exist yet.
They MUST fail before implementation.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

try:
    from scripts.swebench.verify_report import check_bootstrap_rate
except ImportError:
    check_bootstrap_rate = None

requires_gate = pytest.mark.skipif(
    check_bootstrap_rate is None,
    reason="check_bootstrap_rate not implemented yet — test is RED"
)


def _make_arm_csv(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a gt_report.csv with the given rows."""
    csv_path = tmp_path / "gt_report.csv"
    fields = ["instance_id", "material_edit_count", "cycle"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return tmp_path


@requires_gate
def test_high_bootstrap_failure_rate_invalidates_arm(tmp_path):
    """From EB-GATE-2: 6/10 tasks with 0 edits at cycle <= 2 → rate=0.60.
    Must be flagged invalid (rate >= 0.30 threshold)."""
    rows = [
        {"instance_id": f"task-{i}", "material_edit_count": 0, "cycle": 1}
        for i in range(6)
    ] + [
        {"instance_id": f"task-{i}", "material_edit_count": 3, "cycle": 20}
        for i in range(6, 10)
    ]
    run_dir = _make_arm_csv(tmp_path, rows)
    result = check_bootstrap_rate(run_dir)
    assert result["bootstrap_failure_count"] == 6
    assert result["bootstrap_failure_rate"] >= 0.30
    assert result["arm_valid"] is False, (
        f"60% bootstrap failure rate must invalidate the arm. "
        f"Got arm_valid={result['arm_valid']}"
    )


@requires_gate
def test_low_bootstrap_failure_rate_passes(tmp_path):
    """From EB-GATE-3: 1/10 tasks with bootstrap failure → rate=0.10.
    Must pass (rate <= 0.10 < threshold 0.30)."""
    rows = [
        {"instance_id": "task-0", "material_edit_count": 0, "cycle": 1}
    ] + [
        {"instance_id": f"task-{i}", "material_edit_count": 2, "cycle": 15}
        for i in range(1, 10)
    ]
    run_dir = _make_arm_csv(tmp_path, rows)
    result = check_bootstrap_rate(run_dir)
    assert result["bootstrap_failure_count"] == 1
    assert result["arm_valid"] is True


@requires_gate
def test_zero_bootstrap_failures_passes(tmp_path):
    """All tasks have edits → rate=0 → valid."""
    rows = [
        {"instance_id": f"task-{i}", "material_edit_count": 2, "cycle": 15}
        for i in range(10)
    ]
    run_dir = _make_arm_csv(tmp_path, rows)
    result = check_bootstrap_rate(run_dir)
    assert result["bootstrap_failure_count"] == 0
    assert result["arm_valid"] is True


@requires_gate
def test_task_with_edits_at_low_cycle_is_not_bootstrap_failure(tmp_path):
    """A task at cycle=2 that HAS material edits is NOT a bootstrap failure.
    Only 0-edit + low-cycle = bootstrap failure."""
    rows = [
        {"instance_id": "task-0", "material_edit_count": 1, "cycle": 2},
        {"instance_id": "task-1", "material_edit_count": 0, "cycle": 1},
    ]
    run_dir = _make_arm_csv(tmp_path, rows)
    result = check_bootstrap_rate(run_dir)
    assert result["bootstrap_failure_count"] == 1, (
        "Only task-1 (0 edits, cycle 1) is a bootstrap failure. "
        "task-0 has edits and is not."
    )
