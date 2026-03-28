#!/bin/bash
set -euo pipefail

# Fast 50-task A/B using mini-swe-agent + gpt-5.4-nano
# ~45 min total (vs hours with OpenHands + Qwen3)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT="$HOME/results/v8_mini_${TIMESTAMP}"
NUM_TASKS=${1:-50}
NUM_WORKERS=${2:-4}

source ~/gt-venv/bin/activate
source ~/gt-env.sh

mkdir -p "$OUTPUT_ROOT"

echo "================================================="
echo "  FAST 50-task A/B (mini-swe-agent + gpt-5.4-nano)"
echo "  $(date -u) UTC"
echo "  Tasks:    $NUM_TASKS"
echo "  Workers:  $NUM_WORKERS"
echo "  Model:    openai/gpt-5.4-nano"
echo "  Output:   $OUTPUT_ROOT"
echo "================================================="

cd "$REPO_DIR"

# ── BASELINE ─────────────────────────────────────────────────────────
BASELINE_DIR="$OUTPUT_ROOT/baseline"
mkdir -p "$BASELINE_DIR"

echo ""
echo "─── BASELINE ($NUM_TASKS tasks) ───"
echo "  $(date -u) UTC"

python3 benchmarks/swebench/run_v7_baseline.py \
    -c benchmarks/swebench/mini_swebench_v7_baseline.yaml \
    --model openai/gpt-5.4-nano \
    --subset lite --split test \
    --slice "0:$NUM_TASKS" \
    -w "$NUM_WORKERS" \
    -o "$BASELINE_DIR" \
    2>&1 | tee "$BASELINE_DIR/run.log" || true

echo "Baseline done: $(date -u) UTC"

# ── GT V8 PRECOMPUTE ─────────────────────────────────────────────────
GT_DIR="$OUTPUT_ROOT/gt_v8"
mkdir -p "$GT_DIR"

echo ""
echo "─── GT V8 PRECOMPUTE ($NUM_TASKS tasks) ───"
echo "  $(date -u) UTC"

python3 benchmarks/swebench/run_mini_gt_v8_precompute.py \
    -c benchmarks/swebench/mini_swebench_gt_v7.yaml \
    --model openai/gpt-5.4-nano \
    --subset lite --split test \
    --slice "0:$NUM_TASKS" \
    -w "$NUM_WORKERS" \
    -o "$GT_DIR" \
    2>&1 | tee "$GT_DIR/run.log" || true

echo "GT done: $(date -u) UTC"

# ── RESULTS ──────────────────────────────────────────────────────────
echo ""
echo "================================================="
echo "  RESULTS"
echo "  $(date -u) UTC"
echo "================================================="
echo "Baseline: $BASELINE_DIR"
echo "GT v8:    $GT_DIR"

# Count predictions
for d in "$BASELINE_DIR" "$GT_DIR"; do
    label=$(basename "$d")
    if [ -f "$d/preds.json" ]; then
        count=$(python3 -c "import json; print(len(json.load(open('$d/preds.json'))))" 2>/dev/null || echo 0)
        echo "  $label: $count predictions"
    fi
done

echo ""
echo "================================================="
echo "  ALL DONE: $(date -u) UTC"
echo "================================================="
