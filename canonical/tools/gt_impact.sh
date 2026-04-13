#!/bin/bash
# gt_impact — Callers at risk if you change this symbol.
# Run before editing a function with external callers.
#
# Usage: bash /tmp/gt_impact.sh <symbol> [file_path]

source /tmp/_gt_budget.sh 2>/dev/null

if [ -z "$1" ]; then
    echo "Usage: gt_impact <symbol> [file_path]"
    exit 0
fi

if ! gt_budget_check impact; then
    exit 0
fi

gt_budget_inc impact
python3 /tmp/gt_tools.py impact "$1" ${2:+"$2"} 2>/dev/null
