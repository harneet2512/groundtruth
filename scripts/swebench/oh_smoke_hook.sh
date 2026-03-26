#!/bin/bash
set -euo pipefail

# Smoke test for gt_hook.py passive evidence hook.
# Runs 10 Django tasks with the new amalgamated hook injected.
# Gate: hook must fire (non-empty stdout) on >=3/10 tasks with no crashes.
#
# Usage:
#   bash oh_smoke_hook.sh [--num-workers 2] [--instances django1,django2,...]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="$OH_DIR/.llm_config/vertex_qwen3.json"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/results/smoke_hook_${TIMESTAMP}"
GT_LOG_DIR="$OUTPUT_DIR/gt_logs"
NUM_WORKERS=2

# 10 Django instances for smoke test
DEFAULT_INSTANCES="django__django-10097,django__django-10554,django__django-10880,django__django-10914,django__django-10973,django__django-11066,django__django-11087,django__django-11095,django__django-11099,django__django-11133"

INSTANCES="$DEFAULT_INSTANCES"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --num-workers) NUM_WORKERS="$2"; shift 2 ;;
        --instances)   INSTANCES="$2";  shift 2 ;;
        *) shift ;;
    esac
done

# Check proxy
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000."
    echo "Start it: bash $SCRIPT_DIR/oh_setup_proxy.sh"
    exit 1
fi
echo "Proxy: OK"

# Check gt_hook.py exists
GT_HOOK="$REPO_DIR/benchmarks/swebench/gt_hook.py"
if [ ! -f "$GT_HOOK" ]; then
    echo "ERROR: gt_hook.py not found at $GT_HOOK"
    exit 1
fi
echo "gt_hook.py: $(wc -c < "$GT_HOOK") bytes"

mkdir -p "$OUTPUT_DIR" "$GT_LOG_DIR"
export GT_LOG_DIR

echo ""
echo "================================================="
echo "  GT Hook Smoke Test"
echo "  Started:  $(date -u) UTC"
echo "  Output:   $OUTPUT_DIR"
echo "  Logs:     $GT_LOG_DIR"
echo "  Workers:  $NUM_WORKERS"
echo "  Tasks:    $(echo "$INSTANCES" | tr ',' '\n' | wc -l)"
echo "================================================="
echo ""

cd "$OH_DIR"
uv run python "$SCRIPT_DIR/oh_gt_hook_wrapper.py" "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --workspace docker \
    --max-iterations 50 \
    --num-workers "$NUM_WORKERS" \
    --filter-instances "$INSTANCES" \
    --output-dir "$OUTPUT_DIR" \
    2>&1 | tee "$OUTPUT_DIR/run.log"

echo ""
echo "================================================="
echo "  Smoke test run complete: $(date -u) UTC"
echo "================================================="
echo ""

# Analyze hook logs
if [ -d "$GT_LOG_DIR" ] && [ "$(ls -A "$GT_LOG_DIR" 2>/dev/null)" ]; then
    echo "Analyzing hook logs..."
    python3 "$SCRIPT_DIR/analyze_hook_logs.py" "$GT_LOG_DIR" --smoke-gate 3
else
    echo "WARNING: No hook logs found in $GT_LOG_DIR"
    echo "         Hook may not have fired or log extraction failed."
fi
