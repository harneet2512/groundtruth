from __future__ import annotations
import json
import os
from typing import Any

def generate_smoke_report(task_dirs: list[str], output_dir: str) -> dict[str, Any]:
    """Generate 5-smoke report from task artifacts."""
    os.makedirs(output_dir, exist_ok=True)
    report: dict[str, Any] = {"tasks": [], "summary": {}}

    for task_dir in task_dirs:
        task_report: dict[str, Any] = {"task_dir": task_dir}

        # Read layer events
        for f in ["gt_layer_events", "gt_agent_reactions", "gt_belief_ledger"]:
            path = os.path.join(task_dir, f"{f}.jsonl")  # task_id appended at runtime
            events = []
            if os.path.exists(path):
                with open(path) as fh:
                    for line in fh:
                        try: events.append(json.loads(line))
                        except: pass
            task_report[f] = len(events)

        report["tasks"].append(task_report)

    # Write outputs
    with open(os.path.join(output_dir, "layer_utilization_summary.json"), "w") as f:
        json.dump(report, f, indent=2)

    return report
