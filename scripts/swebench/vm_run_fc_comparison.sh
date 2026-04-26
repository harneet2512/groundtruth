#!/bin/bash
# 2-arm comparison: baseline vs GT, both using function_calling parser.
#
# This is the corrected benchmark. The previous runs used thought_action parser
# which is incompatible with Qwen3-Coder (43-block monologue → parser picks wrong action).
# function_calling uses structured JSON tool calls, matching V104 behavior.
#
# Prerequisites:
#   - LiteLLM proxy running at 172.17.0.1:4000 (or $OPENAI_API_BASE)
#   - preflight_fc_parser.sh passed (especially test 3: tool_calls work)
#   - SWE-agent installed: pip install sweagent
#   - Docker running
#
# Usage: bash vm_run_fc_comparison.sh [OUTDIR]

set -euo pipefail

TIMESTAMP=$(date +%s)
OUTDIR="${1:-/tmp/fc_comparison_$TIMESTAMP}"
REPO_DIR="${GT_REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
TASK_SUITE="$REPO_DIR/scripts/swebench/frozen_gt_astropy10.txt"
WORKERS="${GT_WORKERS:-4}"

echo "=== FC Parser 2-Arm Comparison ==="
echo "Time: $(date -u)"
echo "Output: $OUTDIR"
echo "Workers: $WORKERS"
echo "Tasks: $(wc -l < "$TASK_SUITE") from frozen_gt_astropy10"
echo ""

# Ensure configs are in SWE-agent config dir
SWEAGENT_DIR="${GT_SWEAGENT_DIR:-/tmp/SWE-agent}"
if [ -d "$SWEAGENT_DIR/config" ]; then
    cp "$REPO_DIR/benchmarks/swebench/canary_gt_qwen_fc.yaml" "$SWEAGENT_DIR/config/"
    cp "$REPO_DIR/benchmarks/swebench/canary_nogt_qwen_fc.yaml" "$SWEAGENT_DIR/config/"
    echo "Configs copied to $SWEAGENT_DIR/config/"
else
    echo "WARN: $SWEAGENT_DIR/config/ not found, using repo configs directly"
fi

# Read task list
TASKS=$(paste -sd'|' "$TASK_SUITE")

mkdir -p "$OUTDIR"

# ── Arm BL: Baseline (no GT, function_calling) ──
echo ""
echo "=== Launching Arm BL (baseline, function_calling) ==="
BL_DIR="$OUTDIR/arm_BL"
mkdir -p "$BL_DIR"

nohup python3 -m sweagent run-batch \
    --config "$REPO_DIR/benchmarks/swebench/canary_nogt_qwen_fc.yaml" \
    --instances.subset verified \
    --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$BL_DIR" \
    --num_workers "$WORKERS" \
    > "$BL_DIR/run.log" 2>&1 &
BL_PID=$!
echo "  PID=$BL_PID arm=BL"

# ── Arm GT: GroundTruth (function_calling) ──
echo ""
echo "=== Launching Arm GT (GroundTruth, function_calling) ==="
GT_DIR="$OUTDIR/arm_GT"
mkdir -p "$GT_DIR"

# Copy GT bundle for each task
for TASK in $(cat "$TASK_SUITE"); do
    TASK_BUNDLE="$GT_DIR/$TASK/groundtruth_bundle"
    mkdir -p "$TASK_BUNDLE"
    if [ -d "$SWEAGENT_DIR/tools/groundtruth" ]; then
        cp -a "$SWEAGENT_DIR/tools/groundtruth/." "$TASK_BUNDLE/"
    elif [ -d "$REPO_DIR/benchmarks/swebench/vm_bundle" ]; then
        cp -a "$REPO_DIR/benchmarks/swebench/vm_bundle/." "$TASK_BUNDLE/"
    fi
done

nohup python3 -m sweagent run-batch \
    --config "$REPO_DIR/benchmarks/swebench/canary_gt_qwen_fc.yaml" \
    --instances.subset verified \
    --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$GT_DIR" \
    --num_workers "$WORKERS" \
    > "$GT_DIR/run.log" 2>&1 &
GT_PID=$!
echo "  PID=$GT_PID arm=GT"

echo ""
echo "Both arms launched. PIDs: BL=$BL_PID GT=$GT_PID"
echo "Monitor: tail -f $BL_DIR/run.log $GT_DIR/run.log"
echo ""
echo "Wait for completion:"
echo "  wait $BL_PID $GT_PID"
echo ""
echo "Evaluate after completion:"
echo "  python3 -m swebench.harness.run_evaluation --predictions_path $BL_DIR/preds.json --run_id arm_BL --max_workers 4"
echo "  python3 -m swebench.harness.run_evaluation --predictions_path $GT_DIR/preds.json --run_id arm_GT --max_workers 4"
