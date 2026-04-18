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
export GT_RUN_ID="${GT_RUN_ID:-smoke5gcp_lsp_$(date +%s)}"
export GT_ARM="${GT_ARM:-gt-hybrid}"
export GT_LSP_ENABLED=1

CFG=/tmp/SWE-agent/config/canary_gt_ds_lsp.yaml
TASKS="astropy__astropy-12907 astropy__astropy-13033 astropy__astropy-13236 astropy__astropy-13398 astropy__astropy-13453"
OUTDIR=/tmp/smoke5_lsp
rm -rf $OUTDIR
mkdir -p $OUTDIR

echo "=== 5-task smoke LSP-hybrid $(date) ===" | tee $OUTDIR/master.log

# Per-task YAML patch injects GT_ARM/GT_RUN_ID/GT_INSTANCE_ID/GT_TELEMETRY_DIR
# into tools.env_variables so the in-container hook sees them. Host shell env
# vars do NOT propagate into the sweagent Docker runtime. canary_gt_ds_lsp.yaml
# already has GT_LSP_ENABLED=1 baked in.
for T in $TASKS; do
  mkdir -p $OUTDIR/$T
  export GT_TELEMETRY_DIR=$OUTDIR/$T
  export GT_INSTANCE_ID=$T
  PATCHED=$OUTDIR/$T/cfg.yaml
  python3 - "$CFG" "$PATCHED" "$GT_ARM" "$GT_RUN_ID" "$T" "$GT_TELEMETRY_DIR" <<'PY'
import sys, yaml
src, dst, arm, run_id, iid, tdir = sys.argv[1:7]
with open(src) as f:
    cfg = yaml.safe_load(f)
env = cfg["agent"]["tools"].setdefault("env_variables", {})
env["GT_ARM"] = arm
env["GT_RUN_ID"] = run_id
env["GT_INSTANCE_ID"] = iid
env["GT_TELEMETRY_DIR"] = tdir
with open(dst, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
PY
  python3 -m sweagent run-batch \
    --config "$PATCHED" \
    --instances.type swe_bench --instances.subset verified --instances.split test \
    --instances.filter $T \
    --output_dir $OUTDIR/$T \
    > $OUTDIR/$T/run.log 2>&1 &
  echo "  $T PID=$!" | tee -a $OUTDIR/master.log
done

echo "All 5 launched. Waiting..." | tee -a $OUTDIR/master.log
wait
echo "=== ALL DONE $(date) ===" | tee -a $OUTDIR/master.log
