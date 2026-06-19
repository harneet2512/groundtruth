#!/bin/bash
# GT v13 — 60-task Pro run with Gemini Pro
# Run on VM: bash ~/groundtruth/scripts/swebench/run_v13_60task.sh
set -euo pipefail

source ~/gt-venv/bin/activate
source ~/gt-env.sh 2>/dev/null || true

cd ~/groundtruth

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR=$HOME/results/v13_gt_pro_${TIMESTAMP}
mkdir -p "$OUTPUT_DIR"

echo "=== GT v13 — 60 Pro tasks ==="
echo "Output: $OUTPUT_DIR"
echo "Model: gemini-pro (via LiteLLM proxy)"
echo "Workers: 4"
echo "Started: $(date -u) UTC"
echo ""

# Verify LiteLLM is running
curl -s http://localhost:4000/health >/dev/null 2>&1 || {
    echo "ERROR: LiteLLM proxy not running. Starting..."
    systemctl --user start litellm
    sleep 10
    curl -s http://localhost:4000/health >/dev/null 2>&1 || { echo "FAIL: proxy won't start"; exit 1; }
}
echo "LiteLLM proxy: healthy"

# Run GT v13 hooked (60 tasks)
python3 benchmarks/swebench/run_mini_gt_hooked.py \
    -c benchmarks/swebench/mini_swebench_pro_baseline.yaml \
    --model openai/gemini-pro \
    --subset ScaleAI/SWE-bench_Pro --split test \
    --slice 0:60 \
    -w 4 \
    -o "$OUTPUT_DIR" \
    2>&1 | tee "$OUTPUT_DIR/run.log"

echo ""
echo "=== Run complete: $(date -u) UTC ==="
echo "Results: $OUTPUT_DIR"
ls -la "$OUTPUT_DIR/preds.json" 2>/dev/null || echo "WARNING: preds.json not found"
