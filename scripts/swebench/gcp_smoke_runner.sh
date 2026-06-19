#!/usr/bin/env bash
# gcp_smoke_runner.sh — run GT smoke tasks on GCP VM
# Usage: bash gcp_smoke_runner.sh <task_id>
# Example: bash gcp_smoke_runner.sh amoffat__sh-744
set -euo pipefail

TASK="${1:?task_id required (e.g. amoffat__sh-744)}"
source /tmp/.env_gt
source /tmp/gtenv/bin/activate
export PYTHONPATH=/tmp/OpenHands:/tmp/groundtruth/src

echo "=== RUNNING $TASK at $(date) ==="
echo "Python: $(python --version)"
echo "DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY:0:10}..."

# Ensure clean config (no selected_ids prepend corruption)
cat > /tmp/OpenHands/evaluation/benchmarks/swe_bench/config.toml << EOF
[llm.eval]
model = "deepseek/deepseek-v4-flash"
api_key = "$DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com"
temperature = 1.0
top_p = 1.0
max_output_tokens = 65536
drop_params = true
num_retries = 10
timeout = 300
caching_prompt = false
native_tool_calling = true
EOF

# GT env vars (match GHA workflow)
export GT_PHASE=full
export GT_REBUILD_L1=1 GT_REBUILD_L3=1 GT_REBUILD_L3B=1 GT_REBUILD_L5=1
export GT_LAYER_EVENTS=1 GT_STRUCTURED_EVENTS=1 GT_STRUCTURAL_NEXT_ACTION=1
export GT_L3B_PRIMARY_EDGE=1 GT_L5_STRUCTURAL_UNVERIFIED=1 GT_L5_GOKU_EVENTS=1
export GT_DEEP_LAYER_GROUNDED_METRICS=1 GT_L5B_SAFETY_REQUIRED=1
export GT_LSP_VERIFY=1 GT_ROUTER_V2=live GT_NATIVE_TOOLS=1
export EVAL_CONDENSER=recent_events:5

# Output dirs
OUTDIR="/tmp/results_${TASK}"
EVALDIR="/tmp/eval_${TASK}"
mkdir -p "$OUTDIR" "$EVALDIR"

# Run agent via wrapper
cd /tmp/groundtruth
python scripts/swebench/oh_gt_full_wrapper.py \
    --instance-ids "$TASK" \
    -l eval \
    -i 100 \
    --eval-num-workers 1 \
    --eval-output-dir "$OUTDIR" \
    --dataset 'SWE-bench-Live/SWE-bench-Live' \
    --split lite \
    2>&1 | tee "/tmp/agent_${TASK}.log"

echo "=== AGENT DONE $TASK at $(date) ==="

# Eval immediately
PATCH=$(find "$OUTDIR" -name 'output.jsonl' -exec grep -l 'git_patch.*diff' {} \; 2>/dev/null | head -1)
if [ -z "$PATCH" ]; then
    echo "RESULT: NO_PATCH"
    exit 0
fi

mkdir -p "$EVALDIR"
find "$OUTDIR" -name 'output.jsonl' -exec cat {} + > "$EVALDIR/output.jsonl"
python scripts/swebench/convert_to_submission.py "$EVALDIR/output.jsonl" --output-dir "$EVALDIR" 2>&1 | tail -1

echo "GHCR login for eval images..."
echo "${GITHUB_TOKEN:-}" | docker login ghcr.io -u harneet2512 --password-stdin 2>/dev/null || true

python -m swebench.harness.run_evaluation \
    --dataset_name SWE-bench-Live/SWE-bench-Live \
    --split lite \
    --namespace starryzhang \
    --predictions_path "$EVALDIR/predictions.jsonl" \
    --max_workers 1 \
    --run_id "eval_${TASK}" \
    2>&1 | tee "/tmp/eval_${TASK}.log"

echo "=== EVAL RESULT ==="
grep "Instances resolved" "/tmp/eval_${TASK}.log" || echo "RESULT: UNKNOWN"
echo "=== COMPLETE $TASK at $(date) ==="
