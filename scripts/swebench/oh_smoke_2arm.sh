#!/bin/bash
set -euo pipefail

# 2-Arm Evidence Delivery Smoke Gate (baseline + GT v1.1)
# Proves GT evidence is generated, injected, and visible before any larger run.
#
# Usage: bash oh_smoke_2arm.sh [--num-workers 2] [--max-iterations 50]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="qwen3"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BASE_DIR="$HOME/results/delivery_gate_${TIMESTAMP}"
MODEL="vertex_ai/qwen3-coder-480b"
START_TIME=$(date +%s)

INSTANCES="django__django-10097,django__django-10554,django__django-10880,django__django-10914,django__django-10973,django__django-11066,django__django-11087,django__django-11095,django__django-11099,django__django-11133"
NUM_WORKERS=2
MAX_ITERATIONS=50

while [[ $# -gt 0 ]]; do
    case "$1" in
        --num-workers) NUM_WORKERS="$2"; shift 2 ;;
        --max-iterations) MAX_ITERATIONS="$2"; shift 2 ;;
        --instances) INSTANCES="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

TASK_COUNT=$(echo "$INSTANCES" | tr ',' '\n' | wc -l)

phase_header() {
    echo ""
    echo "============================================================"
    echo "  Phase $1: $2"
    echo "  $(date -u) UTC"
    echo "============================================================"
}

phase_done() {
    echo "  Phase $1 complete."
}

write_metadata() {
    local dir="$1" condition="$2" gt_version="$3"
    cat > "$dir/METADATA.json" << METAEOF
{
  "date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "condition": "$condition",
  "gt_version": "$gt_version",
  "task_count": $TASK_COUNT,
  "num_workers": $NUM_WORKERS,
  "max_iterations": $MAX_ITERATIONS,
  "model": "$MODEL",
  "scaffold": "openhands",
  "gt_branch": "$(cd $REPO_DIR && git branch --show-current 2>/dev/null || echo 'unknown')",
  "gt_commit": "$(cd $REPO_DIR && git rev-parse HEAD 2>/dev/null || echo 'unknown')",
  "oh_version": "$(cd $OH_DIR && git describe --tags 2>/dev/null || echo 'unknown')",
  "hostname": "$(hostname)"
}
METAEOF
}

echo ""
echo "============================================================"
echo "  OpenHands Evidence Delivery Smoke Gate (2-arm)"
echo "  Started: $(date -u) UTC"
echo "  Base dir: $BASE_DIR"
echo "  Tasks: $TASK_COUNT"
echo "  Workers: $NUM_WORKERS"
echo "  Max iterations: $MAX_ITERATIONS"
echo "  Arms: baseline, gt_v11 (hook)"
echo "============================================================"

# ============================================================
# Phase 0: Pre-flight
# ============================================================
phase_header 0 "Pre-flight checks"

echo "Checking proxy..."
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "  WARN: litellm proxy not running on port 4000."
    echo "  Attempting direct Vertex AI instead."
fi

echo "Checking gt_hook.py..."
GT_HOOK="$REPO_DIR/benchmarks/swebench/gt_hook.py"
if [ ! -f "$GT_HOOK" ]; then
    # Check home dir fallback
    GT_HOOK="$HOME/gt_hook.py"
fi
if [ ! -f "$GT_HOOK" ]; then
    echo "FATAL: gt_hook.py not found"
    exit 1
fi
echo "  gt_hook.py: $(wc -c < "$GT_HOOK") bytes"

echo "Checking LLM config..."
cd "$OH_DIR"
export PATH="$HOME/.local/bin:$PATH"
source .venv/bin/activate
python -c "from openhands.core.config.utils import get_llm_config_arg; c=get_llm_config_arg('$LLM_CONFIG'); print(f'  model: {c.model}')" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "FATAL: LLM config '$LLM_CONFIG' not found in config.toml"
    exit 1
fi
echo "  llm_config: OK"

echo "Checking disk..."
df -h / | tail -1
echo ""

echo "Creating output directories..."
mkdir -p "$BASE_DIR/baseline"
mkdir -p "$BASE_DIR/gt_v11/gt_logs"
echo "  output dir: $BASE_DIR"

phase_done 0

# ============================================================
# Phase 1: Arm A — Baseline (no GT)
# ============================================================
phase_header 1 "Arm A — Baseline"

cd "$OH_DIR"

python evaluation/benchmarks/swe_bench/run_infer.py \
    --llm-config "$LLM_CONFIG" \
    --max-iterations "$MAX_ITERATIONS" \
    --eval-num-workers "$NUM_WORKERS" \
    --eval-output-dir "$BASE_DIR/baseline" \
    --split test \
    --dataset princeton-nlp/SWE-bench_Verified \
    --eval-ids $(echo $INSTANCES | tr ',' ' ') \
    2>&1 | tee "$BASE_DIR/baseline/run.log" || true

write_metadata "$BASE_DIR/baseline" "baseline" "none"
phase_done 1

# ============================================================
# Phase 2: Arm B — GT v1.1 (gt_hook passive)
# ============================================================
phase_header 2 "Arm B — GT v1.1 (hook)"

cd "$OH_DIR"
GT_LOG_DIR="$BASE_DIR/gt_v11/gt_logs" \
GT_HOOK_PATH="$HOME/gt_hook.py" \
python "$HOME/oh_gt_v11_runner.py" \
    --llm-config "$LLM_CONFIG" \
    --max-iterations "$MAX_ITERATIONS" \
    --eval-num-workers "$NUM_WORKERS" \
    --eval-output-dir "$BASE_DIR/gt_v11" \
    --split test \
    --dataset princeton-nlp/SWE-bench_Verified \
    --filter-instances "$INSTANCES" \
    2>&1 | tee "$BASE_DIR/gt_v11/run.log" || true

write_metadata "$BASE_DIR/gt_v11" "gt_v11" "1.1"
phase_done 2

# ============================================================
# Phase 3: Delivery gate scan
# ============================================================
phase_header 3 "Delivery gate scan"

python3 "$HOME/oh_delivery_gate.py" scan \
    --baseline "$BASE_DIR/baseline" \
    --gt-v11 "$BASE_DIR/gt_v11" --gt-v11-logs "$BASE_DIR/gt_v11/gt_logs" \
    --instances "$INSTANCES" \
    --model "$MODEL" \
    --telemetry-out "$BASE_DIR/delivery_telemetry.jsonl"

phase_done 3

# ============================================================
# Phase 4: Gate verification (STOP ON FAILURE)
# ============================================================
phase_header 4 "Gate verification"

GATE_EXIT=0
python3 "$HOME/oh_delivery_gate.py" verify \
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
phase_done 4

# ============================================================
# Phase 5: Report
# ============================================================
phase_header 5 "Final report"

python3 "$HOME/oh_delivery_report.py" \
    --telemetry "$BASE_DIR/delivery_telemetry.jsonl" \
    --gate-report "$BASE_DIR/DELIVERY_GATE_REPORT.md" \
    --baseline-meta "$BASE_DIR/baseline/METADATA.json" \
    --gt-v11-meta "$BASE_DIR/gt_v11/METADATA.json" \
    --out-dir "$BASE_DIR" || true

phase_done 5

# ============================================================
# Summary
# ============================================================
END_TIME=$(date +%s)
ELAPSED=$(( END_TIME - START_TIME ))
ELAPSED_MIN=$(( ELAPSED / 60 ))
ELAPSED_SEC=$(( ELAPSED % 60 ))

echo ""
echo "============================================================"
echo "  Smoke Gate Complete"
echo "  Finished: $(date -u) UTC"
echo "  Elapsed: ${ELAPSED_MIN}m ${ELAPSED_SEC}s"
echo "  Results: $BASE_DIR"
echo "============================================================"
echo ""
echo "Files:"
ls -la "$BASE_DIR"/*.md "$BASE_DIR"/*.jsonl "$BASE_DIR"/*.csv 2>/dev/null
