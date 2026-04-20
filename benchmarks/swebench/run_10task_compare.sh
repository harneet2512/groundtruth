#!/bin/bash
set -euo pipefail

# Fixed 10-task GT comparison harness.
# Runs:
#   1. one baseline pass
#   2. three repeats of the non-LSP GT arm
#   3. three repeats of the LSP-hybrid arm
# Then emits a repeat summary report.

ROOT="${ROOT:-/tmp/gt_eval10}"
TASKS_FILE="${TASKS_FILE:-/tmp/SWE-agent/config/smoke10_ds.txt}"
REPEATS="${REPEATS:-3}"
BASELINE_DIR="${BASELINE_DIR:-$ROOT/baseline}"
NOLSP_PREFIX="${NOLSP_PREFIX:-$ROOT/gt_nolsp}"
LSP_PREFIX="${LSP_PREFIX:-$ROOT/gt_lsp}"
SUMMARY_JSON="${SUMMARY_JSON:-$ROOT/gt_repeat_summary.json}"
SUMMARY_MD="${SUMMARY_MD:-$ROOT/gt_repeat_summary.md}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT"

mkdir -p "$ROOT"
echo "=== GT 10-task repeat comparison $(date) ==="
echo "ROOT=$ROOT"
echo "TASKS_FILE=$TASKS_FILE"
echo "REPEATS=$REPEATS"

if [ ! -f "$TASKS_FILE" ]; then
  echo "ERROR: tasks file not found: $TASKS_FILE" >&2
  exit 1
fi

echo "[1/3] baseline run"
TASKS_FILE="$TASKS_FILE" \
OUTDIR="$BASELINE_DIR" \
GT_RUN_ID="baseline_$(date +%s)" \
bash "$SCRIPT_DIR/run_smoke10_nolsp.sh"

for i in $(seq 1 "$REPEATS"); do
  echo "[2/3] nolsp repeat $i"
  RUN_DIR="${NOLSP_PREFIX}_r${i}"
  TASKS_FILE="$TASKS_FILE" \
  OUTDIR="$RUN_DIR" \
  BASELINE_OUTDIR="$BASELINE_DIR" \
  GT_RUN_ID="gt_nolsp_r${i}_$(date +%s)" \
  GT_ARM="gt-nolsp" \
  bash "$SCRIPT_DIR/run_smoke10_gt.sh"

  echo "[3/3] lsp repeat $i"
  RUN_DIR="${LSP_PREFIX}_r${i}"
  TASKS_FILE="$TASKS_FILE" \
  OUTDIR="$RUN_DIR" \
  BASELINE_OUTDIR="$BASELINE_DIR" \
  GT_RUN_ID="gt_lsp_r${i}_$(date +%s)" \
  GT_ARM="gt-hybrid" \
  bash "$SCRIPT_DIR/run_smoke10_lsp.sh"
done

python3 "$SCRIPT_DIR/gt_repeat_summary.py" \
  --baseline "$BASELINE_DIR" \
  --nolsp "${NOLSP_PREFIX}_r1" "${NOLSP_PREFIX}_r2" "${NOLSP_PREFIX}_r3" \
  --lsp "${LSP_PREFIX}_r1" "${LSP_PREFIX}_r2" "${LSP_PREFIX}_r3" \
  --output "$SUMMARY_JSON" \
  --markdown "$SUMMARY_MD"

echo "summary_json=$SUMMARY_JSON"
echo "summary_md=$SUMMARY_MD"
