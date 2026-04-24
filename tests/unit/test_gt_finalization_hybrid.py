from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts.swebench.gt_finalization import load_run_metrics, readiness_status


def _write_run_artifacts(run_dir: Path, summary: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "gt_arm_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with (run_dir / "gt_report.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "run_id",
            "arm",
            "instance_id",
            "cycle",
            "material_edit_count",
            "ack_armed_count",
            "steer_delivered_count",
            "ack_engagement_count",
        ])
        writer.writerow(["run-1", "gt-lsp-hybrid", "task-1", 1, 1, 1, 1, 1])


class TestHybridReadiness:
    def test_hybrid_run_is_not_ready_when_lsp_is_enabled_but_not_ready(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "hybrid_not_ready"
        _write_run_artifacts(
            run_dir,
            {
                "task_count": 1,
                "avg_material_edit": 1.0,
                "ack_armed_total": 1,
                "steer_delivered_total": 1,
                "ack_engagement_total": 1,
                "lsp_enabled": True,
                "lsp_ready": False,
                "lsp_fallback_count": 1,
                "lsp_promotion_count": 0,
                "hybrid_active_before_first_edit": False,
            },
        )

        run = load_run_metrics(run_dir)
        status = readiness_status(run)

        assert status["ready"] is False
        assert "lsp_not_ready" in status["fail_reasons"]
        assert "lsp_degraded" in status["fail_reasons"]
        assert status["lsp_enabled"] is True
        assert status["lsp_ready"] is False

    def test_hybrid_run_is_ready_only_when_lsp_contract_is_satisfied(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "hybrid_ready"
        _write_run_artifacts(
            run_dir,
            {
                "task_count": 1,
                "avg_material_edit": 1.0,
                "ack_armed_total": 1,
                "steer_delivered_total": 1,
                "ack_engagement_total": 1,
                "lsp_enabled": True,
                "lsp_ready": True,
                "lsp_ready_ts": 1713878400,
                "lsp_ready_source": "preflight",
                "lsp_fallback_count": 0,
                "lsp_promotion_count": 1,
                "hybrid_active_before_first_edit": True,
            },
        )

        run = load_run_metrics(run_dir)
        status = readiness_status(run)

        assert status["ready"] is True
        assert status["fail_reasons"] == []
        assert status["lsp_enabled"] is True
        assert status["lsp_ready"] is True
        assert status["lsp_promotion_count"] == 1
