#!/usr/bin/env bash
# Full 300-task A/B run on OpenHands: Baseline vs GT Phase 3.
#
# Usage:
#   bash scripts/swebench/run_300_openhands.sh               # sequential (safe)
#   bash scripts/swebench/run_300_openhands.sh --parallel     # both conditions simultaneously
set -euo pipefail

source ~/gt-venv/bin/activate
[ -f ~/gt-env.sh ] && source ~/gt-env.sh
cd ~/groundtruth

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR=~/openhands_300_${TIMESTAMP}
BASELINE_DIR="$OUTPUT_DIR/baseline"
GT_DIR="$OUTPUT_DIR/gt"
mkdir -p "$BASELINE_DIR" "$GT_DIR"

BASELINE_CONFIG="benchmarks/swebench/openhands_config_baseline.toml"
GT_CONFIG="benchmarks/swebench/openhands_config_gt.toml"
WORKERS="${OH_WORKERS:-4}"

echo "=== OpenHands 300-Task A/B Run ==="
echo "Output:  $OUTPUT_DIR"
echo "Workers: $WORKERS"
echo "Started: $(date)"

# ── Detect OpenHands inference command ────────────────────────────────
if command -v openhands-swebench-infer &> /dev/null; then
    INFER_CMD="openhands-swebench-infer"
elif python3 -m openhands.swebench.scripts.infer --help &> /dev/null 2>&1; then
    INFER_CMD="python3 -m openhands.swebench.scripts.infer"
else
    echo "ERROR: Cannot find OpenHands SWE-bench inference command."
    exit 1
fi
echo "Inference command: $INFER_CMD"

run_condition() {
    local name="$1"
    local config="$2"
    local outdir="$3"

    echo ""
    echo "=== Condition: $name ==="
    echo "Config: $config"
    echo "Output: $outdir"
    echo "Started: $(date)"

    $INFER_CMD \
        --llm-config "$config" \
        --dataset princeton-nlp/SWE-bench_Lite --split test \
        --max-iterations 300 \
        --num-workers "$WORKERS" \
        -o "$outdir" \
        2>&1 | tee "$outdir/run.log"

    echo "$name finished: $(date)"
}

# ── Run ───────────────────────────────────────────────────────────────
if [ "${1:-}" = "--parallel" ]; then
    echo "Running both conditions in parallel..."
    run_condition "baseline" "$BASELINE_CONFIG" "$BASELINE_DIR" &
    PID_BL=$!
    run_condition "gt_phase3" "$GT_CONFIG" "$GT_DIR" &
    PID_GT=$!

    echo "Baseline PID: $PID_BL, GT PID: $PID_GT"
    echo "Waiting for both to complete..."

    wait $PID_BL
    BL_EXIT=$?
    wait $PID_GT
    GT_EXIT=$?

    echo "Baseline exit: $BL_EXIT, GT exit: $GT_EXIT"
else
    echo "Running sequentially (baseline first, then GT)..."
    run_condition "baseline" "$BASELINE_CONFIG" "$BASELINE_DIR"
    run_condition "gt_phase3" "$GT_CONFIG" "$GT_DIR"
fi

# ── Mid-audit (quick stats) ──────────────────────────────────────────
echo ""
echo "============================================"
echo "=== POST-RUN SUMMARY ==="
echo "============================================"

python3 -c "
import os, glob, json

for label, d in [('Baseline', '$BASELINE_DIR'), ('GT Phase3', '$GT_DIR')]:
    files = glob.glob(os.path.join(d, '**/*.json'), recursive=True)
    files += glob.glob(os.path.join(d, '*.jsonl'), recursive=True)
    task_count = len([f for f in files if 'traj' in f or 'output' in f])
    print(f'{label}: {task_count} tasks completed, {len(files)} files')
"

echo ""
echo "=== Next Steps ==="
echo "1. Convert trajectories to SWE-bench predictions format"
echo "2. Run Docker eval:"
echo "   bash scripts/swebench/run_eval.sh $BASELINE_DIR/preds.json baseline_oh"
echo "   bash scripts/swebench/run_eval.sh $GT_DIR/preds.json phase3_oh"
echo "3. Compare results and write attribution report"
echo ""
echo "Output: $OUTPUT_DIR"
echo "Finished: $(date)"
