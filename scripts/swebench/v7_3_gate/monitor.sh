#!/usr/bin/env bash
# v7.3 gate -- monitor.sh
# Runs ON VM1. Prints disk + per-arm progress every 60s.
# Exits when both arms have PID-finished AND output.jsonl line count == expected.

set -euo pipefail

PIDS_FILE="/tmp/v7_3_gate.pids"
if [ ! -f "$PIDS_FILE" ]; then
  echo "FAIL: $PIDS_FILE missing. Run run_gate.sh first."
  exit 1
fi
# shellcheck disable=SC1090
source "$PIDS_FILE"

# --- Determine expected task count from the task-id file ---
TASK_FILE_V73="$ARM_V73_DIR/instance_ids.txt"
if [ ! -f "$TASK_FILE_V73" ]; then
  echo "FAIL: missing $TASK_FILE_V73"
  exit 1
fi
EXPECTED=$(grep -c -v '^[[:space:]]*$' "$TASK_FILE_V73" || echo 0)
if [ "$EXPECTED" -lt 1 ]; then
  echo "FAIL: instance_ids.txt is empty: $TASK_FILE_V73"
  exit 1
fi

START_TS=$(date +%s)

is_alive() {
  local pid="$1"
  if [ -z "$pid" ]; then return 1; fi
  if kill -0 "$pid" 2>/dev/null; then return 0; else return 1; fi
}

count_jsonl() {
  local dir="$1"
  local f="$dir/output.jsonl"
  if [ -f "$f" ]; then
    wc -l < "$f" | tr -d '[:space:]'
  else
    echo 0
  fi
}

count_errored() {
  # Count lines whose JSON has "error" key (best-effort, no jq dep).
  local dir="$1"
  local f="$dir/output.jsonl"
  if [ -f "$f" ]; then
    grep -c '"error"' "$f" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

format_eta() {
  local done="$1"
  local total="$2"
  local elapsed="$3"
  if [ "$done" -lt 1 ]; then
    echo "n/a"
    return
  fi
  local rate_s_per_task=$(( elapsed / done ))
  if [ "$rate_s_per_task" -lt 1 ]; then rate_s_per_task=1; fi
  local remaining=$(( (total - done) * rate_s_per_task ))
  if [ "$remaining" -lt 0 ]; then remaining=0; fi
  printf '%dm%02ds' $(( remaining / 60 )) $(( remaining % 60 ))
}

echo "============================================================"
echo "  v7.3 gate monitor"
echo "  Expected per arm: $EXPECTED tasks"
echo "  v7.3 dir: $ARM_V73_DIR  PID=$PID_V73"
echo "  v7.2 dir: $ARM_V72_DIR  PID=$PID_V72"
echo "============================================================"

while true; do
  NOW=$(date +%s)
  ELAPSED=$(( NOW - START_TS ))

  DU_LINE=$(du -sh /var/lib/containerd 2>/dev/null | awk '{print $1}')

  V73_DONE=$(count_jsonl "$ARM_V73_DIR")
  V72_DONE=$(count_jsonl "$ARM_V72_DIR")
  V73_ERR=$(count_errored "$ARM_V73_DIR")
  V72_ERR=$(count_errored "$ARM_V72_DIR")
  V73_REM=$(( EXPECTED - V73_DONE ))
  V72_REM=$(( EXPECTED - V72_DONE ))
  if [ "$V73_REM" -lt 0 ]; then V73_REM=0; fi
  if [ "$V72_REM" -lt 0 ]; then V72_REM=0; fi

  V73_ALIVE="dead"; if is_alive "$PID_V73"; then V73_ALIVE="alive"; fi
  V72_ALIVE="dead"; if is_alive "$PID_V72"; then V72_ALIVE="alive"; fi

  V73_ETA=$(format_eta "$V73_DONE" "$EXPECTED" "$ELAPSED")
  V72_ETA=$(format_eta "$V72_DONE" "$EXPECTED" "$ELAPSED")

  echo ""
  echo "[$(date -u +%FT%TZ)]  elapsed=${ELAPSED}s  containerd=${DU_LINE}"
  printf "  v7.3 [%s]  done=%d  remaining=%d  errored=%d  ETA=%s\n" \
    "$V73_ALIVE" "$V73_DONE" "$V73_REM" "$V73_ERR" "$V73_ETA"
  printf "  v7.2 [%s]  done=%d  remaining=%d  errored=%d  ETA=%s\n" \
    "$V72_ALIVE" "$V72_DONE" "$V72_REM" "$V72_ERR" "$V72_ETA"

  # Exit condition: both arms PID-finished AND both reached expected count.
  if ! is_alive "$PID_V73" && ! is_alive "$PID_V72" \
     && [ "$V73_DONE" -ge "$EXPECTED" ] && [ "$V72_DONE" -ge "$EXPECTED" ]; then
    echo ""
    echo "============================================================"
    echo "  PASS: both arms finished and reached $EXPECTED tasks each."
    echo "  Run: bash /opt/gt_bundle/v7_3_gate/verify_and_report.sh"
    echo "============================================================"
    exit 0
  fi

  # Defensive: if both PIDs are dead but counts < expected, exit non-zero so
  # the user investigates rather than verify_and_report.sh running on a partial.
  if ! is_alive "$PID_V73" && ! is_alive "$PID_V72"; then
    if [ "$V73_DONE" -lt "$EXPECTED" ] || [ "$V72_DONE" -lt "$EXPECTED" ]; then
      echo ""
      echo "============================================================"
      echo "  FAIL: both PIDs ended but at least one arm is short."
      echo "    v7.3 done=$V73_DONE / $EXPECTED"
      echo "    v7.2 done=$V72_DONE / $EXPECTED"
      echo "  Inspect run.log in each arm dir before re-launching."
      echo "============================================================"
      exit 1
    fi
  fi

  sleep 60
done
