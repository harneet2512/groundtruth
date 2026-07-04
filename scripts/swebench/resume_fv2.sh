#!/bin/bash
set -euo pipefail

REPO_DIR="$HOME/groundtruth"
RESULTS_DIR="$HOME/foundation_v2"

echo "============================================"
echo "  Foundation v2 — RESUME Experiment"
echo "  Started: $(date -u) UTC"
echo "============================================"

# Ensure proxy is running
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running."
    exit 1
fi
echo "Proxy: OK"

# --- Phase 1: Finish Condition A (remaining 61 tasks) ---
REMAINING="$RESULTS_DIR/manifests/remaining_tasks.txt"
REMAINING_COUNT=$(wc -l < "$REMAINING")
echo ""
echo "============================================"
echo "  CONDITION A: Baseline — RESUME ($REMAINING_COUNT remaining)"
echo "  Started: $(date -u) UTC"
echo "============================================"

INSTANCES=$(paste -sd ',' "$REMAINING")

bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_baseline.sh" \
    --instances "$INSTANCES" \
    --output-dir "$RESULTS_DIR/condition_a" \
    --max-iterations 100 2>&1 | tee -a "$RESULTS_DIR/condition_a/run_resume.log"

echo ""
echo "Condition A resume complete: $(date -u) UTC"

# Merge outputs if needed (OpenHands appends to output.jsonl)
OUTPUT_DIR=$(find "$RESULTS_DIR/condition_a" -name "output.jsonl" -exec dirname {} \; | head -1)
if [ -n "$OUTPUT_DIR" ]; then
    TOTAL=$(wc -l < "$OUTPUT_DIR/output.jsonl")
    echo "Condition A total tasks: $TOTAL"
fi

# Disk cleanup between conditions
echo ""
echo "=== Cleaning up between conditions ==="
docker container prune -f 2>/dev/null | tail -1
# Remove images not needed by condition_b (they'll be re-pulled)
docker image prune -a -f 2>/dev/null | tail -1
echo "Disk after cleanup: $(df -h / | tail -1)"

# --- Phase 2: Run Condition B (all 100 tasks) ---
echo ""
echo "============================================"
echo "  CONDITION B: GT Phase 3 (100 tasks)"
echo "  Started: $(date -u) UTC"
echo "============================================"
mkdir -p "$RESULTS_DIR/condition_b"

ALL_INSTANCES=$(paste -sd ',' "$RESULTS_DIR/manifests/hundred_tasks.txt")

bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh" \
    --instances "$ALL_INSTANCES" \
    --output-dir "$RESULTS_DIR/condition_b" \
    --max-iterations 100 2>&1 | tee "$RESULTS_DIR/condition_b/run.log"

echo ""
echo "Condition B complete: $(date -u) UTC"

# --- Summary ---
echo ""
echo "============================================"
echo "  EXPERIMENT COMPLETE"
echo "  Finished: $(date -u) UTC"
echo "============================================"

for cond in condition_a condition_b; do
    output_file=$(find "$RESULTS_DIR/$cond" -name "output.jsonl" | head -1)
    if [ -n "$output_file" ]; then
        count=$(wc -l < "$output_file")
        echo "$cond: $count tasks"
    else
        echo "$cond: no output"
    fi
done

echo "Disk: $(df -h / | tail -1)"
