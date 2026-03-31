#!/bin/bash
set -euo pipefail
source ~/gt-venv/bin/activate
source ~/gt-env.sh 2>/dev/null || true

REPO_DIR=$HOME/groundtruth
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT=$HOME/results/v13_verified_500_${TIMESTAMP}
MODEL="${MODEL:-openai/gemini-flash}"
WORKERS="${WORKERS:-20}"
CONDITION="${1:-both}"  # "baseline", "gt", or "both"

mkdir -p "$OUTPUT_ROOT"

echo "================================================="
echo "  v13 VERIFIED 500-TASK RUN"
echo "  Model: $MODEL | Workers: $WORKERS | Condition: $CONDITION"
echo "  $(date -u) UTC"
echo "  Output: $OUTPUT_ROOT"
echo "================================================="

cd "$REPO_DIR"

# ── Start LiteLLM proxy ──────────────────────────────────────────────────
pkill -f 'litellm.*4000' 2>/dev/null || true
sleep 2

litellm --config scripts/swebench/litellm_verified.yaml --port 4000 > /tmp/litellm.log 2>&1 &
LITELLM_PID=$!
for i in $(seq 1 30); do curl -s http://localhost:4000/health >/dev/null 2>&1 && break; sleep 2; done
curl -s http://localhost:4000/health >/dev/null 2>&1 && echo "Proxy: OK (PID $LITELLM_PID)" || { echo "Proxy: FAIL"; exit 1; }

# ── Run conditions ────────────────────────────────────────────────────────

if [ "$CONDITION" = "baseline" ] || [ "$CONDITION" = "both" ]; then
    echo ""
    echo "--- Starting BASELINE (500 tasks, $WORKERS workers) ---"
    python3 benchmarks/swebench/run_v7_baseline.py \
        -c benchmarks/swebench/mini_swebench_verified_baseline.yaml \
        --model "$MODEL" \
        --subset princeton-nlp/SWE-bench_Verified --split test \
        -w "$WORKERS" \
        -o "$OUTPUT_ROOT/baseline" \
        > "$OUTPUT_ROOT/baseline.log" 2>&1 &
    BL_PID=$!
    echo "Baseline PID: $BL_PID"
fi

if [ "$CONDITION" = "gt" ] || [ "$CONDITION" = "both" ]; then
    echo ""
    echo "--- Starting GT v13 (500 tasks, $WORKERS workers) ---"
    python3 benchmarks/swebench/run_mini_gt_hooked.py \
        -c benchmarks/swebench/mini_swebench_verified_gt_v13.yaml \
        --model "$MODEL" \
        --subset princeton-nlp/SWE-bench_Verified --split test \
        -w "$WORKERS" \
        -o "$OUTPUT_ROOT/gt_v13" \
        > "$OUTPUT_ROOT/gt_v13.log" 2>&1 &
    GT_PID=$!
    echo "GT PID: $GT_PID"
fi

# ── Wait for completion ───────────────────────────────────────────────────
echo ""
echo "Waiting for runs to complete..."

if [ -n "${BL_PID:-}" ]; then
    wait $BL_PID && echo "Baseline done: $(date -u) UTC" || echo "Baseline exited with error: $(date -u) UTC"
fi
if [ -n "${GT_PID:-}" ]; then
    wait $GT_PID && echo "GT done: $(date -u) UTC" || echo "GT exited with error: $(date -u) UTC"
fi

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
echo "================================================="
echo "  COMPLETION SUMMARY"
echo "================================================="

if [ -f "$OUTPUT_ROOT/baseline/preds.json" ]; then
    BL_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_ROOT/baseline/preds.json'))))" 2>/dev/null || echo 0)
    echo "Baseline predictions: $BL_COUNT / 500"
fi

if [ -f "$OUTPUT_ROOT/gt_v13/preds.json" ]; then
    GT_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_ROOT/gt_v13/preds.json'))))" 2>/dev/null || echo 0)
    echo "GT v13 predictions: $GT_COUNT / 500"
fi

echo ""
echo "Output: $OUTPUT_ROOT"
echo "Done: $(date -u) UTC"

pkill -f 'litellm.*4000' 2>/dev/null || true
