#!/usr/bin/env bash
# v1.0.5 1-task probe launcher (Phase 1 gate per the apparatus plan).
#
# Runs ONE Live-Lite task end-to-end on this VM with the v1.0.5 apparatus:
#   - OH 0.54 + Qwen3-Coder-480B-A35B (Vertex MaaS via litellm proxy)
#   - 5 GT layers (v8.2.2 brief / 5-family hook / 3 composite endpoints /
#     budget caps / pre-submit gate)
#   - Telemetry into /tmp/gt_telemetry_<instance_id>/ across 5 active layers
#
# Prereqs on this host:
#   - litellm proxy running on :4000 with vertex_ai/qwen3-coder route ready
#     (test: `curl -s localhost:4000/v1/models | jq` should list the model)
#   - $GT_PREBUILT_INDEXES_ROOT exists with <instance_id>/graph.db prebuilt
#     (default: /home/Lenovo/eval_live_lite_2026-05-04/gt_indexes/)
#   - GroundTruth repo synced to ~/groundtruth at the v1.0.5 commit
#   - Docker daemon up; SWE-bench-Live image for the chosen task pre-pulled
#
# Usage:
#   GT_INSTANCE_ID=django__django-12345 ./launch_v105_probe.sh
#   or
#   ./launch_v105_probe.sh django__django-12345

set -euo pipefail

INSTANCE_ID="${1:-${GT_INSTANCE_ID:-}}"
if [[ -z "$INSTANCE_ID" ]]; then
    echo "ERROR: pass instance_id as arg 1 or set GT_INSTANCE_ID" >&2
    echo "Usage: $0 <instance_id>" >&2
    exit 2
fi

REPO_ROOT="${GT_REPO_ROOT:-$HOME/groundtruth}"
LLM_CONFIG="${LLM_CONFIG:-$REPO_ROOT/.llm_config/vertex_qwen3_v105.json}"
INDEXES_ROOT="${GT_PREBUILT_INDEXES_ROOT:-/home/Lenovo/eval_live_lite_2026-05-04/gt_indexes}"
OUT_BASE="${OUT_BASE:-$HOME/groundtruth/runs/v105_probe}"
RUN_DIR="$OUT_BASE/$INSTANCE_ID"
TELEMETRY_ROOT="${GT_TELEMETRY_ROOT:-/tmp}"

mkdir -p "$RUN_DIR"

# -- preflight --------------------------------------------------------------
echo "=== v1.0.5 PROBE PREFLIGHT ==="
echo "  instance_id        = $INSTANCE_ID"
echo "  repo_root          = $REPO_ROOT"
echo "  llm_config         = $LLM_CONFIG"
echo "  indexes_root       = $INDEXES_ROOT"
echo "  prebuilt graph.db  = $INDEXES_ROOT/$INSTANCE_ID/graph.db"
echo "  out_dir            = $RUN_DIR"
echo "  telemetry_root     = $TELEMETRY_ROOT"
echo

if [[ ! -f "$LLM_CONFIG" ]]; then
    echo "ERROR: missing LLM config at $LLM_CONFIG" >&2
    exit 3
fi
if [[ ! -f "$INDEXES_ROOT/$INSTANCE_ID/graph.db" ]]; then
    echo "WARN: no prebuilt graph.db at $INDEXES_ROOT/$INSTANCE_ID/graph.db"
    echo "      brief generation will return empty; agent runs without focus block."
fi

# Quick litellm proxy probe — fail fast if the route isn't there.
if ! curl -s -m 3 http://localhost:4000/v1/models >/dev/null; then
    echo "ERROR: litellm proxy not responding on localhost:4000" >&2
    exit 4
fi

# -- export env for the wrapper --------------------------------------------
export GT_INSTANCE_ID="$INSTANCE_ID"
export GT_PREBUILT_INDEXES_ROOT="$INDEXES_ROOT"
export GT_TELEMETRY_ROOT="$TELEMETRY_ROOT"
export PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}"
# v22_brief uses an embed cache for sentence-transformers; keep it scoped.
export EMBED_CACHE_DIR="${EMBED_CACHE_DIR:-$HOME/.embed_cache/v105}"

# Fresh telemetry per probe so we don't double-count.
rm -rf "$TELEMETRY_ROOT/gt_telemetry_$INSTANCE_ID" 2>/dev/null || true
rm -f "$TELEMETRY_ROOT/gt_calls_$INSTANCE_ID.json" 2>/dev/null || true
rm -f "$TELEMETRY_ROOT/gt_finish_attempts_$INSTANCE_ID.json" 2>/dev/null || true
rm -f "$TELEMETRY_ROOT/gt_hook_dedup_$INSTANCE_ID.json" 2>/dev/null || true
rm -f "/tmp/gt_check_log.jsonl" 2>/dev/null || true
rm -f "/tmp/gt_hook_log.jsonl" 2>/dev/null || true

# Single-instance config file (so the wrapper's --instance-ids-file flag has
# something to point at).
INSTANCE_FILE="$RUN_DIR/config.json"
cat > "$INSTANCE_FILE" <<EOF
{ "instance_ids": ["$INSTANCE_ID"] }
EOF

# -- launch the wrapper -----------------------------------------------------
echo "=== LAUNCHING v1.0.5 WRAPPER ==="
LAUNCH_LOG="$RUN_DIR/launch.log"
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "start: $START_TS" | tee "$LAUNCH_LOG"

cd "$REPO_ROOT"
python scripts/swebench/oh_gt_live_lite_wrapper.py \
    "$LLM_CONFIG" \
    --workspace docker \
    --dataset SWE-bench-Live/SWE-bench-Live \
    --split lite \
    --instance-ids-file "$INSTANCE_FILE" \
    --output-dir "$RUN_DIR" \
    --hooks both \
    --max-iterations 100 \
    2>&1 | tee -a "$LAUNCH_LOG" || {
    echo "WRAPPER_EXIT_NONZERO" | tee -a "$LAUNCH_LOG"
}

END_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "end: $END_TS" | tee -a "$LAUNCH_LOG"

# -- aggregate --------------------------------------------------------------
echo "=== AGGREGATING PER-TASK SUMMARY ==="
TELEMETRY_DIR="$TELEMETRY_ROOT/gt_telemetry_$INSTANCE_ID"
if [[ -d "$TELEMETRY_DIR" ]]; then
    python scripts/swebench/aggregate_v105_per_task.py \
        --telemetry-dir "$TELEMETRY_DIR" \
        --output-dir "$RUN_DIR" \
        --arm gt_v105 \
        ${RUN_DIR:+--oh-output-jsonl "$RUN_DIR/output.jsonl"} \
        ${RUN_DIR:+--llm-completions-dir "$RUN_DIR/llm_completions"} \
        || echo "WARN: aggregator failed (continuing)"
else
    echo "WARN: no telemetry dir at $TELEMETRY_DIR — apparatus may not have fired"
fi

# Mirror gt_hook_log out of /tmp into the run dir for offline analysis.
[[ -f /tmp/gt_hook_log.jsonl ]] && cp /tmp/gt_hook_log.jsonl "$RUN_DIR/" || true
[[ -f /tmp/gt_check_log.jsonl ]] && cp /tmp/gt_check_log.jsonl "$RUN_DIR/" || true
[[ -f "$TELEMETRY_ROOT/gt_calls_$INSTANCE_ID.json" ]] && \
    cp "$TELEMETRY_ROOT/gt_calls_$INSTANCE_ID.json" "$RUN_DIR/" || true
[[ -f "$TELEMETRY_ROOT/gt_finish_attempts_$INSTANCE_ID.json" ]] && \
    cp "$TELEMETRY_ROOT/gt_finish_attempts_$INSTANCE_ID.json" "$RUN_DIR/" || true

echo
echo "=== DONE ==="
echo "  per_task_summary: $RUN_DIR/per_task_summary.json"
echo "  telemetry        : $TELEMETRY_DIR/"
echo "  launch log       : $LAUNCH_LOG"
echo
echo "Next: render PROBE_REPORT.md against the behavioral checkboxes from the plan."
