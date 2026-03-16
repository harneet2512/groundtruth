#!/usr/bin/env bash
# Stage B: 10-20 Lite tasks, 4 workers, both conditions. Validate MCP proof.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$REPO_DIR"

MODEL="${MODEL_NAME_EXACT:-}"
if [ -z "$MODEL" ]; then
  MODEL=$(python3 scripts/swebench/resolve_model.py --json | python3 -c "import sys,json; print(json.load(sys.stdin)['MODEL_NAME_EXACT'])")
  export MODEL_NAME_EXACT="$MODEL"
fi

# Limit to 20 tasks: use scripts/swebench/lite_task_ids.txt if present, else a small default set
if [ -f scripts/swebench/lite_task_ids.txt ]; then
  INSTANCE_IDS=$(head -20 scripts/swebench/lite_task_ids.txt | tr '\n' ' ')
else
  # Default: 5 tasks for stability (user can create lite_task_ids.txt for more)
  INSTANCE_IDS="django__django-11039 django__django-11049 django__django-11055 sympy__sympy-13956 transformers-8765"
fi

RESULTS="${RESULTS:-benchmarks/swebench/results/stability}"
mkdir -p "$RESULTS" logs
WORKERS="${WORKERS:-4}"

echo "=== Baseline ($WORKERS workers) ==="
python3 -m benchmarks.swebench.runner \
  --mode baseline \
  --model "$MODEL" \
  --workers "$WORKERS" \
  --output-dir "$RESULTS" \
  $([ -n "$INSTANCE_IDS" ] && echo "--instance-ids $INSTANCE_IDS") \
  2>&1 | tee logs/stability_baseline.log || true

echo "=== GroundTruth MCP ($WORKERS workers) ==="
python3 -m benchmarks.swebench.runner \
  --mode groundtruth_mcp \
  --model "$MODEL" \
  --workers "$WORKERS" \
  --output-dir "$RESULTS" \
  $([ -n "$INSTANCE_IDS" ] && echo "--instance-ids $INSTANCE_IDS") \
  2>&1 | tee logs/stability_groundtruth.log || true

echo "=== Validate MCP proof ==="
python3 scripts/swebench/validate_mcp_proof.py "$RESULTS/groundtruth_mcp" || exit 1

echo "Stability done."
