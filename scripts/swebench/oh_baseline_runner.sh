#!/usr/bin/env bash
# oh_baseline_runner.sh --- per-shard runner for the OpenHands + Vertex Qwen3-Coder
# baseline-family calibration on SWE-bench-Live Lite.
#
# Family-defining contract (must not change between calibration and a later GT A/B):
#   harness       = SWE-bench-Live/OpenHands @ f4da691c
#   agent         = CodeActAgent
#   dataset/split = SWE-bench-Live/SWE-bench-Live lite
#   model         = vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas
#
# Operational knobs (flexible): SHARD_ID, INSTANCE_RANGE, NUM_WORKERS.
#
# Expected env (set by caller / systemd-run):
#   OUTDIR                          - absolute path to the run output dir (created)
#   GT_RUN_ID                       - caller-set run id (forwarded into OH config)
#   OH_DIR                          - path to the SWE-bench-Live/OpenHands fork checkout
#   GOOGLE_APPLICATION_CREDENTIALS  - Vertex service-account JSON
#   VERTEXAI_PROJECT                - GCP project id
#   VERTEXAI_LOCATION               - winner from preflight (e.g., us-south1 or global)
#   MANIFEST_TXT                    - path to cal20_live_lite_oh.txt (for eval_selection)
#
# Usage:
#   bash oh_baseline_runner.sh <SHARD_ID> <START-END> <NUM_WORKERS>
#   bash oh_baseline_runner.sh A 1-10 2

set -euo pipefail

SHARD_ID="${1:?shard id (A|B) required}"
INSTANCE_RANGE="${2:?instance range (e.g., 1-10) required}"
NUM_WORKERS="${3:?num workers required}"

: "${OUTDIR:?OUTDIR is required}"
: "${GT_RUN_ID:?GT_RUN_ID is required}"
: "${OH_DIR:?OH_DIR (SWE-bench-Live/OpenHands checkout) is required}"
: "${GOOGLE_APPLICATION_CREDENTIALS:?GOOGLE_APPLICATION_CREDENTIALS is required}"
: "${VERTEXAI_PROJECT:?VERTEXAI_PROJECT is required}"
: "${VERTEXAI_LOCATION:?VERTEXAI_LOCATION is required}"
: "${MANIFEST_TXT:?MANIFEST_TXT (cal20 id list) is required}"

SHARD_DIR="$OUTDIR/shard_${SHARD_ID}"
mkdir -p "$SHARD_DIR"

START_IDX="${INSTANCE_RANGE%-*}"
END_IDX="${INSTANCE_RANGE#*-}"
SHARD_SIZE=$((END_IDX - START_IDX + 1))

# Slice the manifest for this shard (1-indexed, inclusive).
SHARD_IDS_FILE="$SHARD_DIR/ids.txt"
sed -n "${START_IDX},${END_IDX}p" "$MANIFEST_TXT" > "$SHARD_IDS_FILE"
ACTUAL=$(wc -l < "$SHARD_IDS_FILE" | tr -d ' ')
if [ "$ACTUAL" -ne "$SHARD_SIZE" ]; then
    echo "ERROR: expected $SHARD_SIZE ids for shard $SHARD_ID, got $ACTUAL" >&2
    exit 2
fi

# The fork's run_infer.sh honors EVAL_LIMIT and accepts an explicit instance selection
# via env var. The canonical interface is:
#   run_infer.sh <llm_section> <commit> <agent> <eval_limit> <max_iter> <num_workers> <dataset> <split>
# We export INSTANCE_IDS so the fork's driver picks exactly our shard's ids.
export EVAL_INSTANCES_FILE="$SHARD_IDS_FILE"
export EVAL_OUTPUT_DIR="$SHARD_DIR"
export GT_RUN_ID

# Log a provenance header so each shard's output is traceable.
{
    echo "=== oh_baseline_runner ==="
    echo "shard_id=$SHARD_ID"
    echo "range=$INSTANCE_RANGE"
    echo "shard_size=$SHARD_SIZE"
    echo "num_workers=$NUM_WORKERS"
    echo "vertex_region=$VERTEXAI_LOCATION"
    echo "vertex_project=$VERTEXAI_PROJECT"
    echo "oh_dir=$OH_DIR"
    echo "outdir=$SHARD_DIR"
    echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$SHARD_DIR/runner_provenance.txt"

cd "$OH_DIR"

# Canonical invocation (matches the leaderboard submission's harness surface).
# llm.qwen3_coder_vertex is defined in $OH/config.toml (written by the smoke bootstrap).
poetry run ./evaluation/benchmarks/swe_bench/scripts/run_infer.sh \
    llm.qwen3_coder_vertex \
    HEAD \
    CodeActAgent \
    "$SHARD_SIZE" \
    100 \
    "$NUM_WORKERS" \
    SWE-bench-Live/SWE-bench-Live \
    lite \
    2>&1 | tee "$SHARD_DIR/run_infer.log"

echo "finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$SHARD_DIR/runner_provenance.txt"

# Report: count output.jsonl lines and trajectory dirs so the reconcile step has signal.
if [ -f "$SHARD_DIR/output.jsonl" ]; then
    LINES=$(wc -l < "$SHARD_DIR/output.jsonl" | tr -d ' ')
    echo "output_jsonl_lines=$LINES" >> "$SHARD_DIR/runner_provenance.txt"
fi
