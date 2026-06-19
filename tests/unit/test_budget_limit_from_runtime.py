"""TTD: reporter must use the container's own budget limits, not hardcoded ones.

Reproduction: smoke v6 had `run_invalid=2` on both arms from gt_check budget
violations like `gt_check_count:6>3`. But the container's budget state had
`check.limit=20` — the agent was within the runtime limit. The reporter used
its hardcoded `GT_TOOL_LIMITS = {gt_check_count: 3}` instead of reading the
limit from the scraped budget state, creating a false violation.

Old runs masked this because half the tasks had no budget state file (scraper
missed the write window). With periodic summary emission, budget state is
present on every task, exposing the limit mismatch.

Fix: reporter reads `limit` from the budget state's per-tool bucket when
present; falls back to GT_TOOL_LIMITS only when budget state is absent.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTER = REPO_ROOT / "benchmarks" / "swebench" / "vm_bundle" / "gt_canary_report.py"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "swebench"))
import verify_report  # noqa: E402


def _make_task(outdir: Path, iid: str, arm: str, check_count: int,
               budget_limit: int) -> None:
    td = outdir / iid
    td.mkdir(parents=True, exist_ok=True)
    events = [
        {"event": "checkpoint_startup"},
        {"event": "material_edit"},
        {"event": "ack_armed"},
        {"event": "steer_delivered"},
        {"event": "ack_engagement"},
    ]
    with (td / "gt_hook_telemetry.jsonl").open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    (td / "gt_per_task_summary.json").write_text(json.dumps({
        "run_id": "r", "arm": arm, "instance_id": iid,
        "identity_ok": True, "cycle": 10, "within_call_budget": True,
    }))
    (td / "gt_budget.state.json").write_text(json.dumps({
        "scope": f"r__{iid}__{arm}",
        "orient": {"count": 1, "limit": 2, "exhausted": False},
        "lookup": {"count": 0, "limit": 3, "exhausted": False},
        "impact": {"count": 0, "limit": 2, "exhausted": False},
        "check": {"count": check_count, "limit": budget_limit, "exhausted": False},
        "initialized": True,
    }))


def _run_reporter(outdir: Path, arm: str) -> dict:
    cmd = [sys.executable, str(REPORTER),
           "--outdir", str(outdir), "--arm", arm, "--run-id", "r",
           "--max-steps", "150"]
    subprocess.run(cmd, check=False, capture_output=True)
    return json.loads((outdir / "gt_arm_summary.json").read_text())


class TestBudgetLimitFromRuntime:
    def test_check_count_within_runtime_limit_is_not_a_violation(self, tmp_path):
        """If the runtime limit is 20 and the agent called gt_check 6 times,
        that's within budget. Reporter must NOT flag it as a violation."""
        _make_task(tmp_path, "astropy__astropy-12907", "gt-nolsp",
                   check_count=6, budget_limit=20)
        s = _run_reporter(tmp_path, "gt-nolsp")
        assert s.get("gt_budget_violations") == 0, (
            f"check=6 with limit=20 is within budget. "
            f"Reporter should not flag a violation. Got violations={s.get('gt_budget_violations')}"
        )
        assert s.get("run_invalid_count") == 0

    def test_check_count_exceeding_runtime_limit_is_a_violation(self, tmp_path):
        """If runtime limit is 5 and agent called gt_check 8 times, that IS
        a real violation."""
        _make_task(tmp_path, "astropy__astropy-12907", "gt-nolsp",
                   check_count=8, budget_limit=5)
        s = _run_reporter(tmp_path, "gt-nolsp")
        assert s.get("gt_budget_violations") == 1
        assert s.get("run_invalid_count") == 1

    def test_absent_budget_state_falls_back_to_hardcoded_limits(self, tmp_path):
        """When budget state is missing, reporter should fall back to
        GT_TOOL_LIMITS (check=3). Count=5 exceeds 3 → violation."""
        td = tmp_path / "astropy__astropy-12907"
        td.mkdir(parents=True)
        events = [{"event": "checkpoint_startup"}, {"event": "material_edit"},
                  {"event": "ack_armed"}, {"event": "steer_delivered"},
                  {"event": "ack_engagement"}]
        with (td / "gt_hook_telemetry.jsonl").open("w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        (td / "gt_per_task_summary.json").write_text(json.dumps({
            "run_id": "r", "arm": "gt-nolsp", "instance_id": "astropy__astropy-12907",
            "identity_ok": True, "cycle": 10, "within_call_budget": True,
            "gt_check_count": 5,
        }))
        # NO gt_budget.state.json → fallback to GT_TOOL_LIMITS check=3
        s = _run_reporter(tmp_path, "gt-nolsp")
        assert s.get("gt_budget_violations") == 1, (
            "Without budget state, reporter falls back to GT_TOOL_LIMITS. "
            "gt_check=5 > limit=3 should be a violation."
        )

    def test_end_to_end_run_invalid_zero_when_within_runtime_limits(self, tmp_path):
        """Full pipeline: reporter → verify_report. A task with check=6
        and runtime limit=20 should produce run_invalid=0 and must_ok_rate=1.0
        all the way through verify_report."""
        _make_task(tmp_path, "astropy__astropy-12907", "gt-nolsp",
                   check_count=6, budget_limit=20)
        _run_reporter(tmp_path, "gt-nolsp")
        metrics = verify_report.compute(tmp_path)
        assert metrics["raw"]["run_invalid_count"] == 0
        assert metrics["raw"]["must_ok_rate"] >= 0.90
