#!/bin/bash
set -euo pipefail

# v11 Pro 60-task: Sequential baseline then GT, 8 workers each
# Expected: ~25 min baseline + ~30 min GT + ~20 min eval = ~75 min total

source ~/gt-venv/bin/activate
source ~/gt-env.sh
export MSWEA_COST_TRACKING=ignore_errors
export OPENAI_BASE_URL=http://localhost:4000/v1
export OPENAI_API_KEY=dummy

REPO_DIR=/home/Lenovo/groundtruth
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT=$HOME/results/v11_pro_60_${TIMESTAMP}
mkdir -p "$OUTPUT_ROOT"

echo "================================================="
echo "  v11 Pro 60-task A/B (8 workers, sequential)"
echo "  $(date -u) UTC"
echo "  Output: $OUTPUT_ROOT"
echo "================================================="

cd "$REPO_DIR"

# ── BASELINE (8 workers) ──
echo ""
echo "--- BASELINE (60 tasks, 8 workers) ---"
echo "  $(date -u) UTC"
python3 benchmarks/swebench/run_v7_baseline.py \
    -c benchmarks/swebench/mini_swebench_pro_baseline.yaml \
    --model openai/qwen3-coder \
    --subset ScaleAI/SWE-bench_Pro --split test --slice 0:60 \
    -w 8 \
    -o "$OUTPUT_ROOT/baseline" \
    2>&1 | tee "$OUTPUT_ROOT/baseline.log" || true
echo "Baseline done: $(date -u) UTC"

# ── GT v11 (8 workers) ──
echo ""
echo "--- GT V11 (60 tasks, 8 workers) ---"
echo "  $(date -u) UTC"
python3 benchmarks/swebench/run_mini_gt_hooked.py \
    -c benchmarks/swebench/mini_swebench_pro_baseline.yaml \
    --model openai/qwen3-coder \
    --subset ScaleAI/SWE-bench_Pro --split test --slice 0:60 \
    -w 8 \
    -o "$OUTPUT_ROOT/gt_v11" \
    2>&1 | tee "$OUTPUT_ROOT/gt_v11.log" || true
echo "GT done: $(date -u) UTC"

# ── RESULTS ──
echo ""
echo "================================================="
echo "  RESULTS — $(date -u) UTC"
echo "================================================="
for d in "$OUTPUT_ROOT/baseline" "$OUTPUT_ROOT/gt_v11"; do
    label=$(basename "$d")
    if [ -f "$d/preds.json" ]; then
        count=$(python3 -c "import json; print(len(json.load(open('$d/preds.json'))))" 2>/dev/null || echo 0)
        echo "  $label: $count predictions"
    else
        echo "  $label: no preds.json"
    fi
done

echo ""
echo "ALL DONE: $(date -u) UTC"
echo "================================================="
