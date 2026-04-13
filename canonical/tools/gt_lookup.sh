#!/bin/bash
# gt_lookup — Symbol definition, callers, and test references.
#
# Usage: bash /tmp/gt_lookup.sh <symbol> [file_path]

source /tmp/_gt_budget.sh 2>/dev/null

if [ -z "$1" ]; then
    echo "Usage: gt_lookup <symbol> [file_path]"
    exit 0
fi

if ! gt_budget_check lookup; then
    exit 0
fi

gt_budget_inc lookup
python3 /tmp/gt_tools.py lookup "$1" ${2:+"$2"} 2>/dev/null
