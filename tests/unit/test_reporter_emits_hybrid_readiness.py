"""Behavior test: given a task dir with telemetry representing a healthy
lsp-hybrid run, the reporter's gt_arm_summary.json must carry hybrid
readiness signal with correct values.

Why this test exists: commit 0c07ba7 added a hybrid readiness gate in
gt_finalization.readiness_status that reads `lsp_ready`, `lsp_fallback_count`,
`hybrid_active_before_first_edit`. But the reporter (gt_canary_report.py)
did not emit those keys — the gate was dormant on real runs. A test that
asserted `arm_summary(mock_rows).get("lsp_ready") == True` passed with the
broken code because it mirrored the implementation. The bug only surfaced
when I walked the real pipeline manually.

This test starts from raw event JSONL (the actual input the reporter reads),
runs the real reporter script as a subprocess, and inspects the JSON file
the reporter writes. It cannot be made green by editing the test; the
reporter must produce real output.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTER = REPO_ROOT / "benchmarks" / "swebench" / "vm_bundle" / "gt_canary_report.py"


def _write_task_dir(outdir: Path, instance_id: str, events: list[dict], arm: str) -> Path:
    task_dir = outdir / instance_id
    task_dir.mkdir(parents=True, exist_ok=True)
    with (task_dir / "gt_hook_telemetry.jsonl").open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    # Minimal per-task summary — reporter consumes identity_ok from here
    (task_dir / "gt_per_task_summary.json").write_text(
        json.dumps({
            "run_id": "r-test",
            "arm": arm,
            "instance_id": instance_id,
            "identity_ok": True,
            "cycle": 10,
            "within_call_budget": True,
        })
    )
    return task_dir


def _run_reporter(outdir: Path, arm: str) -> dict:
    cmd = [
        sys.executable, str(REPORTER),
        "--outdir", str(outdir),
        "--arm", arm,
        "--run-id", "r-test",
        "--max-steps", "150",
    ]
    if arm == "gt-lsp-hybrid":
        cmd.append("--hybrid")
    # Non-zero exit is OK — the reporter exits non-zero on CANARY_VERIFY fails,
    # but the JSON file is still produced and that is what we assert on.
    subprocess.run(cmd, check=False, capture_output=True)
    summary_path = outdir / "gt_arm_summary.json"
    assert summary_path.exists(), f"reporter did not write {summary_path}"
    return json.loads(summary_path.read_text())


class TestReporterEmitsHybridReadiness:
    def test_healthy_lsp_hybrid_run_reports_lsp_ready_true(self, tmp_path: Path):
        events = [
            {"event": "startup_enter"},
            {"event": "lsp_config", "lsp_enabled": 1},
            {"event": "checkpoint_startup"},
            {"event": "material_edit", "files": ["src/foo.py"]},
            {"event": "ack_armed", "channel": "material_edit"},
            {"event": "steer_delivered"},
            {"event": "ack_engagement"},
            {"event": "lsp_promotion_succeeded", "verified": 2},
            {"event": "lsp_promotion_noop"},
        ]
        _write_task_dir(tmp_path, "astropy__astropy-12907", events, "gt-lsp-hybrid")
        summary = _run_reporter(tmp_path, "gt-lsp-hybrid")

        # These are the load-bearing keys gt_finalization.readiness_status reads.
        # If the reporter stops emitting any of them, the gate silently goes
        # dormant. Assert each one by name with its expected value.
        assert summary.get("lsp_enabled") is True, (
            "lsp_enabled missing — gate will be dormant. Summary: "
            f"{list(summary.keys())}"
        )
        assert summary.get("lsp_ready") is True, (
            "lsp_ready must be True on a successful promotion with no failures"
        )
        assert summary.get("lsp_fallback_count", -1) == 0
        assert summary.get("hybrid_active_before_first_edit") is True
        # The per-outcome totals must also be present so operators can bisect
        assert summary.get("lsp_promotion_succeeded_total", 0) >= 1
        assert summary.get("lsp_promotion_noop_total", 0) >= 1
        assert summary.get("lsp_promotion_failed_total", -1) == 0

    def test_failed_promotion_reports_lsp_ready_false(self, tmp_path: Path):
        """Behavior: a failed promotion must degrade lsp_ready and set fallback_count > 0."""
        events = [
            {"event": "lsp_config", "lsp_enabled": 1},
            {"event": "checkpoint_startup"},
            {"event": "material_edit"},
            {"event": "ack_armed", "channel": "material_edit"},
            {"event": "steer_delivered"},
            {"event": "ack_engagement"},
            {"event": "lsp_promotion_failed", "error": "pyright_crash"},
        ]
        _write_task_dir(tmp_path, "astropy__astropy-12907", events, "gt-lsp-hybrid")
        summary = _run_reporter(tmp_path, "gt-lsp-hybrid")

        assert summary.get("lsp_enabled") is True
        assert summary.get("lsp_ready") is False, (
            "lsp_ready must be False when any promotion failed"
        )
        assert summary.get("lsp_fallback_count", 0) >= 1
        assert summary.get("hybrid_active_before_first_edit") is False

    def test_nolsp_arm_reports_lsp_enabled_false_and_ready_false(self, tmp_path: Path):
        """Behavior: a nolsp run must not trip the hybrid block (gate skipped)."""
        events = [
            {"event": "checkpoint_startup"},
            {"event": "material_edit"},
            {"event": "ack_armed"},
            {"event": "steer_delivered"},
            {"event": "ack_engagement"},
        ]
        _write_task_dir(tmp_path, "astropy__astropy-12907", events, "gt-nolsp")
        summary = _run_reporter(tmp_path, "gt-nolsp")

        assert summary.get("lsp_enabled") is False
        # lsp_ready must be False for nolsp (no promotions) — but
        # gt_finalization only trips the hybrid block when lsp_enabled=True,
        # so this False does not fail readiness for the nolsp arm.
        assert summary.get("lsp_ready") is False
        assert summary.get("lsp_fallback_count", -1) == 0

    def test_empty_telemetry_does_not_crash_and_reports_lsp_enabled_false(self, tmp_path: Path):
        """Behavior: missing/empty telemetry file → lsp_enabled=False, no crash."""
        task_dir = tmp_path / "astropy__astropy-12907"
        task_dir.mkdir()
        (task_dir / "gt_hook_telemetry.jsonl").write_text("")
        (task_dir / "gt_per_task_summary.json").write_text(json.dumps({
            "run_id": "r", "arm": "gt-lsp-hybrid", "instance_id": "astropy__astropy-12907",
            "identity_ok": True, "cycle": 1, "within_call_budget": True,
        }))
        summary = _run_reporter(tmp_path, "gt-lsp-hybrid")
        assert summary.get("lsp_enabled") is False
        assert summary.get("lsp_ready") is False
