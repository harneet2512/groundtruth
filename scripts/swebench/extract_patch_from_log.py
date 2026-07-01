#!/usr/bin/env python3
"""Extract the agent's patch from trial_output.log.

Strategy: grab raw content between Exit: and terminal markers, then
strip trailing non-diff lines from the bottom up.

Usage: python extract_patch_from_log.py <log_file> <output_diff>
"""
import sys

if len(sys.argv) < 3:
    sys.exit("usage: extract_patch_from_log.py <log_file> <output_diff>")

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()

marker = "COMPLETE_TASK_AND_SUBMIT"
marker_pos = log.rfind(marker)
if marker_pos < 0:
    sys.exit(1)

region = log[marker_pos:]

for em in ["\nExit:\n", "\nExit:\r\n"]:
    p = region.find(em)
    if p >= 0:
        region = region[p + len(em):]
        break

diff_start = region.find("diff --git")
if diff_start < 0:
    sys.exit(1)

raw = region[diff_start:]

# Strip from known terminal markers
terminal_markers = [
    "\nSaved trajectory",
    "\nAgent wants to finish",
    "\n=== Pro Trial",
    "\nEVAL_DEFERRED:",
    "\nPRO_EVAL_",
    "\nGT_RUN_PROOF",
    "\n[MEMLOG ",
    "\n[GT_DEEP]",
    "\n[RESMON]",
    "\nbash: line",
    "\nSubmit message:",
]
for tm in terminal_markers:
    pos = raw.find(tm)
    if pos > 0:
        raw = raw[:pos]

# Strip trailing non-diff lines from the bottom.
# A valid diff line starts with: diff, index, ---, +++, @@, +, -, space, \,
# new/old/deleted/rename/copy/Binary/similarity, or is blank.
# Everything else at the tail is noise.
lines = raw.split('\n')
while lines:
    last = lines[-1]
    if last.strip() == '':
        lines.pop()
        continue
    if (last.startswith('diff --git') or last.startswith('index ') or
        last.startswith('--- ') or last.startswith('+++ ') or
        last.startswith('@@ ') or last.startswith('+') or
        last.startswith('-') or last.startswith(' ') or
        last.startswith('\\') or last.startswith('new ') or
        last.startswith('old ') or last.startswith('deleted ') or
        last.startswith('rename') or last.startswith('similarity') or
        last.startswith('Binary')):
        break
    lines.pop()

patch = '\n'.join(lines)
if len(patch) < 10:
    sys.exit(1)

files = sum(1 for l in lines if l.startswith('diff --git'))

with open(sys.argv[2], "w") as f:
    f.write(patch + "\n")

print(f"extracted {files} file(s), {len(patch)} chars")
