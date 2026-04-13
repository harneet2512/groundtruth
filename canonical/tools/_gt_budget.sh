#!/bin/bash
# GT budget enforcement — reads/writes /tmp/gt_budget.json
#
# Usage:
#   source /tmp/_gt_budget.sh
#   gt_budget_check orient   # exits 1 if over budget
#   gt_budget_inc orient     # increments counter
#
# Hard caps: orient=1, lookup=2, impact=2, check=3

GT_BUDGET_FILE="/tmp/gt_budget.json"

# Initialize budget file if missing
if [ ! -f "$GT_BUDGET_FILE" ]; then
    echo '{"orient":0,"lookup":0,"impact":0,"check":0}' > "$GT_BUDGET_FILE"
fi

gt_budget_check() {
    local tool="$1"
    local count
    count=$(python3 -c "
import json, sys
try:
    b = json.load(open('$GT_BUDGET_FILE'))
    print(b.get('$tool', 0))
except Exception:
    print(0)
" 2>/dev/null)

    local limit
    case "$tool" in
        orient) limit=1 ;;
        lookup) limit=2 ;;
        impact) limit=2 ;;
        check)  limit=3 ;;
        *)      limit=99 ;;
    esac

    if [ "${count:-0}" -ge "$limit" ]; then
        echo "GT budget exhausted for $tool ($count/$limit calls used)"
        return 1
    fi
    return 0
}

gt_budget_inc() {
    local tool="$1"
    python3 -c "
import json
try:
    b = json.load(open('$GT_BUDGET_FILE'))
except Exception:
    b = {'orient':0,'lookup':0,'impact':0,'check':0}
b['$tool'] = b.get('$tool', 0) + 1
json.dump(b, open('$GT_BUDGET_FILE', 'w'))
" 2>/dev/null
}
