#!/bin/bash
set -euo pipefail
source ~/sweagent-env/bin/activate
export OPENAI_API_BASE="http://localhost:4000/v1"
export OPENAI_API_KEY="dummy"
cd /tmp/SWE-agent
OUTDIR="/tmp/tier12_fc_$(date +%s)"
TASKS=$(paste -sd'|' /tmp/Groundtruth_vnext/scripts/swebench/frozen_gt_astropy10.txt)
mkdir -p "$OUTDIR/arm_B" "$OUTDIR/arm_C"

echo "=== Arm B: GT Tier 1 (FC install) ==="
nohup python3 -m sweagent run-batch \
    --config config/canary_gt_qwen_fc.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_B" --num_workers 2 \
    > "$OUTDIR/arm_B/run.log" 2>&1 &
echo "B PID=$!"

echo "=== Arm C: GT Tier 1+2 (FC install) ==="
nohup python3 -m sweagent run-batch \
    --config config/canary_gt_consolidated_fc.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_C" --num_workers 2 \
    > "$OUTDIR/arm_C/run.log" 2>&1 &
echo "C PID=$!"

echo "OUTDIR=$OUTDIR"
echo "LAUNCHED"
