"""Behavior test: gt_canary_report.arm_summary() must roll up every event
that gt_finalization.readiness_status reads.

Why this test exists: the initial hybrid readiness fix closed the lsp keys
but left the same bug pattern elsewhere — arm_summary() never emitted
`steer_delivered_total`, `ack_engagement_total`, `identity_missing_total`,
`infra_contaminated_total`. gt_finalization reads those keys and gets 0,
so the basic-chain gate tripped (no_steer_delivered / no_ack_engagement /
run_invalid) on every run regardless of arm.

This is the "reader ahead of writer" bug class: a gate is introduced that
reads summary keys, but nobody updates the summary emitter. A test that
asserts `arm_summary(rows).get("steer_delivered_total") == N` given
pre-synthesized rows would still pass — the real bug lives in the
events → row → summary pipeline. So this test starts from raw telemetry
events and walks the whole reporter.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTER = REPO_ROOT / "benchmarks" / "swebench" / "vm_bundle" / "gt_canary_report.py"
FINALIZATION = REPO_ROOT / "scripts" / "swebench" / "gt_finalization.py"


def _make_task(outdir: Path, instance_id: str, events: list[dict], arm: str, identity_ok: bool = True) -> Path:
    task = outdir / instance_id
    task.mkdir(parents=True, exist_ok=True)
    with (task / "gt_hook_telemetry.jsonl").open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    (task / "gt_per_task_summary.json").write_text(json.dumps({
        "run_id": "r", "arm": arm, "instance_id": instance_id,
        "identity_ok": identity_ok, "cycle": 10, "within_call_budget": True,
    }))
    # gt_budget.state.json is a MUST-gate requirement — reporter marks
    # budget_state_missing otherwise. Minimal stub satisfies the gate.
    (task / "gt_budget.state.json").write_text(json.dumps({
        "scope": f"r__{instance_id}__{arm}",
        "orient": {"count": 1, "limit": 1, "exhausted": False},
        "lookup": {"count": 0, "limit": 2, "exhausted": False},
        "impact": {"count": 0, "limit": 2, "exhausted": False},
        "check": {"count": 0, "limit": 3, "exhausted": False},
        "initialized": True,
    }))
    return task


def _run_reporter(outdir: Path, arm: str) -> dict:
    cmd = [sys.executable, str(REPORTER),
           "--outdir", str(outdir), "--arm", arm, "--run-id", "r",
           "--max-steps", "150"]
    if arm == "gt-lsp-hybrid":
        cmd.append("--hybrid")
    subprocess.run(cmd, check=False, capture_output=True)
    return json.loads((outdir / "gt_arm_summary.json").read_text())


class TestReporterRollupsBasicChain:
    def test_steer_delivered_and_ack_engagement_are_rolled_up(self, tmp_path):
        """Behavior: events fire in telemetry → summary emits matching totals."""
        events = [
            {"event": "checkpoint_startup"},
            {"event": "material_edit"},
            {"event": "ack_armed"},
            {"event": "steer_delivered"},
            {"event": "ack_engagement"},
            {"event": "material_edit"},
            {"event": "ack_armed"},
            {"event": "steer_delivered"},
            {"event": "ack_engagement"},
        ]
        _make_task(tmp_path, "astropy__astropy-12907", events, "gt-nolsp")
        s = _run_reporter(tmp_path, "gt-nolsp")
        assert s.get("steer_delivered_total") == 2, (
            "steer_delivered events must roll up into steer_delivered_total. "
            f"got summary keys: {sorted(s.keys())}"
        )
        assert s.get("ack_engagement_total") == 2
        assert s.get("ack_armed_total") == 2
        assert s.get("identity_missing_total", -1) == 0

    def test_identity_missing_counts_tasks_with_no_summary(self, tmp_path):
        """Behavior: when a task has no per_task_summary or identity_ok=False,
        identity_missing_total must reflect it so the finalization gate sees
        the real degradation."""
        events = [{"event": "checkpoint_startup"}, {"event": "material_edit"}]
        # Task 1: identity OK
        _make_task(tmp_path, "astropy__astropy-12907", events, "gt-nolsp", identity_ok=True)
        # Task 2: identity explicitly broken
        _make_task(tmp_path, "astropy__astropy-13033", events, "gt-nolsp", identity_ok=False)
        # Task 3: no per_task_summary file at all
        task3 = tmp_path / "astropy__astropy-13236"
        task3.mkdir()
        with (task3 / "gt_hook_telemetry.jsonl").open("w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        s = _run_reporter(tmp_path, "gt-nolsp")
        # Two tasks (id=False + no summary) should count as identity_missing
        assert s.get("identity_missing_total") == 2, (
            f"expected 2 identity_missing; got summary: "
            f"{ {k: s[k] for k in sorted(s) if 'identity' in k} }"
        )


class TestReporterToFinalizationGate:
    """End-to-end: healthy events → reporter → finalization → ready=True."""

    def _run_gate(self, outdir: Path) -> dict:
        """Load gt_finalization at runtime and compute readiness."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("gt_finalization", FINALIZATION)
        mod = importlib.util.module_from_spec(spec)
        # Register in sys.modules so @dataclass resolves cls.__module__
        sys.modules["gt_finalization"] = mod
        spec.loader.exec_module(mod)
        run = mod.load_run_metrics(outdir)
        return mod.readiness_status(run)

    def test_healthy_nolsp_run_passes_full_gate(self, tmp_path):
        """The only thing stopping this from returning ready=True is a real
        regression in either reporter rollups or finalization gate logic."""
        events = [
            {"event": "checkpoint_startup"},
            {"event": "material_edit"},
            {"event": "ack_armed"},
            {"event": "steer_delivered"},
            {"event": "ack_engagement"},
        ]
        _make_task(tmp_path, "astropy__astropy-12907", events, "gt-nolsp")
        _run_reporter(tmp_path, "gt-nolsp")
        status = self._run_gate(tmp_path)
        assert status["ready"] is True, (
            f"healthy nolsp run must pass readiness gate. fail_reasons={status.get('fail_reasons')!r}"
        )
        assert status["fail_reasons"] == []

    def test_healthy_lsp_hybrid_run_passes_full_gate(self, tmp_path):
        events = [
            {"event": "lsp_config", "lsp_enabled": 1},
            {"event": "checkpoint_startup"},
            {"event": "material_edit"},
            {"event": "ack_armed"},
            {"event": "steer_delivered"},
            {"event": "ack_engagement"},
            {"event": "lsp_promotion_noop"},
        ]
        _make_task(tmp_path, "astropy__astropy-12907", events, "gt-lsp-hybrid")
        _run_reporter(tmp_path, "gt-lsp-hybrid")
        status = self._run_gate(tmp_path)
        assert status["ready"] is True, (
            f"healthy lsp-hybrid run must pass readiness gate. fail_reasons={status.get('fail_reasons')!r}"
        )
        assert status["lsp_enabled"] is True
        assert status["lsp_ready"] is True

    def test_failed_lsp_promotion_trips_hybrid_gate(self, tmp_path):
        """Negative: lsp_promotion_failed must degrade the hybrid gate."""
        events = [
            {"event": "lsp_config", "lsp_enabled": 1},
            {"event": "checkpoint_startup"},
            {"event": "material_edit"},
            {"event": "ack_armed"},
            {"event": "steer_delivered"},
            {"event": "ack_engagement"},
            {"event": "lsp_promotion_failed", "error": "pyright_crash"},
        ]
        _make_task(tmp_path, "astropy__astropy-12907", events, "gt-lsp-hybrid")
        _run_reporter(tmp_path, "gt-lsp-hybrid")
        status = self._run_gate(tmp_path)
        assert status["ready"] is False
        assert "lsp_not_ready" in status["fail_reasons"] or "lsp_degraded" in status["fail_reasons"]

    def test_nolsp_run_is_not_tripped_by_hybrid_block(self, tmp_path):
        """Regression guard: the hybrid-specific reasons must not fire on a nolsp run."""
        events = [
            {"event": "checkpoint_startup"},
            {"event": "material_edit"},
            {"event": "ack_armed"},
            {"event": "steer_delivered"},
            {"event": "ack_engagement"},
        ]
        _make_task(tmp_path, "astropy__astropy-12907", events, "gt-nolsp")
        _run_reporter(tmp_path, "gt-nolsp")
        status = self._run_gate(tmp_path)
        for hybrid_reason in ("lsp_not_ready", "lsp_degraded", "hybrid_started_late"):
            assert hybrid_reason not in status["fail_reasons"], (
                f"{hybrid_reason} must not appear on nolsp runs"
            )
