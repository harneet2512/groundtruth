#!/usr/bin/env bash
# Benchmark ladder: 10-task → 50-task → full run.
#
# Usage:
#   bash scripts/swebench/run_benchmark_ladder.sh stage1          # 10-task warm
#   bash scripts/swebench/run_benchmark_ladder.sh stage2          # 50-task stability
#   bash scripts/swebench/run_benchmark_ladder.sh full            # full 300-task A/B
#   bash scripts/swebench/run_benchmark_ladder.sh full --workers 8 # override workers
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

STAGE="${1:-stage1}"
shift || true

WORKERS=""
for arg in "$@"; do
    case $arg in
        --workers) WORKERS="$2"; shift 2 ;;
    esac
done

source "$HOME/.local/bin/env" 2>/dev/null || true

run_condition() {
    local CONDITION="$1"  # "gt" or "baseline"
    local INSTANCES="$2"
    local N_WORKERS="$3"
    local OUTPUT_DIR="$4"

    echo ""
    echo "=== Running $CONDITION condition ==="
    echo "Workers: $N_WORKERS"
    echo "Output: $OUTPUT_DIR"

    local RUNNER
    if [ "$CONDITION" = "gt" ]; then
        RUNNER="$SCRIPT_DIR/openhands_run_qwen_gt.sh"
    else
        RUNNER="$SCRIPT_DIR/openhands_run_qwen_baseline.sh"
    fi

    local ARGS=(--instances "$INSTANCES" --output-dir "$OUTPUT_DIR" --max-iterations 100)

    START=$(date +%s)
    bash "$RUNNER" "${ARGS[@]}" 2>&1 | tee "$OUTPUT_DIR/run.log" || true
    END=$(date +%s)
    ELAPSED=$(( END - START ))

    echo ""
    echo "=== $CONDITION condition complete ==="
    echo "Duration: ${ELAPSED}s"
    echo "Results: $OUTPUT_DIR"
}

# ── Kill criteria check ──────────────────────────────────────────────
check_kill_criteria() {
    local DIR="$1"
    local STAGE="$2"

    # Check disk usage
    local DISK_USED=$(du -sm "$DIR" 2>/dev/null | awk '{print $1}')
    echo "Disk used: ${DISK_USED}MB"

    # Check for crashes
    local CRASH_COUNT=$(grep -c -i "error\|crash\|timeout" "$DIR/run.log" 2>/dev/null || echo 0)
    echo "Error/crash mentions: $CRASH_COUNT"

    # Check gt_check adoption (GT runs only)
    if [ -d "$DIR" ]; then
        local GT_CALLS=$(grep -rl "groundtruth_check" "$DIR" 2>/dev/null | wc -l)
        echo "gt_check adoption: $GT_CALLS tasks"
    fi
}

case "$STAGE" in
    stage1)
        echo "═══════════════════════════════════════════"
        echo "  STAGE 1: 10-task warm test"
        echo "  Workers: ${WORKERS:-2}"
        echo "  Expected: ~45 minutes"
        echo "═══════════════════════════════════════════"

        # Pick 10 diverse tasks
        INSTANCES=$(mktemp /tmp/stage1_XXXXXX.txt)
        cat > "$INSTANCES" << 'EOF'
django__django-12856
django__django-14608
django__django-10914
django__django-11179
django__django-12286
sympy__sympy-17655
sympy__sympy-18057
requests__requests-3362
flask__flask-4045
scikit-learn__scikit-learn-13496
EOF

        GT_OUT="$REPO_DIR/results/stage1_gt_$TIMESTAMP"
        BASELINE_OUT="$REPO_DIR/results/stage1_baseline_$TIMESTAMP"
        mkdir -p "$GT_OUT" "$BASELINE_OUT"

        run_condition "gt" "$INSTANCES" "${WORKERS:-2}" "$GT_OUT"
        check_kill_criteria "$GT_OUT" "stage1"

        run_condition "baseline" "$INSTANCES" "${WORKERS:-2}" "$BASELINE_OUT"
        check_kill_criteria "$BASELINE_OUT" "stage1"

        rm -f "$INSTANCES"

        echo ""
        echo "═══════════════════════════════════════════"
        echo "  STAGE 1 COMPLETE"
        echo "  GT results:       $GT_OUT"
        echo "  Baseline results: $BASELINE_OUT"
        echo "  Check pass criteria before proceeding to stage2"
        echo "═══════════════════════════════════════════"
        ;;

    stage2)
        echo "═══════════════════════════════════════════"
        echo "  STAGE 2: 50-task stability slice"
        echo "  Workers: ${WORKERS:-5}"
        echo "  Expected: ~2.5 hours"
        echo "═══════════════════════════════════════════"

        # Use first 50 tasks from SWE-bench Lite
        INSTANCES=$(mktemp /tmp/stage2_XXXXXX.txt)
        cd "$OH_DIR"
        uv run python3 -c "
from datasets import load_dataset
ds = load_dataset('princeton-nlp/SWE-bench_Lite', split='test')
for row in list(ds)[:50]:
    print(row['instance_id'])
" > "$INSTANCES" 2>/dev/null

        GT_OUT="$REPO_DIR/results/stage2_gt_$TIMESTAMP"
        BASELINE_OUT="$REPO_DIR/results/stage2_baseline_$TIMESTAMP"
        mkdir -p "$GT_OUT" "$BASELINE_OUT"

        run_condition "gt" "$INSTANCES" "${WORKERS:-5}" "$GT_OUT"
        check_kill_criteria "$GT_OUT" "stage2"

        run_condition "baseline" "$INSTANCES" "${WORKERS:-5}" "$BASELINE_OUT"
        check_kill_criteria "$BASELINE_OUT" "stage2"

        rm -f "$INSTANCES"

        echo ""
        echo "═══════════════════════════════════════════"
        echo "  STAGE 2 COMPLETE"
        echo "  GT results:       $GT_OUT"
        echo "  Baseline results: $BASELINE_OUT"
        echo "═══════════════════════════════════════════"
        ;;

    full)
        echo "═══════════════════════════════════════════"
        echo "  FULL BENCHMARK: 300 tasks A/B"
        echo "  Workers: ${WORKERS:-8}"
        echo "  Expected: ~6 hours"
        echo "═══════════════════════════════════════════"

        GT_OUT="$REPO_DIR/results/full_gt_$TIMESTAMP"
        BASELINE_OUT="$REPO_DIR/results/full_baseline_$TIMESTAMP"
        mkdir -p "$GT_OUT" "$BASELINE_OUT"

        # Full dataset — no instance filter
        run_condition "gt" "" "${WORKERS:-8}" "$GT_OUT"
        check_kill_criteria "$GT_OUT" "full"

        run_condition "baseline" "" "${WORKERS:-8}" "$BASELINE_OUT"
        check_kill_criteria "$BASELINE_OUT" "full"

        echo ""
        echo "═══════════════════════════════════════════"
        echo "  FULL BENCHMARK COMPLETE"
        echo "  GT results:       $GT_OUT"
        echo "  Baseline results: $BASELINE_OUT"
        echo "═══════════════════════════════════════════"
        ;;

    *)
        echo "Usage: $0 {stage1|stage2|full} [--workers N]"
        exit 1
        ;;
esac
