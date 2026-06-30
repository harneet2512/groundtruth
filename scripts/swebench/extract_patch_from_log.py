#!/usr/bin/env python3
"""Extract the agent's patch from trial_output.log.

The agent submits via: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /tmp/patch.txt
The patch appears in the log but mixed with mini-swe-agent interactive prompts.
Strategy: find the LAST contiguous `diff --git` block in the log — that's the
final submitted patch, regardless of surrounding noise.

Usage: python extract_patch_from_log.py <log_file> <output_diff>
"""
import re
import sys

if len(sys.argv) < 3:
    sys.exit("usage: extract_patch_from_log.py <log_file> <output_diff>")

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()

# Find ALL `diff --git` blocks in the log. Take the LAST complete one.
# A diff block starts with `diff --git a/... b/...` and continues through
# `---`, `+++`, `@@`, and +/- lines until the next non-diff line.
diff_pattern = re.compile(
    r'^(diff --git a/.+?)\n'
    r'((?:(?:index |--- |(?:\+\+\+) |@@ |[-+ ]|\\).*\n)*)',
    re.MULTILINE
)

matches = list(diff_pattern.finditer(log))
if not matches:
    # Fallback: try unified diff without git header
    diff_pattern2 = re.compile(
        r'^(--- a/.+?\n\+\+\+ b/.+?\n(?:@@ .+?\n(?:[-+ ].*\n)*))',
        re.MULTILINE
    )
    matches = list(diff_pattern2.finditer(log))

if not matches:
    sys.exit(1)

# Take the last match — that's the final submitted patch
# But actually, there may be multiple diff --git blocks for multiple files
# in the same submission. Find the last CLUSTER of consecutive diff blocks.
# Walk backwards from the last match and collect all contiguous diff blocks.
last_end = matches[-1].end()
patch_parts = []

# Collect all diff blocks that appear near the end (within 500 chars of each other)
for i in range(len(matches) - 1, -1, -1):
    m = matches[i]
    if not patch_parts:
        patch_parts.insert(0, m.group(0))
    elif last_end - m.start() < 50000:  # within 50K chars = same submission
        patch_parts.insert(0, m.group(0))
        last_end = m.end()
    else:
        break

patch = "\n".join(patch_parts).strip()
if len(patch) < 10:
    sys.exit(1)

with open(sys.argv[2], "w") as f:
    f.write(patch + "\n")

print(f"extracted {len(patch_parts)} file(s), {len(patch)} chars")
