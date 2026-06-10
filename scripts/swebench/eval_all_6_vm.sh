#!/bin/bash
# VM-side driver: run SWE-bench Verified eval on each of 6 preds.json files,
# one at a time to avoid docker contention. Writes a status line per run.
#
# Assumes /tmp/eval_runs/<run_id>/preds.json exists for each entry.
set -u

MAX_WORKERS="${MAX_WORKERS:-4}"
TIMEOUT="${TIMEOUT:-1800}"
DATASET="${DATASET:-SWE-bench/SWE-bench_Verified}"
SPLIT="${SPLIT:-test}"
ROOT="${ROOT:-/tmp/eval_runs}"

RUNS=(nolsp_r1 nolsp_r2 nolsp_r3 lsp_r1 lsp_r2 lsp_r3)

source ~/sweagent-env/bin/activate
cd "$ROOT"
echo "=== eval_all_6 started $(date -u) ==="

for RID in "${RUNS[@]}"; do
  D="$ROOT/$RID"
  if [ ! -f "$D/preds.json" ]; then
    echo "[$(date -u)] SKIP $RID: no preds.json at $D"
    continue
  fi
  echo ""
  echo "[$(date -u)] ===== EVAL $RID ====="
  cd "$D"
  # Wipe any prior eval state for this run id (so swebench re-runs)
  rm -rf ./logs ./results ./*.json.tmp 2>/dev/null || true
  python3 -m swebench.harness.run_evaluation \
    --dataset_name "$DATASET" \
    --split "$SPLIT" \
    --predictions_path preds.json \
    --max_workers "$MAX_WORKERS" \
    --run_id "$RID" \
    --timeout "$TIMEOUT" \
    --cache_level env \
    > "$D/eval.log" 2>&1
  EC=$?
  echo "[$(date -u)] $RID exit=$EC"
  ls -la "$D"/*.json 2>/dev/null | tail -5
done

echo ""
echo "=== eval_all_6 finished $(date -u) ==="
