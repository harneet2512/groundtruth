#!/bin/bash
set -euo pipefail

source ~/sweagent-env/bin/activate
export OPENAI_API_BASE="http://localhost:4000/v1"
export OPENAI_API_KEY="dummy"

cd /tmp/SWE-agent
OUTDIR="/tmp/tier12_1777271983"
TASKS=$(paste -sd'|' /tmp/Groundtruth_vnext/scripts/swebench/frozen_gt_astropy10.txt)

rm -rf "$OUTDIR/arm_B_tier1" "$OUTDIR/arm_C_tier12"
mkdir -p "$OUTDIR/arm_B_tier1" "$OUTDIR/arm_C_tier12"

echo "=== Arm B: GT Tier 1 ==="
nohup python3 -m sweagent run-batch \
    --config config/canary_gt_qwen_fc.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_B_tier1" --num_workers 2 \
    > "$OUTDIR/arm_B_tier1/run.log" 2>&1 &
echo "B PID=$!"

echo "=== Arm C: GT Tier 1+2 ==="
nohup python3 -m sweagent run-batch \
    --config config/canary_gt_consolidated_fc.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_C_tier12" --num_workers 2 \
    > "$OUTDIR/arm_C_tier12/run.log" 2>&1 &
echo "C PID=$!"

echo "LAUNCHED"
