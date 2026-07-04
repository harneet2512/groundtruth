#!/bin/bash
set -euo pipefail

# ============================================================
# oh_smoke_delivery.sh — 3-Arm OpenHands Evidence Delivery Smoke Gate
#
# Runs baseline, GT v1.1 (gt_hook passive), and GT v1.3 (gt_startupmode)
# arms sequentially, scans telemetry, checks delivery gates, then evals.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="$OH_DIR/.llm_config/vertex_qwen3.json"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

INSTANCES="django__django-10097,django__django-10554,django__django-10880,django__django-10914,django__django-10973,django__django-11066,django__django-11087,django__django-11095,django__django-11099,django__django-11133"
TASK_COUNT=10
NUM_WORKERS=2
MAX_ITERATIONS=50
MODEL="vertex_ai/qwen3-coder-480b"

START_TIME=$(date +%s)

# ---- Parse optional args ----
while [[ $# -gt 0 ]]; do
    case "$1" in
        --num-workers)
            NUM_WORKERS="$2"; shift 2 ;;
        --max-iterations)
            MAX_ITERATIONS="$2"; shift 2 ;;
        --instances)
            INSTANCES="$2"
            TASK_COUNT=$(echo "$INSTANCES" | tr ',' '\n' | wc -l | tr -d ' ')
            shift 2 ;;
        *)
            echo "Unknown arg: $1"; exit 1 ;;
    esac
done

BASE_DIR="$HOME/results/delivery_gate_${TIMESTAMP}"

# ---- Helpers ----
phase_header() {
    local phase="$1"
    local desc="$2"
    echo ""
    echo "========================================="
    echo "  Phase $phase: $desc"
    echo "  $(date '+%Y-%m-%d %H:%M:%S')"
    echo "========================================="
    echo ""
}

phase_done() {
    echo "  -> Phase $1 complete at $(date '+%H:%M:%S')"
}

write_metadata() {
    local arm_dir="$1"
    local arm_name="$2"
    local gt_version="$3"
    cat > "$arm_dir/METADATA.json" <<METAEOF
{
  "date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "condition": "$arm_name",
  "gt_version": "$gt_version",
  "task_count": $TASK_COUNT,
  "num_workers": $NUM_WORKERS,
  "max_iterations": $MAX_ITERATIONS,
  "model": "$MODEL",
  "scaffold": "openhands",
  "gt_branch": "$(cd "$REPO_DIR" && git branch --show-current)",
  "gt_commit": "$(cd "$REPO_DIR" && git rev-parse HEAD)",
  "hostname": "$(hostname)"
}
METAEOF
}

# ============================================================
# Phase 0: Pre-flight
# ============================================================
phase_header 0 "Pre-flight checks"

echo "Checking LiteLLM proxy..."
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "FATAL: LiteLLM proxy not reachable at localhost:4000"
    exit 1
fi
echo "  proxy: OK"

echo "Checking gt_hook.py..."
if [ ! -f "$REPO_DIR/benchmarks/swebench/gt_hook.py" ]; then
    echo "FATAL: gt_hook.py not found at $REPO_DIR/benchmarks/swebench/gt_hook.py"
    exit 1
fi
echo "  gt_hook.py: OK"

echo "Checking gt_tool_v4.py..."
if [ ! -f "$REPO_DIR/benchmarks/swebench/gt_tool_v4.py" ]; then
    echo "FATAL: gt_tool_v4.py not found at $REPO_DIR/benchmarks/swebench/gt_tool_v4.py"
    exit 1
fi
echo "  gt_tool_v4.py: OK"

echo "Checking LLM config..."
if [ ! -f "$LLM_CONFIG" ]; then
    echo "FATAL: LLM config not found at $LLM_CONFIG"
    exit 1
fi
echo "  llm_config: OK"

echo "Creating output directories..."
mkdir -p "$BASE_DIR/baseline"
mkdir -p "$BASE_DIR/gt_v11/gt_logs"
mkdir -p "$BASE_DIR/gt_v13/gt_logs"
echo "  output dir: $BASE_DIR"

phase_done 0

# ============================================================
# Phase 1: Arm A — Baseline (no GT)
# ============================================================
phase_header 1 "Arm A — Baseline"

cd "$OH_DIR"
uv run swebench-infer "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Verified --split test \
    --workspace docker --max-iterations "$MAX_ITERATIONS" \
    --num-workers "$NUM_WORKERS" \
    --filter-instances "$INSTANCES" \
    --output-dir "$BASE_DIR/baseline" \
    2>&1 | tee "$BASE_DIR/baseline/run.log"

write_metadata "$BASE_DIR/baseline" "baseline" "none"
phase_done 1

# ============================================================
# Phase 2: Arm B — GT v1.1 (gt_hook passive)
# ============================================================
phase_header 2 "Arm B — GT v1.1 (gt_hook passive)"

cd "$OH_DIR"
GT_LOG_DIR="$BASE_DIR/gt_v11/gt_logs" \
uv run python "$SCRIPT_DIR/oh_gt_hook_wrapper.py" "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Verified --split test \
    --workspace docker --max-iterations "$MAX_ITERATIONS" \
    --num-workers "$NUM_WORKERS" \
    --filter-instances "$INSTANCES" \
    --output-dir "$BASE_DIR/gt_v11" \
    2>&1 | tee "$BASE_DIR/gt_v11/run.log"

write_metadata "$BASE_DIR/gt_v11" "gt_v11" "1.1"
phase_done 2

# ============================================================
# Phase 3: Arm C — GT v1.3 (gt_startupmode)
# ============================================================
phase_header 3 "Arm C — GT v1.3 (gt_startupmode)"

cd "$OH_DIR"
uv run python "$SCRIPT_DIR/oh_gt_startupmode_wrapper.py" "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Verified --split test \
    --workspace docker --max-iterations "$MAX_ITERATIONS" \
    --num-workers "$NUM_WORKERS" \
    --filter-instances "$INSTANCES" \
    --output-dir "$BASE_DIR/gt_v13" \
    --hooks=write-only \
    2>&1 | tee "$BASE_DIR/gt_v13/run.log"

write_metadata "$BASE_DIR/gt_v13" "gt_v13" "1.3"
phase_done 3

# ============================================================
# Phase 4: Delivery gate scan
# ============================================================
phase_header 4 "Delivery gate scan"

python3 "$SCRIPT_DIR/oh_delivery_gate.py" scan \
    --baseline "$BASE_DIR/baseline" \
    --gt-v11 "$BASE_DIR/gt_v11" --gt-v11-logs "$BASE_DIR/gt_v11/gt_logs" \
    --gt-v13 "$BASE_DIR/gt_v13" --gt-v13-logs "$BASE_DIR/gt_v13/gt_logs" \
    --instances "$INSTANCES" \
    --model "$MODEL" \
    --telemetry-out "$BASE_DIR/delivery_telemetry.jsonl"

phase_done 4

# ============================================================
# Phase 5: Gate verification (STOP ON FAILURE)
# ============================================================
phase_header 5 "Gate verification"

GATE_EXIT=0
python3 "$SCRIPT_DIR/oh_delivery_gate.py" verify \
    --telemetry "$BASE_DIR/delivery_telemetry.jsonl" \
    --gate-report "$BASE_DIR/DELIVERY_GATE_REPORT.md" \
    || GATE_EXIT=$?

if [ $GATE_EXIT -ne 0 ]; then
    echo ""
    echo "========================================="
    echo "  DELIVERY GATE FAILED"
    echo "  Report: $BASE_DIR/DELIVERY_GATE_REPORT.md"
    echo "  DO NOT proceed to evaluation."
    echo "========================================="
    exit 1
fi

echo "  All delivery gates PASSED."
phase_done 5

# ============================================================
# Phase 6: Evaluation (only if gates pass)
# ============================================================
phase_header 6 "Evaluation — convert outputs to preds"

for arm in baseline gt_v11 gt_v13; do
    echo "  Converting $arm/output.jsonl -> preds.jsonl"
    python3 "$SCRIPT_DIR/oh_output_to_preds.py" \
        --input "$BASE_DIR/$arm/output.jsonl" \
        --output "$BASE_DIR/$arm/preds.jsonl"
done

echo ""
echo "  Prediction files ready. To run SWE-bench evaluation:"
echo ""
echo "    for arm in baseline gt_v11 gt_v13; do"
echo "      python -m swebench.harness.run_evaluation \\"
echo "        --predictions_path \$BASE_DIR/\$arm/preds.jsonl \\"
echo "        --swe_bench_tasks princeton-nlp/SWE-bench_Verified \\"
echo "        --log_dir \$BASE_DIR/\$arm/eval_logs \\"
echo "        --testbed /tmp/swebench_testbed"
echo "    done"
echo ""

phase_done 6

# ============================================================
# Phase 7: Report
# ============================================================
phase_header 7 "Final report"

python3 "$SCRIPT_DIR/oh_delivery_report.py" \
    --telemetry "$BASE_DIR/delivery_telemetry.jsonl" \
    --gate-report "$BASE_DIR/DELIVERY_GATE_REPORT.md" \
    --baseline-eval "$BASE_DIR/baseline/result.json" \
    --gt-v11-eval "$BASE_DIR/gt_v11/result.json" \
    --gt-v13-eval "$BASE_DIR/gt_v13/result.json" \
    --baseline-meta "$BASE_DIR/baseline/METADATA.json" \
    --gt-v11-meta "$BASE_DIR/gt_v11/METADATA.json" \
    --gt-v13-meta "$BASE_DIR/gt_v13/METADATA.json" \
    --out-dir "$BASE_DIR"

phase_done 7

# ============================================================
# Summary
# ============================================================
END_TIME=$(date +%s)
ELAPSED=$(( END_TIME - START_TIME ))
ELAPSED_MIN=$(( ELAPSED / 60 ))
ELAPSED_SEC=$(( ELAPSED % 60 ))

echo ""
echo "========================================="
echo "  Delivery smoke complete"
echo "  Total time: ${ELAPSED_MIN}m ${ELAPSED_SEC}s"
echo "  Results:    $BASE_DIR"
echo "  Gate report: $BASE_DIR/DELIVERY_GATE_REPORT.md"
echo "========================================="
