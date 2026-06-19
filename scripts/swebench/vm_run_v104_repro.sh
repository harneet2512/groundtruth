#!/bin/bash
set -euo pipefail

# V104 reproduction: DeepSeek V3.2 + thought_action + original GT bundle.
# Goal: reproduce the 5/10 BL and 6/10 GT that V104 v2 achieved.
# Then change ONE variable at a time.

REPO="/tmp/Groundtruth_vnext"
SWEAGENT="/tmp/SWE-agent"
TIMESTAMP=$(date +%s)
OUTDIR="/tmp/v104_repro_$TIMESTAMP"

echo "=== V104 Reproduction ==="
echo "Time: $(date -u)"

source ~/sweagent-env/bin/activate

# Kill stale
kill $(pgrep -f litellm 2>/dev/null) 2>/dev/null || true
kill $(pgrep -f "sweagent run-batch" 2>/dev/null) 2>/dev/null || true
sleep 3

# Restart proxy with BOTH DeepSeek and Qwen models
PROJECT=$(gcloud config get project 2>/dev/null || echo "project-26227097-98fa-4016-a54")
cat > /tmp/litellm_v104.yaml << LCFG
model_list:
  - model_name: deepseek-v3.2-maas
    litellm_params:
      model: vertex_ai/deepseek-ai/deepseek-v3-0324
      vertex_project: $PROJECT
      vertex_location: us-east5
  - model_name: qwen3-coder-480b-a35b-instruct-maas
    litellm_params:
      model: vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas
      vertex_project: $PROJECT
      vertex_location: us-south1
      supports_function_calling: true
LCFG

echo "--- Starting proxy ---"
cat /tmp/litellm_v104.yaml
nohup litellm --config /tmp/litellm_v104.yaml --port 4000 > /tmp/litellm.log 2>&1 &
echo "Proxy PID=$!"
sleep 15

if curl -sf http://localhost:4000/health > /dev/null; then
    echo "Proxy: HEALTHY"
else
    echo "Proxy: FAILED"
    tail -20 /tmp/litellm.log
    exit 1
fi

# Copy configs
cp "$REPO/benchmarks/swebench/canary_v104_exact.yaml" "$SWEAGENT/config/"
cp "$REPO/benchmarks/swebench/canary_v104_baseline.yaml" "$SWEAGENT/config/"

# Restore the ORIGINAL install.sh (not the FC version)
cp "$SWEAGENT/tools/groundtruth/install.sh" "$SWEAGENT/tools/groundtruth/install_fc_backup.sh" 2>/dev/null || true
ORIG_INSTALL=$(find "$REPO/benchmarks/swebench" -path "*/groundtruth_bundle/install.sh" -not -name "*.pre*" | head -1)
if [ -n "$ORIG_INSTALL" ]; then
    cp "$ORIG_INSTALL" "$SWEAGENT/tools/groundtruth/install.sh"
    echo "Restored original install.sh from $ORIG_INSTALL"
else
    echo "WARN: Could not find original install.sh"
fi

export OPENAI_API_BASE="http://localhost:4000/v1"
export OPENAI_API_KEY="dummy"

TASKS=$(paste -sd'|' "$REPO/scripts/swebench/frozen_gt_astropy10.txt")
mkdir -p "$OUTDIR"

cd "$SWEAGENT"

# Arm BL: DeepSeek baseline (no GT)
echo ""
echo "=== Arm BL: DeepSeek V3.2 baseline (thought_action, no GT) ==="
mkdir -p "$OUTDIR/arm_BL"
nohup python3 -m sweagent run-batch \
    --config config/canary_v104_baseline.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_BL" --num_workers 2 \
    > "$OUTDIR/arm_BL/run.log" 2>&1 &
echo "BL PID=$!"

# Arm GT: DeepSeek + GT (V104 v2 exact)
echo ""
echo "=== Arm GT: DeepSeek V3.2 + GT (V104 v2 exact, thought_action) ==="
mkdir -p "$OUTDIR/arm_GT"
nohup python3 -m sweagent run-batch \
    --config config/canary_v104_exact.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_GT" --num_workers 2 \
    > "$OUTDIR/arm_GT/run.log" 2>&1 &
echo "GT PID=$!"

echo ""
echo "=== LAUNCHED ==="
echo "OUTDIR=$OUTDIR"
