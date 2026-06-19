#!/bin/bash
# Lane A — Official nolsp repeat launcher.
#
# Wraps run_regression_nolsp_core.sh's bundle + sweagent setup, then replaces
# its blind `wait` loop with a watchdog-driven poll. When any task stalls past
# --stall-minutes with no telemetry progress, the task is SIGTERM'd and written
# to killed_tasks.jsonl; classify then marks the whole run DIAGNOSTIC_ONLY.
#
# Inputs (env):
#   REPEAT              1|2|3                (required)
#   OUTROOT             /tmp/official_nolsp  (default)
#   STALL_MINUTES       15                   (default, per nuclear policy)
#   WATCHDOG_POLL_SECS  60                   (default)
#   SUITE_FILE          /home/Lenovo/frozen_gt_astropy10.txt  (default)
#   FINALIZATION_PY     /home/Lenovo/gt_finalization.py       (default)
#
# Pinned: model = openai/deepseek-v3.2-maas.
set -e

REPEAT="${REPEAT:?REPEAT must be set (1|2|3)}"
OUTROOT="${OUTROOT:-/tmp/official_nolsp}"
STALL_MINUTES="${STALL_MINUTES:-15}"
WATCHDOG_POLL_SECS="${WATCHDOG_POLL_SECS:-60}"
SUITE_FILE="${SUITE_FILE:-/home/Lenovo/frozen_gt_astropy10.txt}"
FINALIZATION_PY="${FINALIZATION_PY:-/home/Lenovo/gt_finalization.py}"

if env | grep -qE '^(AWS_|BEDROCK_|AMAZON_)'; then
  echo "ERROR: AWS/Bedrock env vars present, refusing to launch" >&2
  exit 1
fi

if [ ! -f "$SUITE_FILE" ]; then
  echo "ERROR: suite file $SUITE_FILE not found" >&2
  exit 1
fi
if [ ! -f "$FINALIZATION_PY" ]; then
  echo "ERROR: finalization helper $FINALIZATION_PY not found" >&2
  exit 1
fi

TASKS="$(grep -v '^\s*#' "$SUITE_FILE" | awk 'NF' | tr '\n' ' ')"
OUTDIR="$OUTROOT/repeat_$REPEAT"
GT_RUN_ID="official_nolsp_r${REPEAT}_$(date +%s)"

source ~/sweagent-env/bin/activate
cd /tmp/SWE-agent
export PATH=$HOME/.local/bin:$PATH
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-gt-local}"
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://172.17.0.1:4000}"
export GT_ARM="gt-nolsp"
export GT_LSP_ENABLED="0"
CFG=/tmp/SWE-agent/config/canary_gt_qwen.yaml

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"
KILLED_LOG="$OUTDIR/killed_tasks.jsonl"
touch "$KILLED_LOG"

# ---------- same bundle/cleanup flow as run_regression_nolsp_core.sh ----------
cleanup_old_task_containers() {
  # Hard cleanup: rm -f covers both running and stopped containers. `docker stop`
  # alone leaves stopped containers on disk, and the scraper then docker-cp's
  # their stale telemetry files into the new OUTDIR — that's what caused the
  # 14096 false-positive watchdog kill in official_nolsp_r1_1776848460.
  for T in $TASKS; do
    IDS="$(python3 - "$T" <<'PY'
import subprocess, sys
task = sys.argv[1]
# -a includes stopped containers (critical for removing stale telemetry sources).
out = subprocess.check_output(["docker", "ps", "-a", "--format", "{{.ID}} {{.Names}}"], text=True)
for line in out.splitlines():
    parts = line.strip().split(None, 1)
    if len(parts) == 2 and task in parts[1]:
        print(parts[0])
PY
)"
    if [ -n "$IDS" ]; then
      echo "Removing lingering containers (running or stopped) for $T" | tee -a "$OUTDIR/master.log"
      printf '%s\n' "$IDS" | xargs -r docker rm -f >/dev/null 2>&1 || true
    fi
  done
}
if [ "${SKIP_CLEANUP:-0}" = "1" ]; then
  echo "SKIP_CLEANUP=1, caller is responsible for pre-wave cleanup"
else
  cleanup_old_task_containers
fi

echo "=== Lane A official nolsp repeat $REPEAT $GT_RUN_ID $(date) ===" | tee "$OUTDIR/master.log"
echo "tasks: $TASKS" | tee -a "$OUTDIR/master.log"
echo "outdir: $OUTDIR" | tee -a "$OUTDIR/master.log"
echo "stall_minutes: $STALL_MINUTES  poll_secs: $WATCHDOG_POLL_SECS" | tee -a "$OUTDIR/master.log"

setsid bash /home/Lenovo/gt_telemetry_scraper.sh "$OUTDIR" > "$OUTDIR/scraper.log" 2>&1 < /dev/null &
SCRAPER_PID=$!
echo "scraper PID=$SCRAPER_PID" | tee -a "$OUTDIR/master.log"

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
  cat > "$task_bundle/bin/gt_budget.state.json" <<EOF
{"scope":"${GT_RUN_ID}__${task_id}__${GT_ARM}","orient":{"count":0,"limit":2,"exhausted":false},"lookup":{"count":0,"limit":3,"exhausted":false},"impact":{"count":0,"limit":2,"exhausted":false},"check":{"count":0,"limit":20,"exhausted":false},"orient_exhausted":false,"initialized":true,"source":"launcher_bootstrap"}
EOF
  cat > "$task_bundle/bin/gt_startup_trace.jsonl" <<EOF
{"event":"startup_enter","ts":0,"scope":"${GT_RUN_ID}__${task_id}__${GT_ARM}","run_id":"$GT_RUN_ID","arm":"$GT_ARM","instance_id":"$task_id","source":"launcher"}
EOF
  printf '%s\n' "$task_bundle"
}

declare -A PID_OF
declare -A TASK_OF
for T in $TASKS; do
  mkdir -p "$OUTDIR/$T"
  # Belt-and-suspenders: start with an empty telemetry file so the watchdog
  # never sees stale lines from a previous run (mtime/st_mtime fallback goes
  # to "now" for an empty file, which the watchdog treats as fresh).
  : > "$OUTDIR/$T/gt_hook_telemetry.jsonl"
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
    --instances.filter "$T" \
    --output_dir "$OUTDIR/$T" \
    > "$OUTDIR/$T/run.log" 2>&1 &
  P=$!
  PID_OF["$T"]=$P
  TASK_OF["$P"]=$T
  echo "  $T PID=$P" | tee -a "$OUTDIR/master.log"
done

echo "All ${#PID_OF[@]} tasks launched. Watchdog poll every ${WATCHDOG_POLL_SECS}s, stall threshold ${STALL_MINUTES}m." | tee -a "$OUTDIR/master.log"

# ---------- WATCHDOG POLL LOOP (replaces `wait`) ----------
record_kill() {
  local T="$1"
  local P="$2"
  local REASON="$3"
  local TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"instance_id":"%s","pid":%s,"reason":"%s","killed_at":"%s","killed_manually":false}\n' \
    "$T" "$P" "$REASON" "$TS" >> "$KILLED_LOG"
  kill -TERM "$P" 2>/dev/null || true
  sleep 5
  kill -KILL "$P" 2>/dev/null || true
  # Stop any lingering docker containers that still reference this task.
  IDS="$(docker ps --format '{{.ID}} {{.Names}}' | awk -v t="$T" 'index($0,t) {print $1}')"
  if [ -n "$IDS" ]; then
    echo "Stopping lingering containers for $T" | tee -a "$OUTDIR/master.log"
    printf '%s\n' "$IDS" | xargs -r docker stop >/dev/null 2>&1 || true
  fi
  echo "WATCHDOG_KILL $T PID=$P reason=$REASON at $TS" | tee -a "$OUTDIR/master.log"
}

while true; do
  REMAINING=0
  for T in "${!PID_OF[@]}"; do
    P="${PID_OF[$T]}"
    if [ -z "$P" ]; then continue; fi
    if ! kill -0 "$P" 2>/dev/null; then
      # natural exit
      PID_OF["$T"]=""
      continue
    fi
    REMAINING=$((REMAINING+1))
    # Watchdog check per task
    WD_OUT="$OUTDIR/$T/watchdog_last.json"
    python3 "$FINALIZATION_PY" watchdog --task-dir "$OUTDIR/$T" --stall-minutes "$STALL_MINUTES" \
      > "$WD_OUT" 2>/dev/null || true
    if python3 -c "import json,sys; d=json.load(open(r'$WD_OUT')); sys.exit(0 if d.get('triggered') else 1)" 2>/dev/null; then
      record_kill "$T" "$P" "watchdog_stall_gt_${STALL_MINUTES}m"
      PID_OF["$T"]=""
    fi
  done
  if [ "$REMAINING" -eq 0 ]; then break; fi
  sleep "$WATCHDOG_POLL_SECS"
done

echo "All tasks ended (natural or watchdog)." | tee -a "$OUTDIR/master.log"

# Reap any leftover zombies
for T in "${!PID_OF[@]}"; do
  P_ORIG="${PID_OF[$T]:-}"
  if [ -n "$P_ORIG" ]; then wait "$P_ORIG" 2>/dev/null || true; fi
done

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

python3 - "$OUTDIR" <<'PY'
import glob, json, os, sys
root = sys.argv[1]
preds = {}
for f in glob.glob(os.path.join(root, '*/preds.json')):
    try:
        preds.update(json.load(open(f)))
    except Exception:
        pass
out = os.path.join(root, 'preds.json')
json.dump(preds, open(out, 'w'))
print(f"merged {len(preds)} predictions -> {out}")
PY

# ---------- FINAL CLASSIFICATION ----------
python3 "$FINALIZATION_PY" classify --run-dir "$OUTDIR" >> "$OUTDIR/master.log" 2>&1 || true

echo "=== Lane A repeat $REPEAT DONE $GT_RUN_ID $(date) ===" | tee -a "$OUTDIR/master.log"
cat "$OUTDIR/run_classification.json" 2>/dev/null | tee -a "$OUTDIR/master.log" || true
