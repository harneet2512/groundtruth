"""A zero delivery denominator is UNMEASURED, never a measured ``impact_rate`` of 0.0.

DEFECT (pre-fix): ``gt_behavioral_impact.analyze_trajectory`` wrote
``impact_rate = 0.0`` when ``total_deliveries == 0``, while
``gt_deep_metrics.py:2100-2104`` writes ``None`` on the same predicate and
``gt_performance_metrics.py:899`` stamps the metric ``applicable=False``.  Two
consequences, both real:

  1. MEANING — "0% of GT deliveries were followed by a pivot" is a manufactured
     verdict when there were no deliveries and therefore no pivot opportunity.
  2. GATE — the per-task mandatory-metrics gate in
     ``swebench_live_lite_full.yml`` (the ``behavioral_deep_parity`` leg)
     compares every ``deep["behavioral_impact"]`` item against the impact
     artifact's ``summary``.  ``0.0 != None`` on EVERY zero-delivery task, so
     each one failed with ``mandatory_metric_detail_invalid_or_parity`` and was
     not citable.

These tests are artifact-first: they drive the REAL producers (deep build + the
impact CLI) over one trajectory and then apply the workflow's own comparison
expression, which is pinned against the yml text so a rewritten gate cannot
silently drift away from what is asserted here.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))

from scripts.swebench import gt_deep_metrics
from scripts.swebench.gt_behavioral_impact import (
    analyze_trajectory,
    main as behavioral_main,
)

_ROOT = Path(__file__).resolve().parents[2]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"

# One assistant search + one plain tool observation carrying NO GT bytes and no
# runtime ledger: a genuine zero-delivery task, the exact shape that tripped the
# parity gate.
_NO_DELIVERY_TRAJECTORY = {
    "messages": [
        {"role": "assistant", "content": "grep -rn parse_config src/"},
        {"role": "tool", "content": "src/pkg.py:12:def parse_config(path):"},
        {
            "role": "assistant",
            "content": "Editing.",
            "tool_calls": [{"function": {"arguments": json.dumps(
                {"command": "sed -i 's/old/new/' src/pkg.py"}
            )}}],
        },
    ]
}


def _parity_snippet() -> str:
    """The gate's behavioral parity expression, read from the workflow itself."""
    text = _WF.read_text(encoding="utf-8")
    match = re.search(
        r"deep_behavioral = deep\.get\(\"behavioral_impact\"\).*?"
        r"behavioral_deep_parity",
        text,
        re.S,
    )
    assert match is not None, "the behavioral_deep_parity gate leg moved or was renamed"
    return match.group(0)


def test_zero_deliveries_emit_unmeasured_impact_rate_with_a_named_reason() -> None:
    summary = analyze_trajectory(_NO_DELIVERY_TRAJECTORY)["summary"]

    assert summary["total_deliveries"] == 0
    assert summary["total_pivots"] == 0
    assert summary["impact_rate"] is None
    assert summary["impact_rate_reason"] == "zero_denominator_no_gt_deliveries"
    # The semantics/causal stamps are unrelated to the denominator and must not move.
    assert summary["impact_rate_semantics"] == "diagnostic_action_type_transition"
    assert summary["causal_status"] == "UNMEASURED"


def test_nonzero_denominator_still_measures_the_rate_and_carries_no_reason() -> None:
    trajectory = {
        "messages": [
            {"role": "assistant", "content": "grep -rn parse_config src/"},
            {"role": "tool", "content": "<gt-evidence>src/pkg.py:12</gt-evidence>"},
            {
                "role": "assistant",
                "content": "Editing.",
                "tool_calls": [{"function": {"arguments": json.dumps(
                    {"command": "sed -i 's/old/new/' src/pkg.py"}
                )}}],
            },
        ]
    }

    summary = analyze_trajectory(trajectory)["summary"]

    assert summary["total_deliveries"] == 1
    assert summary["total_pivots"] == 1
    assert summary["impact_rate"] == 1.0
    assert summary["impact_rate_reason"] is None


def test_zero_delivery_parity_holds_between_the_deep_and_impact_producers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """END-TO-END: both real producers over one trajectory, then the gate's own compare."""
    traj = tmp_path / "mini-swe-agent.trajectory.json"
    traj.write_text(json.dumps(_NO_DELIVERY_TRAJECTORY), encoding="utf-8")
    monkeypatch.setattr(
        gt_deep_metrics, "_find_miniswe_trajectory", lambda _task, _results: str(traj)
    )

    deep = gt_deep_metrics.build("task", str(tmp_path))
    deep_behavioral = deep["behavioral_impact"]

    out = tmp_path / "gt_behavioral_impact_task.json"
    monkeypatch.setattr(
        sys, "argv", ["gt_behavioral_impact.py", str(traj), "--out", str(out)]
    )
    behavioral_main()
    summary = json.loads(out.read_text(encoding="utf-8"))["summary"]

    assert deep_behavioral["total_deliveries"] == 0
    assert deep_behavioral["impact_rate"] is None
    assert summary["impact_rate"] is None
    assert "collection_error" not in deep_behavioral

    # The workflow's exact comparison semantics (yml lines 2581-2586): every deep
    # item must equal the summary value.  None == None counts as parity.
    mismatched = {
        key: (value, summary.get(key))
        for key, value in deep_behavioral.items()
        if summary.get(key) != value
    }
    assert not mismatched, f"behavioral_deep_parity would fail on {mismatched}"

    # The presence tuple the gate enforces (yml lines 2563-2575) must still be satisfied.
    for key in (
        "total_deliveries", "total_pivots", "impact_rate", "per_tag_impact",
        "gt_tokens_injected", "gt_tokens_per_pivot", "nudge_compliance_rate",
    ):
        assert key in summary


def test_gate_parity_leg_is_still_the_any_item_inequality_this_test_models() -> None:
    snippet = _parity_snippet()
    assert "any(summary.get(key) != value" in snippet
    assert "for key, value in deep_behavioral.items()" in snippet


def test_gate_reads_deep_behavioral_from_the_named_artifacts() -> None:
    """Guards the assumption that the two compared sides are the two producers above."""
    doc = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    runs = [
        step.get("run") or ""
        for job in doc.get("jobs", {}).values()
        for step in job.get("steps", []) or []
        if isinstance(step, dict)
    ]
    gate = next(r for r in runs if "behavioral_deep_parity" in r)
    assert "gt_deep_metrics_${GT_TASK_ID}.json" in gate
    assert "gt_behavioral_impact_${GT_TASK_ID}.json" in gate


def test_cli_renders_an_unmeasured_rate_instead_of_raising_on_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The ``:.1%`` format site would TypeError on None — the CLI is a workflow step."""
    traj = tmp_path / "mini-swe-agent.trajectory.json"
    traj.write_text(json.dumps(_NO_DELIVERY_TRAJECTORY), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["gt_behavioral_impact.py", str(traj)])

    behavioral_main()

    stdout = capsys.readouterr().out
    assert "UNMEASURED:zero_denominator_no_gt_deliveries" in stdout
    assert "0.0%" not in stdout
