#!/bin/bash
# Level 2 Canary: GT + DeepSeek V3.2 (Vertex AI MaaS), 3 runs in parallel
set -e

FILTER='astropy__astropy-12907|astropy__astropy-13033|astropy__astropy-13236|astropy__astropy-13398|astropy__astropy-13453|astropy__astropy-13579|astropy__astropy-13977|astropy__astropy-14096|astropy__astropy-14182|astropy__astropy-14309'
CONFIG=~/SWE-agent/config/canary_gt_ds.yaml
OUTDIR=/tmp/canary_ds32
cd ~/SWE-agent
export PATH=$HOME/.local/bin:$PATH

# Set API key to GCP access token for the OpenAI-compatible endpoint
export OPENAI_API_KEY=$(gcloud auth print-access-token)
# Vertex MaaS global endpoint (litellm openai/ prefix reaches it via base URL).
# GCP_PROJECT_ID must be provided by the launch environment; no repo default.
: "${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set before launching (export it or source ~/gt_identity.env)}"
export OPENAI_API_BASE="https://aiplatform.googleapis.com/v1beta1/projects/${GCP_PROJECT_ID}/locations/global/endpoints/openapi"
export GT_RUN_ID="${GT_RUN_ID:-canary3x10_$(date +%s)}"

mkdir -p $OUTDIR
echo "=== 3-Run GT + DeepSeek V3.2 Canary: $(date) ===" | tee $OUTDIR/master.log
echo "Model: deepseek-ai/deepseek-v3.2-maas via Vertex AI MaaS" | tee -a $OUTDIR/master.log
echo "10 tasks per run, 3 runs in parallel" | tee -a $OUTDIR/master.log

for RUN in 1 2 3; do
  RUN_OUT=$OUTDIR/run$RUN
  mkdir -p $RUN_OUT
  echo "Launching run $RUN..." | tee -a $OUTDIR/master.log

  python3 -m sweagent run-batch \
    --config $CONFIG \
    --instances.type swe_bench \
    --instances.subset verified \
    --instances.split test \
    --instances.filter "$FILTER" \
    --output_dir $RUN_OUT \
    > $RUN_OUT/run.log 2>&1 &

  eval "PID_$RUN=$!"
  echo "  Run $RUN PID=$(eval echo \$PID_$RUN)" | tee -a $OUTDIR/master.log
done

echo "" | tee -a $OUTDIR/master.log
echo "All 3 runs launched. Waiting..." | tee -a $OUTDIR/master.log

for RUN in 1 2 3; do
  PID=$(eval echo \$PID_$RUN)
  wait $PID
  EXIT=$?
  echo "Run $RUN done (exit=$EXIT) at $(date)" | tee -a $OUTDIR/master.log
done

echo "" | tee -a $OUTDIR/master.log
echo "=== ALL 3 RUNS COMPLETE $(date) ===" | tee -a $OUTDIR/master.log

# Auto-eval
echo "Starting eval..." | tee -a $OUTDIR/master.log
for RUN in 1 2 3; do
  echo "Eval run $RUN..." | tee -a $OUTDIR/master.log
  python3 -m swebench.harness.run_evaluation \
    --predictions_path $OUTDIR/run$RUN/preds.json \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --run_id ds32_run$RUN \
    --max_workers 3 \
    > $OUTDIR/run$RUN/eval.log 2>&1 &
done
wait
echo "=== EVAL COMPLETE $(date) ===" | tee -a $OUTDIR/master.log

# Results
for RUN in 1 2 3; do
  grep 'resolved\|unresolved' $OUTDIR/run$RUN/eval.log 2>/dev/null | tee -a $OUTDIR/master.log
done
