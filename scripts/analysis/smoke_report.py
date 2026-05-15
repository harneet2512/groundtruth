from __future__ import annotations
import json
import os
from typing import Any
from scripts.analysis.deep_utilization import compute_deep_utilization


def generate_smoke_report(task_dirs: list[str], output_dir: str) -> dict[str, Any]:
    """Generate 5-smoke report from task artifacts with deep utilization."""
    os.makedirs(output_dir, exist_ok=True)
    report: dict[str, Any] = {"tasks": [], "summary": {}}

    for task_dir in task_dirs:
        task_report: dict[str, Any] = {"task_dir": task_dir}

        # Find JSONL files (may have task_id suffix)
        for prefix in ["gt_layer_events", "gt_agent_reactions", "gt_belief_ledger"]:
            matches = [f for f in os.listdir(task_dir) if f.startswith(prefix) and f.endswith(".jsonl")] if os.path.isdir(task_dir) else []
            path = os.path.join(task_dir, matches[0]) if matches else ""
            events = []
            if path and os.path.exists(path):
                with open(path) as fh:
                    for line in fh:
                        try:
                            events.append(json.loads(line))
                        except Exception:
                            pass
            task_report[prefix + "_count"] = len(events)
            if prefix == "gt_layer_events":
                task_report["gt_layer_events_path"] = path

        # Deep utilization per task
        layer_path = task_report.get("gt_layer_events_path", "")
        react_matches = [f for f in os.listdir(task_dir) if f.startswith("gt_agent_reactions") and f.endswith(".jsonl")] if os.path.isdir(task_dir) else []
        belief_matches = [f for f in os.listdir(task_dir) if f.startswith("gt_belief_ledger") and f.endswith(".jsonl")] if os.path.isdir(task_dir) else []
        react_path = os.path.join(task_dir, react_matches[0]) if react_matches else ""
        belief_path = os.path.join(task_dir, belief_matches[0]) if belief_matches else ""

        if layer_path:
            task_report["deep_utilization"] = compute_deep_utilization(layer_path, react_path, belief_path)

        report["tasks"].append(task_report)

    # Write outputs
    with open(os.path.join(output_dir, "layer_utilization_summary.json"), "w") as f:
        json.dump(report, f, indent=2)

    return report
