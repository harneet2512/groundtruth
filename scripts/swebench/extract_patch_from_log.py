#!/usr/bin/env python3
"""Extract the agent's patch from trial_output.log.

The agent submits via: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /tmp/patch.txt
Strategy:
1. Find the last COMPLETE_TASK_AND_SUBMIT marker
2. Find all text after "Exit:" (the actual command output)
3. Extract diff content from that region

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

# The actual patch appears after "Exit:" in the mini-swe-agent output
# Find the last "Exit:" after the marker
exit_pos = region.rfind("\nExit:\n")
if exit_pos >= 0:
    patch_region = region[exit_pos + len("\nExit:\n"):]
else:
    # Fallback: use everything after marker
    patch_region = region

# The patch_region contains the raw diff output, possibly with line wrapping.
# Extract everything from the first "diff --git" to the end of diff content.
# We need to be lenient because terminal wrapping can break lines.

# Find first "diff --git" in the region
diff_start = patch_region.find("diff --git")
if diff_start < 0:
    # Try in the full post-marker region
    diff_start = region.find("diff --git")
    if diff_start >= 0:
        patch_region = region[diff_start:]
    else:
        sys.exit(1)
else:
    patch_region = patch_region[diff_start:]

# Now collect all lines that are part of the diff.
# Stop at lines that are clearly NOT diff content.
lines = patch_region.split('\n')
patch_lines = []
in_diff = False
for line in lines:
    # Diff content lines
    if (line.startswith('diff --git ') or
        line.startswith('index ') or
        line.startswith('--- ') or
        line.startswith('+++ ') or
        line.startswith('@@ ') or
        line.startswith('+') or
        line.startswith('-') or
        line.startswith(' ') or
        line.startswith('\\') or
        # Handle wrapped "diff --git" lines (b/path on next line)
        (patch_lines and patch_lines[-1].startswith('diff --git a/') and line.startswith('b/')) or
        # Handle wrapped index lines
        (patch_lines and patch_lines[-1].startswith('index ') and '..' in line) or
        # Blank line within diff (between hunks or files)
        line == ''):

        if line.startswith('diff --git '):
            in_diff = True

        if in_diff:
            # Join wrapped diff --git header lines
            if patch_lines and patch_lines[-1].startswith('diff --git a/') and line.startswith('b/'):
                patch_lines[-1] = patch_lines[-1].rstrip() + ' ' + line
            else:
                patch_lines.append(line)
    else:
        # Non-diff line — if we've seen diff content, stop
        if in_diff and patch_lines:
            # Allow a few non-matching blank lines but stop at real content
            if line.strip() == '':
                continue
            break

# Remove trailing blank lines
while patch_lines and patch_lines[-1].strip() == '':
    patch_lines.pop()

patch = '\n'.join(patch_lines)
if len(patch) < 10:
    sys.exit(1)

# Count files
files = sum(1 for l in patch_lines if l.startswith('diff --git '))

with open(sys.argv[2], "w") as f:
    f.write(patch + "\n")

print(f"extracted {files} file(s), {len(patch)} chars")
