#!/usr/bin/env python3
"""Extract the agent's patch from trial_output.log.

The agent submits via: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /tmp/patch.txt
The patch content appears in the log after that marker.

Usage: python extract_patch_from_log.py trial_output.log /tmp/pro_patch.diff
"""
import sys

if len(sys.argv) < 3:
    sys.exit("usage: extract_patch_from_log.py <log_file> <output_diff>")

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
marker = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
idx = log.rfind(marker)
if idx < 0:
    sys.exit(1)

after = log[idx + len(marker):]
lines = after.strip().split("\n")
patch_lines = []
in_diff = False
for line in lines:
    if line.startswith("diff --git") or line.startswith("---") or line.startswith("+++") or line.startswith("@@") or line.startswith("+") or line.startswith("-") or line.startswith(" ") or line.startswith("index "):
        in_diff = True
        patch_lines.append(line)
    elif in_diff and line.strip() == "":
        patch_lines.append(line)
    elif in_diff and line.startswith("=== Pro Trial"):
        break
    elif in_diff:
        patch_lines.append(line)

if patch_lines and len("\n".join(patch_lines).strip()) > 10:
    with open(sys.argv[2], "w") as f:
        f.write("\n".join(patch_lines) + "\n")
    print(f"extracted {len(patch_lines)} patch lines")
else:
    sys.exit(1)
