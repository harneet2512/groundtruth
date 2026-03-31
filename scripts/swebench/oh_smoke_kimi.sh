#!/bin/bash
set -euo pipefail

# Smoke test: Kimi K2-Thinking + GT on 3 tasks
# Verifies: model works, GT tools are called, patches are generated
# Usage: bash oh_smoke_kimi.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SMOKE_DIR="$HOME/results/kimi-smoke"

echo "================================================"
echo "  SMOKE TEST: Kimi K2-Thinking + GroundTruth"
echo "================================================"
echo ""

# Step 1: Pick 3 diverse SWE-bench Verified tasks
# (one easy, one medium, one that benefits from codebase awareness)
cat > /tmp/kimi_smoke_instances.txt << 'EOF'
django__django-16379
scikit-learn__scikit-learn-25747
sympy__sympy-24152
EOF
echo "Selected 3 smoke test tasks:"
cat /tmp/kimi_smoke_instances.txt
echo ""

# Step 2: Run baseline (no GT) on these 3 tasks
echo "=== Phase 1: Baseline (no GT) ==="
mkdir -p "$SMOKE_DIR/baseline"
bash "$SCRIPT_DIR/oh_run_kimi_baseline.sh" \
    --select /tmp/kimi_smoke_instances.txt \
    --num-workers 1 \
    --output-dir "$SMOKE_DIR/baseline" \
    2>&1 | tee "$SMOKE_DIR/baseline.log"

echo ""
echo "=== Phase 2: With GT ==="
mkdir -p "$SMOKE_DIR/gt"
bash "$SCRIPT_DIR/oh_run_kimi_gt.sh" \
    --select /tmp/kimi_smoke_instances.txt \
    --num-workers 1 \
    --output-dir "$SMOKE_DIR/gt" \
    2>&1 | tee "$SMOKE_DIR/gt.log"

echo ""
echo "================================================"
echo "  SMOKE TEST RESULTS"
echo "================================================"
echo ""

# Step 3: Check GT tool utilization
echo "--- GT Tool Utilization ---"
if [ -f "$SMOKE_DIR/gt/output.jsonl" ]; then
    # Count how many tasks actually called GT tools
    GT_CALLS=$(grep -c "groundtruth_check\|groundtruth_impact\|groundtruth_references\|gt_tool.py" "$SMOKE_DIR/gt/output.jsonl" 2>/dev/null || echo "0")
    TOTAL_TASKS=$(wc -l < "$SMOKE_DIR/gt/output.jsonl")
    echo "Tasks completed (GT): $TOTAL_TASKS"
    echo "Tasks with GT tool calls: $GT_CALLS"

    # Extract GT tool calls from trajectories
    echo ""
    echo "--- GT Call Details ---"
    grep -oP '(groundtruth_check|groundtruth_impact|groundtruth_references|gt_tool\.py\s+\w+)' \
        "$SMOKE_DIR/gt/output.jsonl" 2>/dev/null | sort | uniq -c | sort -rn || echo "No GT calls found in output"
else
    echo "WARNING: No GT output found at $SMOKE_DIR/gt/output.jsonl"
fi

echo ""
echo "--- Baseline Results ---"
if [ -f "$SMOKE_DIR/baseline/output.jsonl" ]; then
    echo "Tasks completed (baseline): $(wc -l < "$SMOKE_DIR/baseline/output.jsonl")"
else
    echo "WARNING: No baseline output found"
fi

echo ""
echo "--- Token Usage Estimate ---"
# Check litellm logs for token counts
if [ -f /tmp/litellm.log ]; then
    echo "Total API calls: $(grep -c '"POST /v1/chat/completions"' /tmp/litellm.log 2>/dev/null || echo 'unknown')"
    echo "Check /tmp/litellm.log for detailed token usage"
fi

echo ""
echo "--- Quick Comparison ---"
echo "Review trajectories at:"
echo "  Baseline: $SMOKE_DIR/baseline/"
echo "  GT:       $SMOKE_DIR/gt/"
echo ""
echo "Next step: If GT tools were utilized, run 50-task canary:"
echo "  bash $SCRIPT_DIR/oh_run_kimi_gt.sh --select <50_tasks.txt>"
echo ""
echo "Smoke test complete: $(date -u) UTC"
