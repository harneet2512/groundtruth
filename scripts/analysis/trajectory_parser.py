from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import json
import re

@dataclass
class AgentAction:
    iter: int
    action_type: str  # read_file, edit_file, run_command, think, finish, message
    file_path: Optional[str] = None
    command: Optional[str] = None
    command_exit_code: Optional[int] = None
    edit_function: Optional[str] = None
    timestamp_ms: Optional[int] = None

@dataclass
class AgentTrajectory:
    task_id: str
    run_id: str
    actions: list[AgentAction] = field(default_factory=list)
    total_iterations: int = 0
    final_patch: Optional[str] = None
    resolved: Optional[bool] = None

def parse_openhands_trajectory(output_jsonl_path: str) -> AgentTrajectory:
    """Parse OpenHands output.jsonl into AgentTrajectory."""
    traj = AgentTrajectory(task_id="", run_id="")
    try:
        with open(output_jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                # Extract task_id from instance_id
                if not traj.task_id:
                    traj.task_id = record.get("instance_id", "")
                # Extract actions from history
                history = record.get("history", [])
                for i, entry in enumerate(history):
                    action = entry.get("action", "")
                    args = entry.get("args", {})
                    obs = entry.get("observation", "")
                    obs_extras = entry.get("extras", {})

                    act = AgentAction(iter=i, action_type="unknown")

                    if action == "read":
                        act.action_type = "read_file"
                        act.file_path = args.get("path", "")
                    elif action == "write" or action == "edit":
                        act.action_type = "edit_file"
                        act.file_path = args.get("path", "")
                    elif action == "run":
                        act.action_type = "run_command"
                        act.command = args.get("command", "")
                        act.command_exit_code = obs_extras.get("exit_code")
                    elif action == "think":
                        act.action_type = "think"
                    elif action == "finish":
                        act.action_type = "finish"
                    elif action == "message_action":
                        act.action_type = "message"

                    traj.actions.append(act)

                # Extract patch
                test_result = record.get("test_result", {})
                traj.final_patch = test_result.get("git_patch", "")
                traj.total_iterations = len(traj.actions)
    except Exception:
        pass
    return traj

def extract_edit_function(file_content: str, line_start: int) -> Optional[str]:
    """Find which function contains line_start by scanning upward for def/class."""
    lines = file_content.splitlines()
    for i in range(min(line_start, len(lines)) - 1, -1, -1):
        m = re.match(r'\s*(?:def|class)\s+(\w+)', lines[i])
        if m:
            return m.group(1)
    return None
