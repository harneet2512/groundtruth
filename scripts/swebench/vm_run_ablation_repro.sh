#!/bin/bash
set -euo pipefail

# Reproduce the Apr 24 ablation on Qwen:
#   B (format repair, no GT) = 2/10
#   E (full GT) = 3/10
# Source: benchmarks/swebench/fast_diag/ablation_results_qwen_2026-04-24.md

REPO="/tmp/Groundtruth_vnext"
SWEAGENT="/tmp/SWE-agent"
TIMESTAMP=$(date +%s)
OUTDIR="/tmp/ablation_repro_$TIMESTAMP"

echo "=== Ablation Reproduction (Qwen + thought_action) ==="
echo "Time: $(date -u)"

source ~/sweagent-env/bin/activate

kill $(pgrep -f "sweagent run-batch" 2>/dev/null) 2>/dev/null || true
sleep 2

# Verify proxy
curl -sf http://localhost:4000/health > /dev/null || {
    echo "Proxy not running. Starting..."
    PROJECT=$(gcloud config get project 2>/dev/null || echo "project-26227097-98fa-4016-a54")
    printf 'model_list:\n  - model_name: qwen3-coder-480b-a35b-instruct-maas\n    litellm_params:\n      model: vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas\n      vertex_project: %s\n      vertex_location: us-south1\n' "$PROJECT" > /tmp/litellm_proxy.yaml
    nohup litellm --config /tmp/litellm_proxy.yaml --port 4000 > /tmp/litellm.log 2>&1 &
    sleep 15
}
echo "Proxy: OK"

# Copy configs
cp "$REPO/benchmarks/swebench/canary_qwen_ablation_B.yaml" "$SWEAGENT/config/"
cp "$REPO/benchmarks/swebench/canary_qwen_ablation_E.yaml" "$SWEAGENT/config/"

# Restore original install.sh for thought_action (with submit gate)
ORIG=$(find "$SWEAGENT/tools/groundtruth" -name "install.sh.preC*" -o -name "install.sh" | grep -v "fc" | head -1)
if [ -n "$ORIG" ] && [ -f "$ORIG" ]; then
    cp "$ORIG" "$SWEAGENT/tools/groundtruth/install.sh"
    echo "Using install.sh: $ORIG"
else
    echo "WARN: Could not find original install.sh"
fi

export OPENAI_API_BASE="http://localhost:4000/v1"
export OPENAI_API_KEY="dummy"
TASKS=$(paste -sd'|' "$REPO/scripts/swebench/frozen_gt_astropy10.txt")
mkdir -p "$OUTDIR"
cd "$SWEAGENT"

echo ""
echo "=== Arm B: format repair only (no GT) ==="
mkdir -p "$OUTDIR/arm_B"
nohup python3 -m sweagent run-batch \
    --config config/canary_qwen_ablation_B.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_B" --num_workers 2 \
    > "$OUTDIR/arm_B/run.log" 2>&1 &
echo "B PID=$!"

echo ""
echo "=== Arm E: full GT (thought_action) ==="
mkdir -p "$OUTDIR/arm_E"
nohup python3 -m sweagent run-batch \
    --config config/canary_qwen_ablation_E.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_E" --num_workers 2 \
    > "$OUTDIR/arm_E/run.log" 2>&1 &
echo "E PID=$!"

echo ""
echo "=== LAUNCHED ==="
echo "OUTDIR=$OUTDIR"
