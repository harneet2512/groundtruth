"""Replay a REAL frozen trajectory through the Goku governor.

Proves P0 hooks fire on actual agent behavior, not just unit test mocks.
Uses tmp_oh_1task_output.jsonl (delgan__loguru-1297).
"""

from __future__ import annotations

import json
import os
import sys

from groundtruth.trajectory.state import L5TrajectoryState, IterationBand
from groundtruth.trajectory.governor import L5Governor
from groundtruth.trajectory import hooks
from groundtruth.trajectory.event_classifier import classify_file_kind, classify_check_kind


def replay_trajectory(trajectory_path: str) -> dict:
    """Replay a frozen trajectory through the Goku governor, return fire counts."""
    os.environ["GT_L5_GOKU_EVENTS"] = "1"

    with open(trajectory_path, "r", encoding="utf-8", errors="replace") as f:
        data = json.loads(f.readline())

    history = data.get("history", [])
    instance_id = data.get("instance_id", "unknown")

    gov = L5Governor.__new__(L5Governor)
    gov.state = L5TrajectoryState(instance_id=instance_id, max_iter=100)
    gov.state._initialized = True
    gov.state._prev_iter = 0
    gov._log_entries = []

    actions = []
    for entry in history:
        action = entry.get("action", "")
        args = entry.get("args", {})
        if action in ("run", "read", "write", "edit", "finish"):
            actions.append({
                "action": action,
                "path": args.get("path", ""),
                "command": args.get("command", ""),
            })

    fires = {}
    all_events = []

    for i, act in enumerate(actions):
        path = act["path"]
        command = act["command"]
        is_finish = act["action"] == "finish"

        file_kind = classify_file_kind(path) if path else "UNKNOWN_FILE"

        cls_name = {
            "edit": "FileEditAction", "write": "FileWriteAction",
            "run": "CmdRunAction", "read": "FileReadAction", "finish": "AgentFinishAction",
        }.get(act["action"], "Unknown")

        _cls = type(cls_name, (), {
            "command": command, "path": path, "content": command,
        })
        fa = _cls()

        _obs_cls = type("Observation", (), {"content": "", "stdout": ""})
        obs_mock = _obs_cls()

        decision = gov.goku_check(
            fa, obs_mock, i, 100,
            file_path=path or None,
            diff_size=None,
        )

        event = {
            "iter": i,
            "action": act["action"],
            "path": path,
            "command": command[:60],
            "file_kind": file_kind,
            "goku_fired": decision.fired,
            "goku_suppressed": decision.suppressed,
            "goku_hook": decision.hook_name if decision.fired else None,
            "goku_reason": decision.suppression_reason if decision.suppressed else None,
        }
        all_events.append(event)

        if decision.fired:
            hook = decision.hook_name
            fires[hook] = fires.get(hook, 0) + 1
            status = "SUPPRESSED" if decision.suppressed else "EMITTED"
            print(f"  [{status}] iter={i} hook={hook} reason={decision.suppression_reason or decision.trigger_reason}")

    return {
        "instance_id": instance_id,
        "total_actions": len(actions),
        "fires": fires,
        "total_fires": sum(fires.values()),
        "total_emitted": sum(1 for e in all_events if e["goku_fired"] and not e.get("goku_suppressed")),
        "total_suppressed": sum(1 for e in all_events if e.get("goku_suppressed")),
        "state": {
            "edited_source_files": gov.state.edited_source_files,
            "verification_commands_run": gov.state.verification_commands_run,
            "l5_emissions_by_type": gov.state.l5_emissions_by_type,
            "structural_witness_followed": gov.state.structural_witness_followed,
            "patch_collapsed": gov.state.patch_collapsed,
        },
    }


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "tmp_oh_1task_output.jsonl"
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        sys.exit(1)

    print(f"Replaying {path} through Goku governor...")
    result = replay_trajectory(path)
    print()
    print(f"Instance: {result['instance_id']}")
    print(f"Actions: {result['total_actions']}")
    print(f"Goku fires: {result['total_fires']} (emitted={result['total_emitted']}, suppressed={result['total_suppressed']})")
    print(f"Fires by hook: {json.dumps(result['fires'], indent=2)}")
    print(f"State: {json.dumps(result['state'], indent=2)}")
