#!/usr/bin/env bash
# Stage A: 1-2 Lite tasks, 1 worker, both conditions. Validate MCP proof.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$REPO_DIR"

MODEL="${MODEL_NAME_EXACT:-}"
if [ -z "$MODEL" ]; then
  echo "Resolving model..."
  MODEL=$(python3 scripts/swebench/resolve_model.py --json | python3 -c "import sys,json; print(json.load(sys.stdin)['MODEL_NAME_EXACT'])")
  export MODEL_NAME_EXACT="$MODEL"
fi
echo "Model: $MODEL"

# Two instance IDs for smoke (from SWE-bench Lite)
INSTANCE_IDS="${INSTANCE_IDS:-django__django-11039 django__django-11049}"
RESULTS="${RESULTS:-benchmarks/swebench/results/smoke}"
mkdir -p "$RESULTS"
mkdir -p logs

echo "=== Baseline (no MCP) ==="
python3 -m benchmarks.swebench.runner \
  --mode baseline \
  --model "$MODEL" \
  --workers 1 \
  --instance-ids $INSTANCE_IDS \
  --output-dir "$RESULTS" \
  2>&1 | tee logs/smoke_baseline.log || true

# Runner writes to output_dir/mode/predictions.jsonl so baseline goes to results/smoke/baseline
echo "=== GroundTruth MCP ==="
python3 -m benchmarks.swebench.runner \
  --mode groundtruth_mcp \
  --model "$MODEL" \
  --workers 1 \
  --instance-ids $INSTANCE_IDS \
  --output-dir "$RESULTS" \
  2>&1 | tee logs/smoke_groundtruth.log || true

echo "=== Validate MCP proof ==="
python3 scripts/swebench/validate_mcp_proof.py "$RESULTS/groundtruth_mcp" || exit 1

echo "Smoke done. Baseline: $RESULTS/baseline/predictions.jsonl"
echo "GroundTruth MCP: $RESULTS/groundtruth_mcp/predictions.jsonl"
