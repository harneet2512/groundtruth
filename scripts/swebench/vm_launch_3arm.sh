#!/bin/bash
set -euo pipefail

# 3-arm comparison: BL vs GT-Tier1 (V104 v2 restored) vs GT-Tier1+2 (repo map + stuck detection)
# All use function_calling parser on Qwen3-Coder via Vertex AI MaaS.
#
# Evidence basis:
#   Tier 1: V104 v2 = 6/10 (+1 over 5/10 baseline) — verify_results.md Part 0
#   Tier 2a: Aider repo map — 26.3% on SWE-bench Lite (aider.chat/2024/05/22)
#   Tier 2b: Stuck detection — Reflexion +11pp (Shinn et al., NeurIPS 2023)

REPO="/tmp/Groundtruth_vnext"
SWEAGENT="/tmp/SWE-agent"
TIMESTAMP=$(date +%s)
OUTDIR="/tmp/tier12_$TIMESTAMP"

echo "=== 3-Arm Tier 1+2 Benchmark ==="
echo "Time: $(date -u)"
echo "Commit: $(cd $REPO && git rev-parse --short HEAD)"

source ~/sweagent-env/bin/activate
export OPENAI_API_BASE="http://localhost:4000/v1"
export OPENAI_API_KEY="dummy"

# Copy configs
cp "$REPO/benchmarks/swebench/canary_nogt_qwen_fc.yaml" "$SWEAGENT/config/"
cp "$REPO/benchmarks/swebench/canary_gt_qwen_fc.yaml" "$SWEAGENT/config/"
cp "$REPO/benchmarks/swebench/canary_gt_consolidated_fc.yaml" "$SWEAGENT/config/"

# Copy updated GT bundle files (gt_intel.py with repo map, state hook with stuck detection)
if [ -d "$SWEAGENT/tools/groundtruth" ]; then
    cp "$REPO/benchmarks/swebench/gt_intel.py" "$SWEAGENT/tools/groundtruth/bin/" 2>/dev/null || true
    cp "$REPO/benchmarks/swebench/vm_bundle/swe_agent_state_gt.py" "$SWEAGENT/tools/groundtruth/bin/" 2>/dev/null || true
    echo "GT bundle files updated"
fi

TASKS=$(paste -sd'|' "$REPO/scripts/swebench/frozen_gt_astropy10.txt")
mkdir -p "$OUTDIR"

cd "$SWEAGENT"

# Arm A: Baseline (no GT)
echo ""
echo "=== Arm A: Baseline (no GT) ==="
mkdir -p "$OUTDIR/arm_A_baseline"
nohup python3 -m sweagent run-batch \
    --config config/canary_nogt_qwen_fc.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_A_baseline" --num_workers 2 \
    > "$OUTDIR/arm_A_baseline/run.log" 2>&1 &
echo "  PID=$!"

# Arm B: GT Tier 1 (V104 v2 restored — canary_gt_qwen_fc.yaml)
echo ""
echo "=== Arm B: GT Tier 1 (V104 v2) ==="
mkdir -p "$OUTDIR/arm_B_tier1"
nohup python3 -m sweagent run-batch \
    --config config/canary_gt_qwen_fc.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_B_tier1" --num_workers 2 \
    > "$OUTDIR/arm_B_tier1/run.log" 2>&1 &
echo "  PID=$!"

# Arm C: GT Tier 1+2 (repo map + stuck detection — canary_gt_consolidated_fc.yaml)
echo ""
echo "=== Arm C: GT Tier 1+2 (repo map + stuck) ==="
mkdir -p "$OUTDIR/arm_C_tier12"
nohup python3 -m sweagent run-batch \
    --config config/canary_gt_consolidated_fc.yaml \
    --instances.subset verified --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$OUTDIR/arm_C_tier12" --num_workers 2 \
    > "$OUTDIR/arm_C_tier12/run.log" 2>&1 &
echo "  PID=$!"

echo ""
echo "=== LAUNCHED ==="
echo "OUTDIR=$OUTDIR"
echo "Monitor: tail -f $OUTDIR/arm_*/run.log"
