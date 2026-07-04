#!/usr/bin/env bash
# SWE-bench Lite 300: Baseline vs GT V2 (passive context injection + post-edit validation).
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$REPO_DIR"

MODEL="${MODEL_NAME_EXACT:-}"
if [ -z "$MODEL" ]; then
  MODEL=$(python3 scripts/swebench/resolve_model.py --json | python3 -c "import sys,json; print(json.load(sys.stdin)['MODEL_NAME_EXACT'])")
  export MODEL_NAME_EXACT="$MODEL"
fi

RESULTS="${RESULTS:-benchmarks/swebench/results/lite_v2}"
WORKERS="${WORKERS:-4}"
mkdir -p "$RESULTS" logs

echo "=== Full Lite Baseline ($WORKERS workers) ==="
python3 -m benchmarks.swebench.runner \
  --mode baseline \
  --model "$MODEL" \
  --workers "$WORKERS" \
  --save-traces \
  --output-dir "$RESULTS" \
  2>&1 | tee logs/lite_v2_baseline.log || true

echo "=== Full Lite GroundTruth V2 ($WORKERS workers) ==="
python3 -m benchmarks.swebench.runner \
  --mode groundtruth_v2 \
  --model "$MODEL" \
  --workers "$WORKERS" \
  --save-traces \
  --output-dir "$RESULTS" \
  2>&1 | tee logs/lite_v2_groundtruth.log || true

echo "=== Evaluation (if swebench harness available) ==="
for cond in baseline groundtruth_v2; do
  pred="$RESULTS/$cond/predictions.jsonl"
  if [ -f "$pred" ]; then
    python3 -m benchmarks.swebench.evaluate "$pred" --run-id "${cond}-lite-v2" 2>/dev/null || echo "Eval skipped for $cond"
  fi
done

echo "=== Analysis ==="
python3 -m benchmarks.swebench.analyze \
  --baseline "$RESULTS/baseline" \
  --groundtruth "$RESULTS/groundtruth_v2" \
  --output "$RESULTS/analysis.json" \
  2>/dev/null || echo "Analysis skipped (install deps or check paths)"

echo "Done. Results in $RESULTS"
