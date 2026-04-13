#!/bin/bash
# gt_orient — Codebase structure: top dirs, hot symbols, file count.
# Call once at the start of exploration.
#
# Usage: bash /tmp/gt_orient.sh

source /tmp/_gt_budget.sh 2>/dev/null

if ! gt_budget_check orient; then
    exit 0
fi

gt_budget_inc orient
python3 /tmp/gt_tools.py orient 2>/dev/null
