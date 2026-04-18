#!/bin/bash
# 10-task DS V3.2 MaaS baseline (no GT). Mirrors run_5smoke_nolsp.sh but
# reads task IDs from smoke10_ds.txt and uses canary_gt_ds_nolsp_baseline.yaml.
set -u
source ~/sweagent-env/bin/activate
cd /tmp/SWE-agent
export PATH=$HOME/.local/bin:$PATH
export OPENAI_API_KEY=$(gcloud auth print-access-token)
: "${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set before launching (export it or source ~/gt_identity.env)}"
export OPENAI_API_BASE="https://aiplatform.googleapis.com/v1beta1/projects/${GCP_PROJECT_ID}/locations/global/endpoints/openapi"
export GT_RUN_ID="${GT_RUN_ID:-smoke10_nolsp_$(date +%s)}"
export GT_ARM="${GT_ARM:-baseline-nolsp}"

CFG=/tmp/SWE-agent/config/canary_gt_ds_nolsp_baseline.yaml
TASKS_FILE="${TASKS_FILE:-/tmp/SWE-agent/config/smoke10_ds.txt}"
OUTDIR="${OUTDIR:-/tmp/smoke10_nolsp}"

if [ ! -f "$TASKS_FILE" ]; then
    echo "ERROR: tasks file not found: $TASKS_FILE" >&2
    exit 1
fi

# Optional: caller may pass a single task id as $1 to re-run just that task.
if [ $# -gt 0 ]; then
    TASKS="$*"
else
    TASKS="$(grep -vE '^\s*(#|$)' "$TASKS_FILE" | tr '\n' ' ')"
fi

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"
echo "=== 10-task smoke (baseline) $(date) ===" | tee "$OUTDIR/master.log"
echo "Tasks: $TASKS" | tee -a "$OUTDIR/master.log"

PIDS=()
for T in $TASKS; do
    mkdir -p "$OUTDIR/$T"
    export GT_TELEMETRY_DIR="$OUTDIR/$T"
    export GT_INSTANCE_ID="$T"
    PATCHED="$OUTDIR/$T/cfg.yaml"
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
        --instances.filter "$T" \
        --output_dir "$OUTDIR/$T" \
        > "$OUTDIR/$T/run.log" 2>&1 &
    PIDS+=($!)
    echo "  $T PID=$!" | tee -a "$OUTDIR/master.log"
done

echo "All launched. Waiting..." | tee -a "$OUTDIR/master.log"
for p in "${PIDS[@]}"; do wait "$p" 2>/dev/null || true; done

echo "=== ALL DONE $(date) ===" | tee -a "$OUTDIR/master.log"
