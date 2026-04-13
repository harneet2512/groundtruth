#!/bin/bash
# gt_check — Post-edit structural check (removed symbols, stale refs).
# MANDATORY before submission. Also auto-invoked by state command.
#
# Usage: bash /tmp/gt_check.sh <file_path>
#
# Includes diff-hash dedup: skips if patch unchanged since last check.

GT_DIFF_HASH_FILE="/tmp/gt_diff_hash.txt"

source /tmp/_gt_budget.sh 2>/dev/null

if [ -z "$1" ]; then
    echo "Usage: gt_check <file_path>"
    exit 0
fi

# Diff-hash dedup: skip if patch unchanged
current_hash=$(git diff -- "$1" 2>/dev/null | sha256sum | cut -c1-16)
if [ -f "$GT_DIFF_HASH_FILE" ]; then
    last_hash=$(cat "$GT_DIFF_HASH_FILE" 2>/dev/null)
    if [ "$current_hash" = "$last_hash" ] && [ -n "$current_hash" ]; then
        echo "gt_check: patch unchanged since last check (skipped)"
        exit 0
    fi
fi

if ! gt_budget_check check; then
    exit 0
fi

gt_budget_inc check
echo "$current_hash" > "$GT_DIFF_HASH_FILE"
python3 /tmp/gt_tools.py check "$1" 2>/dev/null
