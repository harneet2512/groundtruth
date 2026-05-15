from __future__ import annotations
from typing import Optional
from scripts.analysis.trajectory_parser import AgentAction
from scripts.analysis.test_command_classifier import is_test_command

def classify_behavioral_mode(actions: list[AgentAction], window: int = 5) -> str:
    recent = actions[-window:] if len(actions) >= window else actions
    if not recent:
        return "mixed"

    types = [a.action_type for a in recent]

    if any(a.action_type == "finish" for a in recent[-2:]):
        return "finishing"

    # Stuck loop: same (type, file) 3+ times
    pairs = [(a.action_type, a.file_path) for a in recent]
    for p in set(pairs):
        if pairs.count(p) >= 3:
            return "stuck_loop"

    read_count = types.count("read_file")
    edit_count = types.count("edit_file")
    cmd_count = types.count("run_command")

    if read_count / len(types) > 0.6:
        return "exploring"
    if any(a.action_type == "edit_file" for a in recent[-3:]):
        return "editing"
    test_cmds = sum(1 for a in recent if a.action_type == "run_command" and a.command and is_test_command(a.command))
    if test_cmds / max(cmd_count, 1) > 0.5 and cmd_count >= 2:
        return "testing"
    return "mixed"

def detect_mode_change(actions_before: list[AgentAction], actions_after: list[AgentAction], window: int = 5) -> dict:
    mode_before = classify_behavioral_mode(actions_before, window)
    mode_after = classify_behavioral_mode(actions_after, window)
    return {"mode_before": mode_before, "mode_after": mode_after, "changed": mode_before != mode_after, "actions_in_mode_before": len(actions_before)}

def detect_stuck_loop(actions: list[AgentAction], window: int = 10) -> Optional[dict]:
    recent = actions[-window:]
    pairs = [(a.action_type, a.file_path) for a in recent]
    for p in set(pairs):
        if pairs.count(p) >= 3 and p[0] != "think":
            start_iter = next(a.iter for a in recent if (a.action_type, a.file_path) == p)
            return {"loop_action_type": p[0], "loop_file": p[1], "loop_count": pairs.count(p), "loop_start_iter": start_iter}
    return None
