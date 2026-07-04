#!/bin/bash
set -euo pipefail

# OpenHands Startupmode Baseline — No GT, same prompt as GT condition
# Fresh baseline for A/B comparison with the GT startupmode run.
#
# Usage: bash oh_run_startupmode_baseline.sh [--num-workers 5] [extra args...]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="$OH_DIR/.llm_config/vertex_qwen3.json"
OUTPUT_DIR="$HOME/results/startupmode/baseline"

NUM_WORKERS=5

# Parse flags
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --num-workers)
            NUM_WORKERS="$2"
            shift 2
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

# Ensure proxy is running
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000."
    echo "Start it: bash $SCRIPT_DIR/oh_setup_proxy.sh"
    exit 1
fi
echo "Proxy: OK"

# Verify LLM config exists
if [ ! -f "$LLM_CONFIG" ]; then
    echo "ERROR: LLM config not found at $LLM_CONFIG"
    exit 1
fi

# Copy prompt template (same as GT condition — no GT references)
cp "$SCRIPT_DIR/prompts/gt_startupmode.j2" "$OH_DIR/benchmarks/swebench/prompts/"
echo "Copied gt_startupmode.j2 to OpenHands prompts dir"

mkdir -p "$OUTPUT_DIR"

echo ""
echo "=== OpenHands Startupmode Baseline ==="
echo "Started: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
echo "Config: $LLM_CONFIG"
echo "Workers: $NUM_WORKERS"
echo "NOTE: No GT hooks. Clean baseline."
echo ""

cd "$OH_DIR"
uv run python -m benchmarks.swebench.run_infer "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --workspace docker \
    --max-iterations 100 \
    --num-workers "$NUM_WORKERS" \
    --prompt-path gt_startupmode.j2 \
    --output-dir "$OUTPUT_DIR" \
    "${EXTRA_ARGS[@]}"

echo ""
echo "=== Baseline Run Complete ==="
echo "Finished: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
if [ -f "$OUTPUT_DIR/output.jsonl" ]; then
    echo "Tasks completed: $(wc -l < "$OUTPUT_DIR/output.jsonl")"
fi
