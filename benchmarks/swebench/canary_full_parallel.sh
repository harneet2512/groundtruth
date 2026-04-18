#!/bin/bash
# Full parallel canary: 10 tasks × 3 runs = 30 instances
# Run 10 tasks at a time (one full run), wait, repeat for runs 2 and 3
set -e

TASKS=(
  astropy__astropy-12907
  astropy__astropy-13033
  astropy__astropy-13236
  astropy__astropy-13398
  astropy__astropy-13453
  astropy__astropy-13579
  astropy__astropy-13977
  astropy__astropy-14096
  astropy__astropy-14182
  astropy__astropy-14309
)

CONFIG=~/SWE-agent/config/canary_gt_ds.yaml
OUTDIR=/tmp/canary_final
cd ~/SWE-agent
export PATH=$HOME/.local/bin:$PATH
export OPENAI_API_KEY=$(gcloud auth print-access-token)
# Vertex MaaS global endpoint (litellm openai/ prefix reaches it via base URL).
# GCP_PROJECT_ID must be provided by the launch environment; no repo default.
: "${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set before launching (export it or source ~/gt_identity.env)}"
export OPENAI_API_BASE="https://aiplatform.googleapis.com/v1beta1/projects/${GCP_PROJECT_ID}/locations/global/endpoints/openapi"
export GT_RUN_ID="${GT_RUN_ID:-canary10x3_$(date +%s)}"

mkdir -p $OUTDIR
echo "=== Full Parallel GT + DeepSeek V3.2: $(date) ===" | tee $OUTDIR/master.log
echo "10 tasks × 3 runs, all tasks parallel within each run" | tee -a $OUTDIR/master.log

for RUN in 1 2 3; do
  echo "" | tee -a $OUTDIR/master.log
  echo "=== RUN $RUN starting $(date) ===" | tee -a $OUTDIR/master.log

  # Refresh token for each run (they expire after 1h)
  export OPENAI_API_KEY=$(gcloud auth print-access-token)

  PIDS=()
  for TASK in "${TASKS[@]}"; do
    TASK_OUT=$OUTDIR/run${RUN}/${TASK}
    mkdir -p $TASK_OUT

    python3 -m sweagent run-batch \
      --config $CONFIG \
      --instances.type swe_bench \
      --instances.subset verified \
      --instances.split test \
      --instances.filter "$TASK" \
      --output_dir $TASK_OUT \
      > $TASK_OUT/run.log 2>&1 &

    PIDS+=($!)
  done

  echo "  Launched ${#PIDS[@]} tasks in parallel" | tee -a $OUTDIR/master.log

  # Wait for all tasks in this run
  for i in "${!PIDS[@]}"; do
    wait ${PIDS[$i]} 2>/dev/null
  done

  echo "  Run $RUN complete $(date)" | tee -a $OUTDIR/master.log

  # Merge predictions for this run
  python3 -c "
import json, glob, os
preds = {}
for f in glob.glob('$OUTDIR/run${RUN}/*/preds.json'):
    d = json.load(open(f))
    preds.update(d)
out = '$OUTDIR/run${RUN}/preds.json'
json.dump(preds, open(out,'w'))
print(f'  Merged {len(preds)} predictions for run $RUN')
for k in sorted(preds):
    p = len(preds[k].get('model_patch','') or '')
    print(f'    {k.split(\"-\")[-1]}: {p} chars')
" 2>&1 | tee -a $OUTDIR/master.log
done

echo "" | tee -a $OUTDIR/master.log
echo "=== ALL RUNS COMPLETE $(date) ===" | tee -a $OUTDIR/master.log

# Eval all 3 runs in parallel
echo "Starting eval..." | tee -a $OUTDIR/master.log
for RUN in 1 2 3; do
  python3 -m swebench.harness.run_evaluation \
    --predictions_path $OUTDIR/run${RUN}/preds.json \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --run_id final_run${RUN} \
    --max_workers 3 \
    > $OUTDIR/run${RUN}/eval.log 2>&1 &
done
wait

echo "=== EVAL COMPLETE $(date) ===" | tee -a $OUTDIR/master.log
for RUN in 1 2 3; do
  grep 'resolved\|unresolved' $OUTDIR/run${RUN}/eval.log 2>/dev/null | tee -a $OUTDIR/master.log
done
