#!/usr/bin/env python3
"""Extract the agent's patch from trial_output.log.

The agent submits via: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /tmp/patch.txt
The patch appears in the log AFTER the last COMPLETE_TASK marker, mixed with
mini-swe-agent interactive prompts. Strategy:
1. Find the last COMPLETE_TASK_AND_SUBMIT marker
2. Only look for diff blocks AFTER that marker
3. If no marker found, fall back to last diff cluster in entire log

Usage: python extract_patch_from_log.py <log_file> <output_diff>
"""
import re
import sys

if len(sys.argv) < 3:
    sys.exit("usage: extract_patch_from_log.py <log_file> <output_diff>")

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()

# Step 1: Find the last COMPLETE_TASK marker and only search AFTER it
marker = "COMPLETE_TASK_AND_SUBMIT"
marker_pos = log.rfind(marker)
if marker_pos >= 0:
    search_region = log[marker_pos:]
else:
    search_region = log

# Step 2: Find diff blocks in the search region
diff_pattern = re.compile(
    r'^(diff --git a/.+?)\n'
    r'((?:(?:index |--- |(?:\+\+\+) |@@ |[-+ ]|\\).*\n)*)',
    re.MULTILINE
)

matches = list(diff_pattern.finditer(search_region))
if not matches:
    # Fallback: try unified diff without git header
    diff_pattern2 = re.compile(
        r'^(--- a/.+?\n\+\+\+ b/.+?\n(?:@@ .+?\n(?:[-+ ].*\n)*))',
        re.MULTILINE
    )
    matches = list(diff_pattern2.finditer(search_region))

if not matches:
    # Last resort: search the entire log
    if marker_pos >= 0:
        matches = list(diff_pattern.finditer(log))

if not matches:
    sys.exit(1)

# Step 3: Collect all contiguous diff blocks (they form the multi-file patch)
# Deduplicate by file path — if same file appears twice, keep only the LAST one
seen_files = {}
for m in matches:
    header = m.group(0).split('\n')[0]
    # Extract file path: "diff --git a/path b/path"
    parts = header.split()
    fpath = parts[2] if len(parts) >= 3 else header
    seen_files[fpath] = m.group(0)

patch_parts = list(seen_files.values())
patch = "\n".join(patch_parts).strip()
if len(patch) < 10:
    sys.exit(1)

with open(sys.argv[2], "w") as f:
    f.write(patch + "\n")

print(f"extracted {len(patch_parts)} file(s), {len(patch)} chars")
