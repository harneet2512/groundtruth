"""M05 must never count an UNGRADEABLE delivery as INERT.

Reference defect (compute_paired_metrics.py, pre-2026-07-29): consumption was
graded by substring-matching GT-cited filenames against the next 3 assistant
bash commands -- token overlap, which CLAUDE.md §2 REJECTS as a consumption
grade -- and the reported shape was a bare ``else: inert += 1``.  A
``<gt-task-brief>`` obligations block cites NO file path, so the rule had
nothing to match on and every such delivery was booked as PROVEN INERT.  On
run6_29714439700 that produced 71/71 tasks at ``m05_consumption_rate_inert ==
1.00`` and ``m07_suppression_precision == 1.00`` ("suppress everything") --
36-style manufactured verdicts out of missing evidence.

The sanctioned grade is now the receipt ladder (delivered -> referenced ->
acted) from scripts/swebench/consumption_ledger.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_CPM_PATH = Path(__file__).resolve().parents[2] / "scripts" / "metrics" / "compute_paired_metrics.py"


def _load_cpm():
    spec = importlib.util.spec_from_file_location("compute_paired_metrics_t", _CPM_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["compute_paired_metrics_t"] = mod
    spec.loader.exec_module(mod)
    return mod


cpm = _load_cpm()


def _write_traj(task_dir: Path, gt_block: str, commands: list[str]) -> None:
    """A minimal mini-swe-agent trajectory: one GT delivery then N bash steps."""
    messages: list[dict] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": f"issue text\n{gt_block}"},
    ]
    for cmd in commands:
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"command": cmd}),
                        }
                    }
                ],
            }
        )
        messages.append({"role": "tool", "content": "ok"})
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({"info": {}, "messages": messages}), encoding="utf-8"
    )


OBLIGATIONS_BLOCK = (
    "<gt-task-brief>\n\nRequirements to satisfy (from the issue):\n"
    "- [ ] Add function get_value to FSMContext\n</gt-task-brief>"
)
LOCALIZATION_BLOCK = (
    '<gt-localization confidence="medium">\nCandidate edit targets:\n'
    "  1. pkg/thing/target_module.py\n</gt-localization>"
)


def test_file_free_block_is_unmeasured_never_inert(tmp_path: Path) -> None:
    """An obligations block cites no path: the rule cannot grade it."""
    td = tmp_path / "task_oblig"
    _write_traj(td, OBLIGATIONS_BLOCK, ["ls", "grep -rn foo .", "cat README.md"])

    m = cpm.compute_task_metrics("task_oblig", td)
    assert m is not None
    assert m.m05_deliveries_total == 1.0
    # THE FIX: booked UNMEASURED, not INERT.
    assert m.m05_unmeasured_deliveries == 1.0
    assert m.m05_consumption_rate_unmeasured == 1.0
    assert m.m05_consumption_rate_token_overlap_inert_UNSANCTIONED == 0.0
    # The old field name must be gone so no reader can silently keep citing it.
    assert not hasattr(m, "m05_consumption_rate_inert")


def test_m07_prefers_the_receipt_ladder_over_the_token_bucket(tmp_path: Path) -> None:
    """M07 previously read (inert + harmful)/total off the defective bucket."""
    td = tmp_path / "task_m07"
    _write_traj(td, OBLIGATIONS_BLOCK, ["ls", "pwd"])

    m = cpm.compute_task_metrics("task_m07", td)
    assert m is not None
    # The ladder CAN grade this row (the block is delivered and no later
    # assistant prose names it) -> a real measured verdict, from the sanctioned
    # authority, not from the token-overlap INERT bucket.
    assert m.m07_suppression_precision_source == "receipt_ladder_v2"
    assert m.m05_receipt_ladder_available is True


def test_m07_is_unmeasured_when_no_row_can_be_graded(tmp_path: Path, monkeypatch) -> None:
    """With no ladder, an all-ungradeable task must be UNMEASURED, not 1.00."""
    td = tmp_path / "task_m07_noladder"
    _write_traj(td, OBLIGATIONS_BLOCK, ["ls", "pwd"])

    monkeypatch.setattr(cpm, "_CONSUMPTION_LEDGER", None)
    m = cpm.compute_task_metrics("task_m07_noladder", td)
    assert m is not None
    assert m.m05_consumption_rate_unmeasured == 1.0
    # BEFORE the fix this was 1.00 ("suppress every delivery").
    assert m.m07_available is False
    assert m.m07_suppression_precision_source == "UNMEASURED_no_gradeable_deliveries"


def test_gradeable_non_match_still_counts_inert(tmp_path: Path) -> None:
    """The UNMEASURED bucket must not swallow genuine non-consumption."""
    td = tmp_path / "task_inert"
    _write_traj(td, LOCALIZATION_BLOCK, ["ls", "cat other/unrelated.py", "pwd"])

    m = cpm.compute_task_metrics("task_inert", td)
    assert m is not None
    # The block cites a path AND commands follow -> the rule applies and misses.
    assert m.m05_consumption_rate_token_overlap_inert_UNSANCTIONED == 1.0
    assert m.m05_consumption_rate_unmeasured == 0.0


def test_m10_regression_rate_is_unmeasured_not_zero(tmp_path: Path) -> None:
    """M10 hardcoded 0.0/available=True: a "zero regressions" verdict from nothing."""
    td = tmp_path / "task_m10"
    _write_traj(td, OBLIGATIONS_BLOCK, ["ls"])

    m = cpm.compute_task_metrics("task_m10", td)
    assert m is not None
    assert m.m10_available is False
    assert m.m10_regression_rate != 0.0  # NaN


def test_is_measured_number_rejects_null_and_nan() -> None:
    """`_fmt` writes NaN as JSON null; both must be dropped, never coerced."""
    import math

    assert cpm._is_measured_number(0.0) is True
    assert cpm._is_measured_number(3) is True
    assert cpm._is_measured_number(None) is False
    assert cpm._is_measured_number(math.nan) is False
    assert cpm._is_measured_number(True) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
