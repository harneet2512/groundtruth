#!/bin/bash
# GT v13 — 60-task Pro run with Gemini Pro
# Run on VM: bash ~/groundtruth/scripts/swebench/run_v13_60task.sh
set -e

source ~/gt-venv/bin/activate
cd ~/groundtruth

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR=~/results/v13_gt_pro_${TIMESTAMP}
mkdir -p $OUTPUT_DIR

echo "=== GT v13 — 60 Pro tasks ==="
echo "Output: $OUTPUT_DIR"
echo "Model: gemini-pro (via LiteLLM proxy at localhost:4000)"
echo "Workers: 4"
echo "Started: $(date)"
echo ""

# Verify LiteLLM is running
curl -s http://localhost:4000/health >/dev/null 2>&1 || {
    echo "ERROR: LiteLLM proxy not running. Start with: systemctl --user start litellm"
    exit 1
}
echo "LiteLLM proxy: healthy"

# Run GT v13 hooked
export OPENAI_API_KEY="sk-placeholder"
export OPENAI_API_BASE="http://localhost:4000/v1"

python3 benchmarks/swebench/run_mini_gt_hooked.py \
    -c benchmarks/swebench/mini_swebench_pro_baseline.yaml \
    --model openai/gemini-pro \
    --subset ScaleAI/SWE-bench_Pro \
    --split test \
    --slice 0:60 \
    -w 4 \
    --output-dir $OUTPUT_DIR \
    2>&1 | tee $OUTPUT_DIR/run.log

echo ""
echo "=== Run complete: $(date) ==="
echo "Results: $OUTPUT_DIR"
echo "Preds: $OUTPUT_DIR/preds.json"
