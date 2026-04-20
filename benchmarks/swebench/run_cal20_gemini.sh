#!/bin/bash
# 20-task baseline calibration launcher for Vertex Gemini 3.1 Pro Preview.
# Forked from run_smoke10_nolsp.sh. GT is OFF: no groundtruth bundle, no in-container
# GT env-var injection beyond the run label.
#
# Required env:
#   GT_RUN_ID        -- unique run id (e.g. cal_gemini31pro_1700000000)
#   OUTDIR           -- run output root (e.g. /tmp/cal_gemini31pro_$GT_RUN_ID)
#   SHARD_ID         -- A or B
#   SHARD_SLICE      -- 1-indexed inclusive slice into cal20_live_lite.txt, e.g. "1-10"
#   NUM_WORKERS      -- run-batch worker count, e.g. 4
#
# Optional env:
#   GT_ARM           -- run label (default: baseline-cal-gemini31pro)
#   CONFIG           -- path to CAL_A yaml
#   TASKS_FILE       -- path to cal20_live_lite.txt
#   INSTANCE_TYPE    -- sweagent instance loader type (default: swe_bench_live)
#   INSTANCE_SUBSET  -- instance subset (default: lite)
#   INSTANCE_SPLIT   -- instance split (default: test)
#
# Expected gcloud configuration already active; ADC logged in; SWE-agent venv activated.
# LiteLLM reads vertex_project / vertex_location from the config's completion_kwargs;
# no OPENAI_API_BASE export is needed for the native vertex_ai/ path.
set -euo pipefail

if env | grep -qE '^(AWS_|BEDROCK_|AMAZON_)'; then
    echo "ERROR: AWS/Bedrock env vars present, refusing to launch" >&2
    env | grep -E '^(AWS_|BEDROCK_|AMAZON_)' >&2
    exit 1
fi

: "${GT_RUN_ID:?GT_RUN_ID must be set}"
: "${OUTDIR:?OUTDIR must be set}"
: "${SHARD_ID:?SHARD_ID must be set (A or B)}"
: "${SHARD_SLICE:?SHARD_SLICE must be set (e.g. 1-10)}"
: "${NUM_WORKERS:?NUM_WORKERS must be set}"

export GT_ARM="${GT_ARM:-baseline-cal-gemini31pro}"
CONFIG="${CONFIG:-configs/cal_a_gemini_3_1_pro_fixed.yaml}"
TASKS_FILE="${TASKS_FILE:-benchmarks/swebench/cal20_live_lite.txt}"
INSTANCE_TYPE="${INSTANCE_TYPE:-swe_bench}"
INSTANCE_SUBSET="${INSTANCE_SUBSET:-lite}"
INSTANCE_SPLIT="${INSTANCE_SPLIT:-test}"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: config not found: $CONFIG" >&2
    exit 1
fi
if [ ! -f "$TASKS_FILE" ]; then
    echo "ERROR: tasks file not found: $TASKS_FILE" >&2
    echo "  Generate it first: python scripts/make_cal20_manifest.py" >&2
    exit 1
fi

# Re-assert gcloud ADC is fresh (60 min TTL; shards are budgeted <45 min wall)
if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
    echo "ERROR: gcloud ADC token not available. Run: gcloud auth application-default login" >&2
    exit 1
fi

# Slice tasks for this shard (1-indexed inclusive, e.g. 1-10 picks lines 1..10)
LO="${SHARD_SLICE%-*}"
HI="${SHARD_SLICE#*-}"
if ! [[ "$LO" =~ ^[0-9]+$ && "$HI" =~ ^[0-9]+$ && "$LO" -ge 1 && "$HI" -ge "$LO" ]]; then
    echo "ERROR: bad SHARD_SLICE: $SHARD_SLICE (expected N-M, 1-indexed)" >&2
    exit 1
fi
TASK_LIST="$(grep -vE '^\s*(#|$)' "$TASKS_FILE" | sed -n "${LO},${HI}p" | tr '\n' ' ')"
TASK_COUNT="$(echo "$TASK_LIST" | tr ' ' '\n' | grep -cv '^\s*$' || true)"
if [ "$TASK_COUNT" -eq 0 ]; then
    echo "ERROR: shard slice $SHARD_SLICE yielded 0 tasks from $TASKS_FILE" >&2
    exit 1
fi

SHARD_DIR="$OUTDIR/shard${SHARD_ID}"
mkdir -p "$SHARD_DIR"
MASTER_LOG="$SHARD_DIR/master.log"
: > "$MASTER_LOG"

{
    echo "=== cal20 shard $SHARD_ID START $(date -u +%FT%TZ) ==="
    echo "GT_RUN_ID=$GT_RUN_ID GT_ARM=$GT_ARM"
    echo "CONFIG=$CONFIG"
    echo "TASKS=$TASK_LIST"
    echo "SHARD_SLICE=$SHARD_SLICE TASK_COUNT=$TASK_COUNT NUM_WORKERS=$NUM_WORKERS"
    echo "INSTANCE_TYPE=$INSTANCE_TYPE INSTANCE_SUBSET=$INSTANCE_SUBSET INSTANCE_SPLIT=$INSTANCE_SPLIT"
} | tee -a "$MASTER_LOG"

# Pipe-join the task IDs for the sweagent filter
TASK_FILTER="$(echo "$TASK_LIST" | tr -s ' ' '\n' | grep -v '^$' | paste -sd'|' -)"

START_TS="$(date -u +%FT%TZ)"
RC=0
python3 -m sweagent run-batch \
    --config "$CONFIG" \
    --instances.type "$INSTANCE_TYPE" \
    --instances.subset "$INSTANCE_SUBSET" \
    --instances.split "$INSTANCE_SPLIT" \
    --instances.filter "$TASK_FILTER" \
    --num_workers "$NUM_WORKERS" \
    --output_dir "$SHARD_DIR" \
    > "$SHARD_DIR/run.log" 2>&1 || RC=$?
END_TS="$(date -u +%FT%TZ)"

{
    echo "=== cal20 shard $SHARD_ID END $END_TS rc=$RC ==="
    echo "start_ts=$START_TS end_ts=$END_TS"
} | tee -a "$MASTER_LOG"

# Record shard metadata for cal_metrics.py
cat > "$SHARD_DIR/shard_meta.json" <<EOF
{
  "shard_id": "$SHARD_ID",
  "slice": "$SHARD_SLICE",
  "task_count": $TASK_COUNT,
  "num_workers": $NUM_WORKERS,
  "start_ts": "$START_TS",
  "end_ts": "$END_TS",
  "gt_run_id": "$GT_RUN_ID",
  "gt_arm": "$GT_ARM",
  "config": "$CONFIG",
  "instance_type": "$INSTANCE_TYPE",
  "instance_subset": "$INSTANCE_SUBSET",
  "instance_split": "$INSTANCE_SPLIT",
  "rc": $RC
}
EOF

exit "$RC"
