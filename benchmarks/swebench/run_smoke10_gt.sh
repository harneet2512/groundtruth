#!/bin/bash
# 10-task DS V3.2 MaaS GT smoke. Uses canary_gt_ds_gt_smoke.yaml which
# injects {{gt_briefing}} and wires gt_check as PreSubmit. On completion
# emits gt_task_log.json per task + gt_smoke_summary.{md,json} at outdir.
set -u
# Fail closed on AWS env — GT SWE-bench runs are Vertex-only.
if env | grep -qE '^(AWS_|BEDROCK_|AMAZON_)'; then
    echo "ERROR: AWS/Bedrock env vars present, refusing to launch" >&2
    env | grep -E '^(AWS_|BEDROCK_|AMAZON_)' >&2
    exit 1
fi
source ~/sweagent-env/bin/activate
cd /tmp/SWE-agent
export PATH=$HOME/.local/bin:$PATH
# Route model calls through local litellm proxy on the VM. The proxy uses
# vertex_ai/ native provider with ADC from the GCE metadata server — token
# refresh is automatic, so the whole run (any duration) never 401s.
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-gt-local}"
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://172.17.0.1:4000}"
export GT_RUN_ID="${GT_RUN_ID:-smoke10_gt_$(date +%s)}"
export GT_ARM="${GT_ARM:-gt-smoke}"

CFG=/tmp/SWE-agent/config/canary_gt_ds_gt_smoke.yaml
TASKS_FILE="${TASKS_FILE:-/tmp/SWE-agent/config/smoke10_ds.txt}"
OUTDIR="${OUTDIR:-/tmp/smoke10_gt}"
BASELINE_OUTDIR="${BASELINE_OUTDIR:-/tmp/smoke10_nolsp}"

if [ ! -f "$TASKS_FILE" ]; then
    echo "ERROR: tasks file not found: $TASKS_FILE" >&2
    exit 1
fi

if [ $# -gt 0 ]; then
    TASKS="$*"
else
    TASKS="$(grep -vE '^\s*(#|$)' "$TASKS_FILE" | tr '\n' ' ')"
fi

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"
echo "=== 10-task smoke (GT) $(date) ===" | tee "$OUTDIR/master.log"
echo "Tasks: $TASKS" | tee -a "$OUTDIR/master.log"

setsid bash /home/Lenovo/gt_telemetry_scraper.sh "$OUTDIR" \
    > "$OUTDIR/scraper.log" 2>&1 < /dev/null &
SCRAPER_PID=$!
echo "scraper PID=$SCRAPER_PID" | tee -a "$OUTDIR/master.log"

make_task_bundle() {
    local task_id="$1"
    local telem_dir="$2"
    local task_bundle="$OUTDIR/$task_id/groundtruth_bundle"
    rm -rf "$task_bundle"
    mkdir -p "$task_bundle"
    cp -a /tmp/SWE-agent/tools/groundtruth/. "$task_bundle"/
    mkdir -p "$task_bundle/bin"
    cat > "$task_bundle/bin/gt_identity.env" <<EOF
GT_ARM=$GT_ARM
GT_RUN_ID=$GT_RUN_ID
GT_INSTANCE_ID=$task_id
GT_TELEMETRY_DIR=$telem_dir
EOF
    printf '%s\n' "$task_bundle"
}

PIDS=()
for T in $TASKS; do
    mkdir -p "$OUTDIR/$T"
    export GT_TELEMETRY_DIR="$OUTDIR/$T"
    export GT_INSTANCE_ID="$T"
    TASK_BUNDLE="$(make_task_bundle "$T" "$GT_TELEMETRY_DIR")"
    PATCHED="$OUTDIR/$T/cfg.yaml"
    python3 - "$CFG" "$PATCHED" "$GT_ARM" "$GT_RUN_ID" "$T" "$GT_TELEMETRY_DIR" "$TASK_BUNDLE" <<'PY'
import sys, yaml
src, dst, arm, run_id, iid, tdir, bundle_path = sys.argv[1:8]
with open(src) as f:
    cfg = yaml.safe_load(f)
env = cfg["agent"]["tools"].setdefault("env_variables", {})
env["GT_ARM"] = arm
env["GT_RUN_ID"] = run_id
env["GT_INSTANCE_ID"] = iid
env["GT_TELEMETRY_DIR"] = tdir
for bundle in cfg["agent"]["tools"].get("bundles", []):
    if isinstance(bundle, dict) and bundle.get("path", "").endswith("groundtruth"):
        bundle["path"] = bundle_path
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

bash /home/Lenovo/gt_telemetry_scraper.sh "$OUTDIR" --once \
    >> "$OUTDIR/scraper.log" 2>&1 || true
kill "$SCRAPER_PID" 2>/dev/null || true
pkill -P "$SCRAPER_PID" 2>/dev/null || true

# Plan §3A + §3B: emit per-task logs and cross-task smoke summary.
python3 /tmp/SWE-agent/config/gt_canary_report.py \
    --outdir "$OUTDIR" \
    --arm "$GT_ARM" \
    --run-id "$GT_RUN_ID" \
    --emit-task-logs \
    --emit-smoke-summary \
    --baseline-outdir "$BASELINE_OUTDIR" \
    || true

echo "=== ALL DONE $(date) ===" | tee -a "$OUTDIR/master.log"
echo "summary: $OUTDIR/gt_smoke_summary.md"
