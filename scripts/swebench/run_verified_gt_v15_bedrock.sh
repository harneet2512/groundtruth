#!/bin/bash
set -euo pipefail
source ~/gt-venv/bin/activate
source ~/gt-env.sh 2>/dev/null || true

REPO_DIR=${REPO_DIR:-$HOME/groundtruth}
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT=${OUTPUT_ROOT:-$HOME/results/v15_verified_bedrock_${TIMESTAMP}}
MODEL="${MODEL:-openai/claude-haiku-4-5-bedrock}"
WORKERS="${WORKERS:-20}"
CONFIG_PATH="${CONFIG_PATH:-scripts/swebench/litellm_bedrock_haiku45.yaml}"
GT_CONFIG="${GT_CONFIG:-benchmarks/swebench/mini_swebench_verified_gt_v13.yaml}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
    echo "ERROR: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set."
    exit 1
fi

export AWS_REGION="${AWS_REGION:-us-east-1}"

mkdir -p "$OUTPUT_ROOT"

echo "================================================="
echo "  v1.5 VERIFIED GT RUN (Bedrock / Haiku 4.5)"
echo "  Model: $MODEL | Workers: $WORKERS"
echo "  Region: $AWS_REGION"
echo "  $(date -u) UTC"
echo "  Output: $OUTPUT_ROOT"
echo "================================================="

cd "$REPO_DIR"

pkill -f 'litellm.*4000' 2>/dev/null || true
sleep 2

litellm --config "$CONFIG_PATH" --port 4000 > /tmp/litellm.log 2>&1 &
LITELLM_PID=$!
for i in $(seq 1 30); do curl -s http://localhost:4000/health >/dev/null 2>&1 && break; sleep 2; done
curl -s http://localhost:4000/health >/dev/null 2>&1 || { echo "Proxy: FAIL - check /tmp/litellm.log"; exit 1; }

echo "--- Starting GT run ---"
python3 benchmarks/swebench/run_mini_gt_hooked.py \
    -c "$GT_CONFIG" \
    --model "$MODEL" \
    --subset princeton-nlp/SWE-bench_Verified --split test \
    -w "$WORKERS" \
    -o "$OUTPUT_ROOT/gt_v15" \
    $EXTRA_ARGS

echo ""
if [ -f "$OUTPUT_ROOT/gt_v15/preds.json" ]; then
    GT_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_ROOT/gt_v15/preds.json'))))" 2>/dev/null || echo 0)
    echo "GT predictions: $GT_COUNT"
fi

echo "Output: $OUTPUT_ROOT"
pkill -f 'litellm.*4000' 2>/dev/null || true
