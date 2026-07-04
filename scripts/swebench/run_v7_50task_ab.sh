#!/bin/bash
set -e

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
GT_DIR=/home/Lenovo/groundtruth

# Activate venv
source ~/gt-venv/bin/activate
export OPENAI_API_KEY=dummy
export OPENAI_BASE_URL=http://localhost:4000/v1

echo "========================================="
echo "  GT v7 — 50-task BASELINE run"
echo "  Started: $(date -u) UTC"
echo "========================================="

BASELINE_OUT=~/results/v7_baseline_50t_${TIMESTAMP}
mkdir -p "$BASELINE_OUT"

cd "$GT_DIR"
python3 benchmarks/swebench/run_v7_baseline.py swebench \
    -c benchmarks/swebench/mini_swebench_v7_baseline.yaml \
    --model openai/qwen3-coder \
    --subset lite --split test \
    --slice 0:50 \
    -w 4 \
    -o "$BASELINE_OUT" \
    2>&1 | tee "$BASELINE_OUT/run.log"

echo ""
echo "Baseline 50-task done at $(date -u) UTC"
echo "========================================="
echo ""
echo "========================================="
echo "  GT v7 — 50-task GT run"
echo "  Started: $(date -u) UTC"
echo "========================================="

GT_OUT=~/results/v7_gt_50t_${TIMESTAMP}
mkdir -p "$GT_OUT"

python3 benchmarks/swebench/run_mini_gt_v7.py swebench \
    -c benchmarks/swebench/mini_swebench_gt_v7.yaml \
    --model openai/qwen3-coder \
    --subset lite --split test \
    --slice 0:50 \
    -w 4 \
    -o "$GT_OUT" \
    2>&1 | tee "$GT_OUT/run.log"

echo ""
echo "GT 50-task done at $(date -u) UTC"
echo "Output: $GT_OUT"

# Quick comparison
echo ""
echo "========================================="
echo "  COMPARISON: Baseline vs GT (50 tasks)"
echo "========================================="
echo "Baseline: $BASELINE_OUT"
echo "GT:       $GT_OUT"
