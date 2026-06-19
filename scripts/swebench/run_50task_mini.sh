#!/bin/bash
set -euo pipefail

# Fast 50-task A/B using mini-swe-agent + Qwen3-Coder via litellm proxy
# Expected: ~2 hours total (vs 10+ with OpenHands)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT="$HOME/results/v8_mini_${TIMESTAMP}"
NUM_TASKS=${1:-50}
NUM_WORKERS=${2:-4}

source ~/gt-venv/bin/activate
source ~/gt-env.sh

# Fix cost tracking for qwen3-coder (unknown model to litellm cost calculator)
export MSWEA_COST_TRACKING=ignore_errors
# Point to litellm proxy for qwen3-coder
export OPENAI_BASE_URL=http://localhost:4000/v1
export OPENAI_API_KEY=dummy

mkdir -p "$OUTPUT_ROOT"

# Verify proxy
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000"
    exit 1
fi
echo "Proxy: OK"

echo "================================================="
echo "  FAST 50-task A/B (mini-swe-agent + Qwen3-Coder)"
echo "  $(date -u) UTC"
echo "  Tasks:    $NUM_TASKS"
echo "  Workers:  $NUM_WORKERS"
echo "  Model:    openai/qwen3-coder"
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
    --model openai/qwen3-coder \
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
    --model openai/qwen3-coder \
    --subset lite --split test \
    --slice "0:$NUM_TASKS" \
    -w "$NUM_WORKERS" \
    -o "$GT_DIR" \
    2>&1 | tee "$GT_DIR/run.log" || true

echo "GT done: $(date -u) UTC"

# ── RESULTS ──────────────────────────────────────────────────────────
echo ""
echo "================================================="
echo "  RESULTS — $(date -u) UTC"
echo "================================================="
echo "Baseline: $BASELINE_DIR"
echo "GT v8:    $GT_DIR"

for d in "$BASELINE_DIR" "$GT_DIR"; do
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
