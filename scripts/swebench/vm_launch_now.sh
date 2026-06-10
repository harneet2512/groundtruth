#!/bin/bash
set -euo pipefail

# All-in-one: restart proxy + launch 2-arm benchmark on singhharneet VM.

source ~/sweagent-env/bin/activate

# Kill stale
kill $(pgrep -f litellm 2>/dev/null) 2>/dev/null || true
kill $(pgrep -f "sweagent run-batch" 2>/dev/null) 2>/dev/null || true
sleep 3

# Ensure litellm config exists
cat > /tmp/litellm_fc.yaml << 'LCFG'
model_list:
  - model_name: qwen3-coder-480b-a35b-instruct-maas
    litellm_params:
      model: vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas
      vertex_project: project-26227097-98fa-4016-a54
      vertex_location: us-south1
      supports_function_calling: true
LCFG

# Start proxy
nohup litellm --config /tmp/litellm_fc.yaml --port 4000 > /tmp/litellm.log 2>&1 &
echo "Proxy PID=$!"
sleep 12

# Test
if curl -sf http://localhost:4000/health > /dev/null; then
    echo "Proxy: HEALTHY"
else
    echo "Proxy: FAILED"
    tail -10 /tmp/litellm.log
    exit 1
fi

# Setup
REPO="/tmp/Groundtruth_vnext"
SWEAGENT="/tmp/SWE-agent"
export OPENAI_API_BASE="http://localhost:4000/v1"
export OPENAI_API_KEY="dummy"

cp "$REPO/benchmarks/swebench/canary_nogt_qwen_fc.yaml" "$SWEAGENT/config/"
cp "$REPO/benchmarks/swebench/canary_gt_consolidated_fc.yaml" "$SWEAGENT/config/"

TASKS=$(paste -sd'|' "$REPO/scripts/swebench/frozen_gt_astropy10.txt")
OUTDIR="/tmp/consolidated_$(date +%s)"
mkdir -p "$OUTDIR/arm_BL" "$OUTDIR/arm_GT"

cd "$SWEAGENT"

echo ""
echo "=== Arm BL ==="
nohup python3 -m sweagent run-batch \
    --config config/canary_nogt_qwen_fc.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_BL" --num_workers 2 \
    > "$OUTDIR/arm_BL/run.log" 2>&1 &
echo "BL PID=$!"

echo "=== Arm GT ==="
nohup python3 -m sweagent run-batch \
    --config config/canary_gt_consolidated_fc.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_GT" --num_workers 2 \
    > "$OUTDIR/arm_GT/run.log" 2>&1 &
echo "GT PID=$!"

echo ""
echo "OUTDIR=$OUTDIR"
echo "LAUNCHED"
