#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$REPO_DIR"

SUITE_FILE="${SUITE_FILE:-scripts/swebench/frozen_gt_astropy10.txt}"
MODEL="${MODEL_NAME_EXACT:-}"
if [ -z "$MODEL" ]; then
  MODEL=$(python3 scripts/swebench/resolve_model.py --json | python3 -c "import sys,json; print(json.load(sys.stdin)['MODEL_NAME_EXACT'])")
  export MODEL_NAME_EXACT="$MODEL"
fi

OUTDIR="${OUTDIR:-benchmarks/swebench/results/gt_finalization}"
mkdir -p "$OUTDIR"

echo "=== GT Finalization Freeze ==="
echo "Repo:   $REPO_DIR"
echo "Suite:  $SUITE_FILE"
echo "Model:  $MODEL"
echo "Output: $OUTDIR"

python3 scripts/swebench/gt_finalization.py freeze-state \
  --suite-file "$SUITE_FILE" \
  --model "$MODEL" \
  --out "$OUTDIR/freeze_state.json"

NOLSP_RUNS="${NOLSP_RUNS:-}"
LSP_RUNS="${LSP_RUNS:-}"
BASELINE_DIR="${BASELINE_DIR:-}"

if [ -n "$NOLSP_RUNS" ]; then
  IFS=',' read -r -a NOLSP_DIRS <<< "$NOLSP_RUNS"
  echo
  echo "=== NOLSP Readiness ==="
  for dir in "${NOLSP_DIRS[@]}"; do
    python3 scripts/swebench/gt_finalization.py readiness --summary-dir "$dir"
  done
fi

if [ -n "$LSP_RUNS" ]; then
  IFS=',' read -r -a LSP_DIRS <<< "$LSP_RUNS"
  echo
  echo "=== LSP-HYBRID Readiness ==="
  for dir in "${LSP_DIRS[@]}"; do
    python3 scripts/swebench/gt_finalization.py readiness --summary-dir "$dir"
  done
fi

if [ -n "$NOLSP_RUNS" ] && [ -n "$LSP_RUNS" ]; then
  echo
  echo "=== Final Comparison ==="
  COMPARE_ARGS=(
    python3 scripts/swebench/gt_finalization.py compare
    --group "nolsp=$NOLSP_RUNS"
    --group "lsp_hybrid=$LSP_RUNS"
    --suite-file "$SUITE_FILE"
    --json-out "$OUTDIR/final_compare.json"
    --md-out "$OUTDIR/final_compare.md"
  )
  if [ -n "$BASELINE_DIR" ]; then
    COMPARE_ARGS+=(--baseline-dir "$BASELINE_DIR")
  fi
  "${COMPARE_ARGS[@]}"
  echo
  echo "Comparison written:"
  echo "  $OUTDIR/final_compare.json"
  echo "  $OUTDIR/final_compare.md"
else
  echo
  echo "Set NOLSP_RUNS and LSP_RUNS (comma-separated run directories) to emit the final comparison."
fi
