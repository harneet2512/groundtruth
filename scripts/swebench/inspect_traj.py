#!/usr/bin/env python3
"""Inspect trajectory to see what commands agent ran and if GT fired."""
import json
import sys

traj_path = sys.argv[1]
t = json.load(open(traj_path))
msgs = t.get("messages", t.get("history", []))
print(f"Total messages: {len(msgs)}")
print()

gt_count = 0
edit_count = 0
edit_indicators = ("sed -i", "cat >", "cat <<", "echo >", "echo >>", "tee ", ">>", "patch ")

for i, msg in enumerate(msgs):
    content = msg.get("content", "") or ""
    role = msg.get("role", "?")

    # Check for GT output
    if "GT CODEBASE" in content or "CONNECTED CODE" in content:
        gt_count += 1
        print(f"MSG {i} [{role}]: ** GT OUTPUT FOUND **")
        idx = content.find("GT CODEBASE")
        if idx < 0:
            idx = content.find("CONNECTED CODE")
        print(f"  {content[idx:idx+200]}")
        print()

    # Check for edit commands
    if role == "assistant" and any(ind in content for ind in edit_indicators):
        edit_count += 1

# Show first few assistant messages to understand what commands are used
print(f"\nEdit commands detected: {edit_count}")
print(f"GT fired: {gt_count} times")
print()
print("First 10 assistant messages (commands the agent runs):")
asst_count = 0
for msg in msgs:
    if msg.get("role") != "assistant":
        continue
    content = (msg.get("content", "") or "")[:150]
    if content.strip():
        asst_count += 1
        print(f"  [{asst_count}] {content}")
        if asst_count >= 10:
            break
