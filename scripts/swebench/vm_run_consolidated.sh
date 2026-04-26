#!/bin/bash
set -euo pipefail

# 2-arm: BL (no GT) vs GT-consolidated (hook-only, no GT shell tools, 0.7 confidence gate).
# Both use function_calling parser.

REPO="/tmp/Groundtruth_vnext"
SWEAGENT="/tmp/SWE-agent"
TIMESTAMP=$(date +%s)
OUTDIR="/tmp/consolidated_$TIMESTAMP"

echo "=== Consolidated GT Benchmark ==="
echo "Time: $(date -u)"

source ~/sweagent-env/bin/activate

# Kill stale
kill $(pgrep -f "sweagent run-batch" 2>/dev/null) 2>/dev/null || true
sleep 2

# Verify proxy
if ! curl -sf http://localhost:4000/health > /dev/null 2>&1; then
    echo "FAIL: LiteLLM proxy not running on port 4000"
    exit 1
fi
echo "Proxy: OK"

# Copy configs
cp "$REPO/benchmarks/swebench/canary_nogt_qwen_fc.yaml" "$SWEAGENT/config/"
cp "$REPO/benchmarks/swebench/canary_gt_consolidated_fc.yaml" "$SWEAGENT/config/"

# Copy updated gt_intel.py with 0.7 confidence gate
cp "$REPO/benchmarks/swebench/gt_intel.py" "$SWEAGENT/tools/groundtruth/" 2>/dev/null || true

export OPENAI_API_BASE="http://172.17.0.1:4000/v1"
export OPENAI_API_KEY="dummy"

TASKS=$(paste -sd'|' "$REPO/scripts/swebench/frozen_gt_astropy10.txt")
mkdir -p "$OUTDIR"

echo ""
echo "=== Arm BL: baseline (no GT, function_calling) ==="
BL_DIR="$OUTDIR/arm_BL"
mkdir -p "$BL_DIR"
cd "$SWEAGENT"
nohup python3 -m sweagent run-batch \
    --config config/canary_nogt_qwen_fc.yaml \
    --instances.subset verified \
    --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$BL_DIR" \
    --num_workers 4 \
    > "$BL_DIR/run.log" 2>&1 &
BL_PID=$!
echo "  PID=$BL_PID"

echo ""
echo "=== Arm GT: consolidated (hook-only, 0.7 gate, function_calling) ==="
GT_DIR="$OUTDIR/arm_GT"
mkdir -p "$GT_DIR"
nohup python3 -m sweagent run-batch \
    --config config/canary_gt_consolidated_fc.yaml \
    --instances.subset verified \
    --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$GT_DIR" \
    --num_workers 4 \
    > "$GT_DIR/run.log" 2>&1 &
GT_PID=$!
echo "  PID=$GT_PID"

echo ""
echo "=== LAUNCHED ==="
echo "Output: $OUTDIR"
echo "BL PID=$BL_PID  GT PID=$GT_PID"
echo "Monitor: tail -f $OUTDIR/arm_*/run.log"
