#!/bin/bash
# Run SWE-bench Verified evaluation on all 6 preds.json files, one at a time.
# Writes report.json next to each preds.json on the VM, then scp's the report back.
#
# Inputs (env):
#   MAX_WORKERS  default 4
#   TIMEOUT      default 1800 (30 min per instance)
#   DATASET      default SWE-bench/SWE-bench_Verified
#   SPLIT        default test
set -e

MAX_WORKERS="${MAX_WORKERS:-4}"
TIMEOUT="${TIMEOUT:-1800}"
DATASET="${DATASET:-SWE-bench/SWE-bench_Verified}"
SPLIT="${SPLIT:-test}"

RUNS=(
  "official_nolsp/repeat_1_1776858939:nolsp_r1"
  "official_nolsp/repeat_2_1776862569:nolsp_r2"
  "official_nolsp/repeat_3_1776864495:nolsp_r3"
  "lsp_readiness/repeat_1_1776868420:lsp_r1"
  "lsp_readiness/repeat_2_1776870317:lsp_r2"
  "lsp_readiness/repeat_3_1776872241:lsp_r3"
)

for entry in "${RUNS[@]}"; do
  LOCAL_SUBDIR="${entry%%:*}"
  RUN_ID="${entry##*:}"
  LOCAL_DIR="benchmarks/swebench/$LOCAL_SUBDIR"
  PREDS="$LOCAL_DIR/preds.json"
  if [ ! -f "$PREDS" ]; then
    echo "=== SKIP $RUN_ID: missing $PREDS ==="
    continue
  fi
  echo ""
  echo "===== EVAL $RUN_ID ====="
  REMOTE_DIR="/tmp/eval_$RUN_ID"

  # Upload preds.json to VM
  gcloud compute ssh gt-runner-gcp --zone=us-central1-a \
    --command "rm -rf $REMOTE_DIR && mkdir -p $REMOTE_DIR" 2>&1 | tail -1
  gcloud compute scp --zone=us-central1-a "$PREDS" \
    "gt-runner-gcp:$REMOTE_DIR/preds.json" 2>&1 | tail -2

  # Run eval on VM
  gcloud compute ssh gt-runner-gcp --zone=us-central1-a --command "
set -e
source ~/sweagent-env/bin/activate
cd $REMOTE_DIR
echo \"predictions count: \$(python3 -c 'import json; print(len(json.load(open(\\\"preds.json\\\"))))')\"
python3 -m swebench.harness.run_evaluation \\
  --dataset_name $DATASET \\
  --split $SPLIT \\
  --predictions_path preds.json \\
  --max_workers $MAX_WORKERS \\
  --run_id $RUN_ID \\
  --timeout $TIMEOUT \\
  --cache_level env 2>&1 | tail -40
ls -la *.json 2>&1 | tail -10
" 2>&1 | tail -50

  # Pull report back
  REMOTE_REPORT="$REMOTE_DIR/${DATASET##*/}.${SPLIT}.${RUN_ID}.json"
  gcloud compute ssh gt-runner-gcp --zone=us-central1-a --command "
find $REMOTE_DIR -maxdepth 2 -name '*.json' -newer $REMOTE_DIR/preds.json 2>/dev/null | head -5
" 2>&1 | tail -5
  # The exact output filename depends on swebench convention; discover + fetch
  gcloud compute ssh gt-runner-gcp --zone=us-central1-a --command "
REPORT=\$(find $REMOTE_DIR -maxdepth 2 -name '*.json' -not -name 'preds.json' -not -name 'full_eval_results.json' | head -1)
if [ -n \"\$REPORT\" ]; then
  echo REPORT_PATH=\$REPORT
  echo '---content preview---'
  python3 -c \"import json; d=json.load(open('\$REPORT')); print({k: v if not isinstance(v,list) else len(v) for k,v in d.items()})\" 2>/dev/null | head -3
fi
" 2>&1 | tail -6
  gcloud compute scp --zone=us-central1-a \
    "gt-runner-gcp:$REMOTE_DIR/*.json" "$LOCAL_DIR/" 2>&1 | grep -v preds.json | tail -5
done

echo ""
echo "===== ALL 6 EVALS DONE ====="
