#!/bin/bash
if env | grep -qE '^(AWS_|BEDROCK_|AMAZON_)'; then
  echo "ERROR: AWS/Bedrock env vars present, refusing to launch" >&2
  env | grep -E '^(AWS_|BEDROCK_|AMAZON_)' >&2
  exit 1
fi
source ~/sweagent-env/bin/activate
cd /tmp/SWE-agent
export PATH=$HOME/.local/bin:$PATH
# Route model calls through the local litellm proxy (vertex_ai native provider
# + ADC from GCE metadata). The proxy handles token refresh for the entire
# run duration, so we never see the 60-min AuthErr burst that plagued the
# pre-v11 direct-to-Vertex launcher.
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-gt-local}"
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://172.17.0.1:4000}"
export GT_RUN_ID="${GT_RUN_ID:-smoke5gcp_lsp_$(date +%s)}"
export GT_ARM="${GT_ARM:-gt-hybrid}"
export GT_LSP_ENABLED=1

CFG=/tmp/SWE-agent/config/canary_gt_ds_lsp.yaml
TASKS="${TASKS:-astropy__astropy-12907 astropy__astropy-13033 astropy__astropy-13236 astropy__astropy-13398 astropy__astropy-13453}"
OUTDIR=/tmp/smoke5_lsp
rm -rf $OUTDIR
mkdir -p $OUTDIR

echo "=== 5-task smoke LSP-hybrid $(date) ===" | tee $OUTDIR/master.log

# Start telemetry scraper BEFORE tasks. Detached so wait doesn't block on it.
setsid bash /home/Lenovo/gt_telemetry_scraper.sh "$OUTDIR" > "$OUTDIR/scraper.log" 2>&1 < /dev/null &
SCRAPER_PID=$!
echo "scraper PID=$SCRAPER_PID" | tee -a $OUTDIR/master.log

make_task_bundle() {
  local task_id="$1"
  local telem_dir="$2"
  local task_bundle="$OUTDIR/$task_id/groundtruth_bundle"
  rm -rf "$task_bundle"
  mkdir -p "$task_bundle"
  cp -a /tmp/SWE-agent/tools/groundtruth/. "$task_bundle"/
  mkdir -p "$task_bundle/src"
  cp -a /home/Lenovo/groundtruth_src/groundtruth "$task_bundle/src/"
  mkdir -p "$task_bundle/bin"
  cat > "$task_bundle/bin/gt_identity.env" <<EOF
GT_ARM=$GT_ARM
GT_RUN_ID=$GT_RUN_ID
GT_INSTANCE_ID=$task_id
GT_TELEMETRY_DIR=$telem_dir
EOF
  printf '%s\n' "$task_bundle"
}

# Per-task YAML patch injects GT_ARM/GT_RUN_ID/GT_INSTANCE_ID/GT_TELEMETRY_DIR
# into tools.env_variables so the in-container hook sees them. Host shell env
# vars do NOT propagate into the sweagent Docker runtime. canary_gt_ds_lsp.yaml
# already has GT_LSP_ENABLED=1 baked in.
PIDS=()
for T in $TASKS; do
  mkdir -p $OUTDIR/$T
  export GT_TELEMETRY_DIR=$OUTDIR/$T
  export GT_INSTANCE_ID=$T
  TASK_BUNDLE="$(make_task_bundle "$T" "$GT_TELEMETRY_DIR")"
  PATCHED=$OUTDIR/$T/cfg.yaml
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
    --instances.filter $T \
    --output_dir $OUTDIR/$T \
    > $OUTDIR/$T/run.log 2>&1 &
  PIDS+=($!)
  echo "  $T PID=$!" | tee -a $OUTDIR/master.log
done

echo "All 5 launched. Waiting..." | tee -a $OUTDIR/master.log
for p in "${PIDS[@]}"; do wait "$p" 2>/dev/null; done

bash /home/Lenovo/gt_telemetry_scraper.sh "$OUTDIR" --once >> "$OUTDIR/scraper.log" 2>&1 || true
kill "$SCRAPER_PID" 2>/dev/null || true
pkill -P "$SCRAPER_PID" 2>/dev/null || true

# Emit per-task logs + cross-task smoke summary. `--hybrid` enforces the
# lsp_promotion>=1 SHOULD gate on edited tasks. `|| true` so a report
# failure never masks the actual run exit status.
python3 /tmp/SWE-agent/config/gt_canary_report.py \
  --outdir "$OUTDIR" \
  --arm "$GT_ARM" \
  --run-id "$GT_RUN_ID" \
  --hybrid \
  --max-steps 150 \
  --emit-task-logs \
  --emit-smoke-summary \
  >> "$OUTDIR/master.log" 2>&1 || true

echo "=== ALL DONE $(date) ===" | tee -a $OUTDIR/master.log
echo "summary: $OUTDIR/gt_smoke_summary.md" | tee -a $OUTDIR/master.log
