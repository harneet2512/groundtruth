#!/bin/bash
set -euo pipefail
source ~/gt-venv/bin/activate
source ~/gt-env.sh

REPO_DIR=$HOME/groundtruth
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT=$HOME/results/v13_full_731_${TIMESTAMP}
mkdir -p "$OUTPUT_ROOT"

echo "================================================="
echo "  v13 FULL 731-task PARALLEL (Gemini 3.1 Pro)"
echo "  $(date -u) UTC"
echo "  Output: $OUTPUT_ROOT"
echo "================================================="

cd "$REPO_DIR"

pkill -f 'litellm.*4000' 2>/dev/null || true
sleep 2

litellm --config ~/litellm_config.yaml --port 4000 > /tmp/litellm.log 2>&1 &
LITELLM_PID=$!
for i in $(seq 1 30); do curl -s http://localhost:4000/health >/dev/null 2>&1 && break; sleep 2; done
curl -s http://localhost:4000/health >/dev/null 2>&1 && echo "Proxy: OK (PID $LITELLM_PID)" || { echo "Proxy: FAIL"; exit 1; }

echo "--- Starting BASELINE (731 tasks, 2 workers) ---"
python3 benchmarks/swebench/run_v7_baseline.py \
    -c benchmarks/swebench/mini_swebench_pro_baseline.yaml \
    --model openai/gemini-pro \
    --subset ScaleAI/SWE-bench_Pro --split test \
    -w 2 \
    -o "$OUTPUT_ROOT/baseline" \
    > "$OUTPUT_ROOT/baseline.log" 2>&1 &
BL_PID=$!

echo "--- Starting GT V13 (731 tasks, 2 workers) ---"
python3 benchmarks/swebench/run_mini_gt_hooked.py \
    -c benchmarks/swebench/mini_swebench_pro_baseline.yaml \
    --model openai/gemini-pro \
    --subset ScaleAI/SWE-bench_Pro --split test \
    -w 2 \
    -o "$OUTPUT_ROOT/gt_v13" \
    > "$OUTPUT_ROOT/gt_v13.log" 2>&1 &
GT_PID=$!

echo "BL PID: $BL_PID, GT PID: $GT_PID"
echo "Both running in parallel. Waiting..."
wait $BL_PID; echo "Baseline done: $(date -u) UTC"
wait $GT_PID; echo "GT done: $(date -u) UTC"

BL_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_ROOT/baseline/preds.json'))))" 2>/dev/null || echo 0)
GT_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_ROOT/gt_v13/preds.json'))))" 2>/dev/null || echo 0)
echo "=== RESULTS: BL=$BL_COUNT GT=$GT_COUNT ==="

pkill -f 'litellm.*4000' 2>/dev/null || true
echo "ALL DONE: $(date -u) UTC"
