#!/usr/bin/env python3
"""Find the actual bash commands the agent ran, especially file edits."""
import json
import sys

traj_path = sys.argv[1]
t = json.load(open(traj_path))
msgs = t.get("messages", t.get("history", []))

# mini-swe-agent format: alternating assistant (thought+command) and user (observation) messages
# The command is embedded in the assistant message, often in a code block
edit_indicators = ("sed -i", "cat >", "cat <<", "echo >", "echo >>",
                   "tee ", "patch ", "str_replace", "create_file", "edit_file")

print("ALL COMMANDS (looking for file modifications):")
print()
for i, msg in enumerate(msgs):
    content = (msg.get("content", "") or "")
    role = msg.get("role", "?")

    # In mini-swe-agent, commands are in bash code blocks in assistant messages
    if role == "assistant":
        # Look for bash code blocks
        import re
        code_blocks = re.findall(r'```(?:bash|sh)?\n(.*?)```', content, re.DOTALL)
        for block in code_blocks:
            for line in block.strip().split("\n"):
                line = line.strip()
                if any(ind in line for ind in edit_indicators):
                    print(f"  MSG {i}: {line[:200]}")

        # Also check for inline commands
        if any(ind in content for ind in edit_indicators):
            # Find the line with the indicator
            for line in content.split("\n"):
                if any(ind in line for ind in edit_indicators):
                    print(f"  MSG {i} (inline): {line.strip()[:200]}")
