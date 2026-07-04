#!/bin/bash
# Lane B — LSP-hybrid readiness re-probe (1 task, declared READINESS_SMOKE).
#
# Runs one SWE-bench task under arm=gt-lsp-hybrid to confirm the post-sync LSP
# bundle emits material_edit / ack_armed / steer_delivered / ack_engagement
# properly. Uses the same watchdog + classify wrappers as Lane A's official
# launcher, but output dir is under /tmp/gt_lane_b_lsp_readiness/ and the final
# classify is --declared READINESS_SMOKE so it never counts as official.
#
# Inputs (env):
#   TASK                default astropy__astropy-12907 (the same task Lane B
#                       used pre-sync — comparable)
#   OUTROOT             /tmp/gt_lane_b_lsp_readiness   (default)
#   STALL_MINUTES       15                              (default)
#   WATCHDOG_POLL_SECS  60                              (default)
#   FINALIZATION_PY     /home/Lenovo/gt_finalization.py (default)
set -e

TASK="${TASK:-astropy__astropy-12907}"
OUTROOT="${OUTROOT:-/tmp/gt_lane_b_lsp_readiness}"
STALL_MINUTES="${STALL_MINUTES:-15}"
WATCHDOG_POLL_SECS="${WATCHDOG_POLL_SECS:-60}"
FINALIZATION_PY="${FINALIZATION_PY:-/home/Lenovo/gt_finalization.py}"

if env | grep -qE '^(AWS_|BEDROCK_|AMAZON_)'; then
  echo "ERROR: AWS/Bedrock env vars present, refusing to launch" >&2
  exit 1
fi
if [ ! -f "$FINALIZATION_PY" ]; then
  echo "ERROR: finalization helper $FINALIZATION_PY not found" >&2
  exit 1
fi

STAMP=$(date +%s)
OUTDIR="$OUTROOT/probe_resynced_$STAMP"
GT_RUN_ID="lane_b_probe_resynced_$STAMP"

source ~/sweagent-env/bin/activate
cd /tmp/SWE-agent
export PATH=$HOME/.local/bin:$PATH
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-gt-local}"
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://172.17.0.1:4000}"
export GT_ARM="gt-lsp-hybrid"
export GT_LSP_ENABLED="1"
CFG=/tmp/SWE-agent/config/canary_gt_ds.yaml

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"
KILLED_LOG="$OUTDIR/killed_tasks.jsonl"
touch "$KILLED_LOG"

# Hard cleanup: rm -f covers both running and stopped containers. See the
# same fix in run_official_nolsp_repeat.sh for why.
IDS="$(docker ps -a --format '{{.ID}} {{.Names}}' | awk -v t="$TASK" 'index($0,t) {print $1}')"
if [ -n "$IDS" ]; then
  echo "Removing lingering containers (running or stopped) for $TASK" | tee -a "$OUTDIR/master.log"
  printf '%s\n' "$IDS" | xargs -r docker rm -f >/dev/null 2>&1 || true
fi

echo "=== Lane B LSP readiness re-probe $GT_RUN_ID $(date) ===" | tee "$OUTDIR/master.log"
echo "task: $TASK" | tee -a "$OUTDIR/master.log"
echo "outdir: $OUTDIR" | tee -a "$OUTDIR/master.log"
echo "stall_minutes: $STALL_MINUTES  poll_secs: $WATCHDOG_POLL_SECS" | tee -a "$OUTDIR/master.log"
echo "bundle source: /tmp/SWE-agent/tools/groundtruth (canonical; GT_LSP_ENABLED=1 selects hybrid path)" | tee -a "$OUTDIR/master.log"

setsid bash /home/Lenovo/gt_telemetry_scraper.sh "$OUTDIR" > "$OUTDIR/scraper.log" 2>&1 < /dev/null &
SCRAPER_PID=$!
echo "scraper PID=$SCRAPER_PID" | tee -a "$OUTDIR/master.log"

TASK_DIR="$OUTDIR/$TASK"
mkdir -p "$TASK_DIR"
: > "$TASK_DIR/gt_hook_telemetry.jsonl"
TASK_BUNDLE="$TASK_DIR/groundtruth_bundle"
rm -rf "$TASK_BUNDLE"; mkdir -p "$TASK_BUNDLE"
cp -a /tmp/SWE-agent/tools/groundtruth/. "$TASK_BUNDLE"/
mkdir -p "$TASK_BUNDLE/src"
cp -a /home/Lenovo/groundtruth_src/groundtruth "$TASK_BUNDLE/src/" 2>/dev/null || true
mkdir -p "$TASK_BUNDLE/bin"
cat > "$TASK_BUNDLE/bin/gt_identity.env" <<EOF
GT_ARM=$GT_ARM
GT_RUN_ID=$GT_RUN_ID
GT_INSTANCE_ID=$TASK
GT_TELEMETRY_DIR=$TASK_DIR
EOF
cat > "$TASK_BUNDLE/bin/gt_budget.state.json" <<EOF
{"scope":"${GT_RUN_ID}__${TASK}__${GT_ARM}","orient":{"count":0,"limit":2,"exhausted":false},"lookup":{"count":0,"limit":3,"exhausted":false},"impact":{"count":0,"limit":2,"exhausted":false},"check":{"count":0,"limit":10,"exhausted":false},"orient_exhausted":false,"initialized":true,"source":"launcher_bootstrap"}
EOF
cat > "$TASK_BUNDLE/bin/gt_startup_trace.jsonl" <<EOF
{"event":"startup_enter","ts":0,"scope":"${GT_RUN_ID}__${TASK}__${GT_ARM}","run_id":"$GT_RUN_ID","arm":"$GT_ARM","instance_id":"$TASK","source":"launcher"}
EOF

PATCHED="$TASK_DIR/cfg.yaml"
python3 - "$CFG" "$PATCHED" "$GT_ARM" "$GT_RUN_ID" "$TASK" "$TASK_DIR" "$TASK_BUNDLE" <<'PY'
import sys, yaml
src, dst, arm, run_id, iid, tdir, bundle_path = sys.argv[1:8]
with open(src) as f:
    cfg = yaml.safe_load(f)
env = cfg["agent"]["tools"].setdefault("env_variables", {})
env["GT_ARM"] = arm
env["GT_RUN_ID"] = run_id
env["GT_INSTANCE_ID"] = iid
env["GT_TELEMETRY_DIR"] = tdir
env["GT_LSP_ENABLED"] = "1"
env["GT_ARM_ON_MATERIAL_EDIT"] = "1"
for bundle in cfg["agent"]["tools"].get("bundles", []):
    if isinstance(bundle, dict) and bundle.get("path", "").endswith("groundtruth"):
        bundle["path"] = bundle_path
with open(dst, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
PY

python3 -m sweagent run-batch \
  --config "$PATCHED" \
  --instances.type swe_bench --instances.subset verified --instances.split test \
  --instances.filter "$TASK" \
  --output_dir "$TASK_DIR" \
  > "$TASK_DIR/run.log" 2>&1 &
P=$!
echo "  $TASK PID=$P" | tee -a "$OUTDIR/master.log"

# --- WATCHDOG POLL (1 task) ---
record_kill() {
  local T="$1" P="$2" REASON="$3"
  local TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"instance_id":"%s","pid":%s,"reason":"%s","killed_at":"%s","killed_manually":false}\n' \
    "$T" "$P" "$REASON" "$TS" >> "$KILLED_LOG"
  kill -TERM "$P" 2>/dev/null || true
  sleep 5
  kill -KILL "$P" 2>/dev/null || true
  IDS="$(docker ps --format '{{.ID}} {{.Names}}' | awk -v t="$T" 'index($0,t) {print $1}')"
  if [ -n "$IDS" ]; then
    printf '%s\n' "$IDS" | xargs -r docker stop >/dev/null 2>&1 || true
  fi
  echo "WATCHDOG_KILL $T PID=$P reason=$REASON at $TS" | tee -a "$OUTDIR/master.log"
}

while kill -0 "$P" 2>/dev/null; do
  WD_OUT="$TASK_DIR/watchdog_last.json"
  python3 "$FINALIZATION_PY" watchdog --task-dir "$TASK_DIR" --stall-minutes "$STALL_MINUTES" \
    > "$WD_OUT" 2>/dev/null || true
  if python3 -c "import json,sys; d=json.load(open(r'$WD_OUT')); sys.exit(0 if d.get('triggered') else 1)" 2>/dev/null; then
    record_kill "$TASK" "$P" "watchdog_stall_gt_${STALL_MINUTES}m"
    break
  fi
  sleep "$WATCHDOG_POLL_SECS"
done
wait "$P" 2>/dev/null || true

bash /home/Lenovo/gt_telemetry_scraper.sh "$OUTDIR" --once >> "$OUTDIR/scraper.log" 2>&1 || true
kill "$SCRAPER_PID" 2>/dev/null || true
pkill -P "$SCRAPER_PID" 2>/dev/null || true

python3 /tmp/SWE-agent/config/gt_canary_report.py \
  --outdir "$OUTDIR" \
  --arm "$GT_ARM" \
  --run-id "$GT_RUN_ID" \
  --max-steps 150 \
  --emit-task-logs \
  --emit-smoke-summary \
  >> "$OUTDIR/master.log" 2>&1 || true

# Classify as READINESS_SMOKE so it never counts as official.
python3 "$FINALIZATION_PY" classify --run-dir "$OUTDIR" --declared READINESS_SMOKE \
  >> "$OUTDIR/master.log" 2>&1 || true

echo "=== Lane B re-probe DONE $GT_RUN_ID $(date) ===" | tee -a "$OUTDIR/master.log"
cat "$OUTDIR/run_classification.json" 2>/dev/null | tee -a "$OUTDIR/master.log" || true
