#!/bin/bash
source ~/sweagent-env/bin/activate
cd /tmp/SWE-agent
export PATH=$HOME/.local/bin:$PATH
export OPENAI_API_KEY=$(gcloud auth print-access-token)
# Vertex MaaS global endpoint (litellm openai/ prefix reaches it via base URL).
# GCP_PROJECT_ID must be provided by the launch environment or identity file;
# intentionally no default here so the project id never appears in the repo.
: "${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set before launching (export it or source ~/gt_identity.env)}"
export OPENAI_API_BASE="https://aiplatform.googleapis.com/v1beta1/projects/${GCP_PROJECT_ID}/locations/global/endpoints/openapi"
export GT_RUN_ID="${GT_RUN_ID:-smoke5gcp_nolsp_$(date +%s)}"
export GT_ARM="${GT_ARM:-gt-nolsp}"

TASKS="astropy__astropy-12907 astropy__astropy-13033 astropy__astropy-13236 astropy__astropy-13398 astropy__astropy-13453"
OUTDIR=/tmp/smoke5_nolsp
rm -rf $OUTDIR
mkdir -p $OUTDIR

echo "=== 5-task smoke no-LSP $(date) ===" | tee $OUTDIR/master.log

for T in $TASKS; do
  mkdir -p $OUTDIR/$T
  export GT_TELEMETRY_DIR=$OUTDIR/$T
  python3 -m sweagent run-batch \
    --config /tmp/SWE-agent/config/canary_gt_ds.yaml \
    --instances.type swe_bench --instances.subset verified --instances.split test \
    --instances.filter $T \
    --output_dir $OUTDIR/$T \
    > $OUTDIR/$T/run.log 2>&1 &
  echo "  $T PID=$!" | tee -a $OUTDIR/master.log
done

echo "All 5 launched. Waiting..." | tee -a $OUTDIR/master.log
wait
echo "=== ALL DONE $(date) ===" | tee -a $OUTDIR/master.log
