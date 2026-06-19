#!/usr/bin/env bash
# Stage C+D+E: Full Lite run, then evaluate and analyze.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$REPO_DIR"

MODEL="${MODEL_NAME_EXACT:-}"
if [ -z "$MODEL" ]; then
  MODEL=$(python3 scripts/swebench/resolve_model.py --json | python3 -c "import sys,json; print(json.load(sys.stdin)['MODEL_NAME_EXACT'])")
  export MODEL_NAME_EXACT="$MODEL"
fi

RESULTS="${RESULTS:-benchmarks/swebench/results/lite}"
WORKERS="${WORKERS:-4}"
mkdir -p "$RESULTS" logs

echo "=== Full Lite Baseline ($WORKERS workers) ==="
python3 -m benchmarks.swebench.runner \
  --mode baseline \
  --model "$MODEL" \
  --workers "$WORKERS" \
  --output-dir "$RESULTS" \
  2>&1 | tee logs/lite_baseline.log || true

echo "=== Full Lite GroundTruth MCP ($WORKERS workers) ==="
python3 -m benchmarks.swebench.runner \
  --mode groundtruth_mcp \
  --model "$MODEL" \
  --workers "$WORKERS" \
  --output-dir "$RESULTS" \
  2>&1 | tee logs/lite_groundtruth.log || true

echo "=== Validate MCP proof ==="
python3 scripts/swebench/validate_mcp_proof.py "$RESULTS/groundtruth_mcp" || echo "WARNING: MCP proof validation had issues (see above). Continuing with evaluation."

echo "=== Evaluation (if swebench harness available) ==="
for cond in baseline groundtruth_mcp; do
  pred="$RESULTS/$cond/predictions.jsonl"
  if [ -f "$pred" ]; then
    python3 -m benchmarks.swebench.evaluate "$pred" --run-id "${cond}-lite" 2>/dev/null || echo "Eval skipped for $cond"
  fi
done

echo "=== Analysis ==="
python3 -m benchmarks.swebench.analyze \
  --baseline "$RESULTS/baseline" \
  --groundtruth "$RESULTS/groundtruth_mcp" \
  --output "$RESULTS/analysis.json" \
  2>/dev/null || echo "Analysis skipped (install deps or check paths)"

echo "Done. Results in $RESULTS"
