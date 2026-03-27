#!/bin/bash
set -euo pipefail

# GT v7 Smoke Test — runs baseline (no GT) and GT v7 (with hook) side by side.
#
# Uses OpenHands benchmark framework (not mini-swe-agent).
# Baseline uses the default SWE-bench prompt (no GT tool).
# GT v7 uses gt_hook_v7.j2 with gt_hook.py injected via oh_gt_hook_wrapper.py.
#
# Usage:
#   bash oh_run_v7_smoke.sh [--tasks N] [--workers N] [--gt-only] [--baseline-only]
#
# Examples:
#   bash oh_run_v7_smoke.sh                    # 10 tasks, 2 workers, both runs
#   bash oh_run_v7_smoke.sh --tasks 50 --workers 4

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="/root/oh-benchmarks"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT="$HOME/results/v7_smoke_${TIMESTAMP}"

NUM_TASKS=10
NUM_WORKERS=2
RUN_BASELINE=true
RUN_GT=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tasks)          NUM_TASKS="$2";    shift 2 ;;
        --workers)        NUM_WORKERS="$2";  shift 2 ;;
        --gt-only)        RUN_BASELINE=false; shift ;;
        --baseline-only)  RUN_GT=false;       shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ── Create instance selection file ──────────────────────────────────
SELECT_FILE="$OUTPUT_ROOT/select_instances.txt"
mkdir -p "$OUTPUT_ROOT"

if [ "$NUM_TASKS" -le 10 ]; then
    # Default 10 Django instances for smoke
    cat > "$SELECT_FILE" << 'INSTANCES'
django__django-10097
django__django-10554
django__django-10880
django__django-10914
django__django-10973
django__django-11066
django__django-11087
django__django-11095
django__django-11099
django__django-11133
INSTANCES
    # Trim to exact count
    if [ "$NUM_TASKS" -lt 10 ]; then
        head -n "$NUM_TASKS" "$SELECT_FILE" > "$SELECT_FILE.tmp"
        mv "$SELECT_FILE.tmp" "$SELECT_FILE"
    fi
else
    INSTANCES_FILE="$SCRIPT_DIR/instances_a.txt"
    if [ ! -f "$INSTANCES_FILE" ]; then
        echo "ERROR: instances_a.txt not found at $INSTANCES_FILE"
        exit 1
    fi
    head -n "$NUM_TASKS" "$INSTANCES_FILE" > "$SELECT_FILE"
    echo "Selected $NUM_TASKS tasks from instances_a.txt"
fi

TASK_COUNT=$(wc -l < "$SELECT_FILE")

# ── Preflight checks ─────────────────────────────────────────────────
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000."
    exit 1
fi
echo "Proxy: OK"

# LLM config
LLM_CONFIG="$SCRIPT_DIR/oh_llm_config_qwen3_proxy.json"
if [ ! -f "$LLM_CONFIG" ]; then
    echo "ERROR: No LLM config found at $LLM_CONFIG"
    exit 1
fi
echo "LLM config: $LLM_CONFIG"

GT_HOOK="$REPO_DIR/benchmarks/swebench/gt_hook.py"
if $RUN_GT && [ ! -f "$GT_HOOK" ]; then
    echo "ERROR: gt_hook.py not found at $GT_HOOK"
    exit 1
fi

echo ""
echo "================================================="
echo "  GT v7 Smoke Test (OpenHands)"
echo "  Started:   $(date -u) UTC"
echo "  Output:    $OUTPUT_ROOT"
echo "  Tasks:     $TASK_COUNT"
echo "  Workers:   $NUM_WORKERS"
echo "  Baseline:  $RUN_BASELINE"
echo "  GT v7:     $RUN_GT"
echo "================================================="
echo ""

# ── Common args for run_infer ─────────────────────────────────────────
COMMON_ARGS=(
    --dataset princeton-nlp/SWE-bench_Lite
    --split test
    --workspace docker
    --max-iterations 50
    --num-workers "$NUM_WORKERS"
    --select "$SELECT_FILE"
)

# ── Run 1: Baseline (no GT) ──────────────────────────────────────────
if $RUN_BASELINE; then
    BASELINE_DIR="$OUTPUT_ROOT/baseline"
    mkdir -p "$BASELINE_DIR"

    echo ""
    echo "───────────────────────────────────────────────────"
    echo "  Run 1/2: BASELINE (no GT tool)"
    echo "  Output: $BASELINE_DIR"
    echo "───────────────────────────────────────────────────"
    echo ""

    cd "$OH_DIR"
    sudo .venv/bin/python -m benchmarks.swebench.run_infer "$LLM_CONFIG" \
        "${COMMON_ARGS[@]}" \
        --output-dir "$BASELINE_DIR" \
        --note "v7_baseline" \
        2>&1 | tee "$BASELINE_DIR/run.log" || true

    if [ -f "$BASELINE_DIR/output.jsonl" ]; then
        BASELINE_COUNT=$(wc -l < "$BASELINE_DIR/output.jsonl")
        echo "Baseline completed: $BASELINE_COUNT tasks"
    else
        echo "WARNING: No baseline output.jsonl found"
        BASELINE_COUNT=0
    fi
fi

# ── Run 2: GT v7 (with hook) ─────────────────────────────────────────
if $RUN_GT; then
    GT_DIR="$OUTPUT_ROOT/gt_v7"
    GT_LOG_DIR="$GT_DIR/gt_logs"
    mkdir -p "$GT_DIR" "$GT_LOG_DIR"
    export GT_LOG_DIR

    # Copy GT v7 prompt template to OH prompts dir
    PROMPT_NAME="gt_hook_v7.j2"
    PROMPT_SRC="$SCRIPT_DIR/oh_prompt_gt_v7.j2"
    if [ ! -f "$PROMPT_SRC" ]; then
        echo "ERROR: No v7 prompt template found at $PROMPT_SRC"
        exit 1
    fi
    sudo cp "$PROMPT_SRC" "$OH_DIR/benchmarks/swebench/prompts/$PROMPT_NAME"
    echo "Prompt template copied: $PROMPT_NAME"

    echo ""
    echo "───────────────────────────────────────────────────"
    echo "  Run 2/2: GT v7 (with gt_hook.py)"
    echo "  Output:   $GT_DIR"
    echo "  Hook:     $GT_HOOK ($(wc -c < "$GT_HOOK") bytes)"
    echo "  Logs:     $GT_LOG_DIR"
    echo "───────────────────────────────────────────────────"
    echo ""

    cd "$OH_DIR"
    sudo .venv/bin/python "$SCRIPT_DIR/oh_gt_hook_wrapper.py" "$LLM_CONFIG" \
        "${COMMON_ARGS[@]}" \
        --prompt-path "$PROMPT_NAME" \
        --output-dir "$GT_DIR" \
        --note "v7_gt" \
        2>&1 | tee "$GT_DIR/run.log" || true

    if [ -f "$GT_DIR/output.jsonl" ]; then
        GT_COUNT=$(wc -l < "$GT_DIR/output.jsonl")
        echo "GT v7 completed: $GT_COUNT tasks"
    else
        echo "WARNING: No GT v7 output.jsonl found"
        GT_COUNT=0
    fi

    # Analyze hook logs
    if [ -d "$GT_LOG_DIR" ] && [ "$(ls -A "$GT_LOG_DIR" 2>/dev/null)" ]; then
        echo ""
        echo "=== Hook Log Analysis ==="
        python3 "$SCRIPT_DIR/analyze_hook_logs.py" "$GT_LOG_DIR" --smoke-gate 2 || true
    else
        echo "WARNING: No hook logs found in $GT_LOG_DIR"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo "================================================="
echo "  GT v7 Smoke Test Complete"
echo "  Finished: $(date -u) UTC"
echo "  Output:   $OUTPUT_ROOT"
echo ""
if $RUN_BASELINE; then
    echo "  Baseline: ${BASELINE_COUNT:-?} tasks → $OUTPUT_ROOT/baseline/"
fi
if $RUN_GT; then
    echo "  GT v7:    ${GT_COUNT:-?} tasks → $OUTPUT_ROOT/gt_v7/"
fi
echo ""
echo "  Next: Run SWE-bench eval on both dirs to compare resolve rates"
echo "================================================="
