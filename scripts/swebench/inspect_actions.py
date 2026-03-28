#!/usr/bin/env python3
"""Extract actual bash commands from mini-swe-agent trajectory."""
import json
import sys

traj_path = sys.argv[1]
t = json.load(open(traj_path))
msgs = t.get("messages", t.get("history", []))

for i, msg in enumerate(msgs):
    extra = msg.get("extra", {})
    actions = extra.get("actions", [])
    for action in actions:
        cmd = action.get("command", "")
        if cmd:
            # Truncate long commands
            display = cmd if len(cmd) < 300 else cmd[:300] + "..."
            # Highlight edit commands
            is_edit = any(ind in cmd for ind in (
                "sed -i", "cat >", "cat <<", "echo >", "echo >>",
                "tee ", "patch ", "python3 -c", "python -c",
                "str_replace", "> /", ">>", "write_to_file",
            ))
            marker = " ** EDIT **" if is_edit else ""
            print(f"MSG {i}{marker}: {display}")
