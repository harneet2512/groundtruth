#!/usr/bin/env python3
"""Extract the agent's patch from trial_output.log.

The agent submits via: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /tmp/patch.txt
The patch appears after the last COMPLETE_TASK marker, inside the "Exit:" block
of mini-swe-agent's output. Strategy:
1. Find the last COMPLETE_TASK_AND_SUBMIT marker
2. Find "Exit:" after it — everything from there to the end is the patch content
3. Write it as-is (git apply is lenient about trailing content)

Usage: python extract_patch_from_log.py <log_file> <output_diff>
"""
import sys

if len(sys.argv) < 3:
    sys.exit("usage: extract_patch_from_log.py <log_file> <output_diff>")

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()

# Find the last COMPLETE_TASK marker
marker = "COMPLETE_TASK_AND_SUBMIT"
marker_pos = log.rfind(marker)
if marker_pos < 0:
    sys.exit(1)

region = log[marker_pos:]

# Find "Exit:" after the marker — the patch starts there
exit_markers = ["\nExit:\n", "\nExit:\r\n"]
exit_pos = -1
for em in exit_markers:
    p = region.find(em)
    if p >= 0:
        exit_pos = p + len(em)
        break

if exit_pos < 0:
    # No "Exit:" — try to find first "diff --git" after marker
    dg = region.find("diff --git")
    if dg >= 0:
        exit_pos = dg
    else:
        sys.exit(1)

raw_patch = region[exit_pos:]

# Find the first "diff --git" in the raw content
diff_start = raw_patch.find("diff --git")
if diff_start < 0:
    sys.exit(1)

raw_patch = raw_patch[diff_start:]

# Trim trailing non-patch content: stop at known terminal markers
terminal_markers = [
    "\nSubmit message:",
    "\n=== Pro Trial",
    "\nEVAL_DEFERRED:",
    "\nPRO_EVAL_",
    "\nGT_RUN_PROOF",
    "\n[MEMLOG ",
    "\n[GT_DEEP]",
    "\n[RESMON]",
    "\nbash: line",
]
for tm in terminal_markers:
    pos = raw_patch.find(tm)
    if pos > 0:
        raw_patch = raw_patch[:pos]

patch = raw_patch.strip()
if len(patch) < 10:
    sys.exit(1)

files = patch.count("diff --git")

with open(sys.argv[2], "w") as f:
    f.write(patch + "\n")

print(f"extracted {files} file(s), {len(patch)} chars")
