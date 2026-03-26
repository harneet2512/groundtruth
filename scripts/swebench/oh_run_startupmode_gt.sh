#!/bin/bash
set -euo pipefail

# OpenHands GT Startupmode Run — Passive hooks, Qwen3-Coder via Vertex AI
# Injects gt_tool_v4.py with transparent read/write hooks.
# Agent never sees GT tools in prompt. GT enriches tool responses silently.
#
# Usage: bash oh_run_startupmode_gt.sh [--hooks write-only|both] [--num-workers 5] [extra args...]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="$OH_DIR/.llm_config/vertex_qwen3.json"
OUTPUT_DIR="$HOME/results/startupmode/gt"
GT_TOOL_PATH="$REPO_DIR/benchmarks/swebench/gt_tool_v4.py"

# Default: write-only hooks (Experiment A)
HOOKS_MODE="write-only"
NUM_WORKERS=5

# Parse our flags (pass rest through to OpenHands)
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --hooks=*)
            HOOKS_MODE="${1#*=}"
            shift
            ;;
        --hooks)
            HOOKS_MODE="$2"
            shift 2
            ;;
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

# Verify gt_tool_v4.py exists
if [ ! -f "$GT_TOOL_PATH" ]; then
    echo "ERROR: gt_tool_v4.py not found at $GT_TOOL_PATH"
    exit 1
fi

# Export for wrapper
export GT_TOOL_PATH

# Copy prompt template to oh-benchmarks prompts dir
cp "$SCRIPT_DIR/prompts/gt_startupmode.j2" "$OH_DIR/benchmarks/swebench/prompts/"
echo "Copied gt_startupmode.j2 to OpenHands prompts dir"

mkdir -p "$OUTPUT_DIR"

echo ""
echo "=== OpenHands GT Startupmode Run ==="
echo "Started: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
echo "Config: $LLM_CONFIG"
echo "GT tool: $GT_TOOL_PATH"
echo "Hooks: $HOOKS_MODE"
echo "Workers: $NUM_WORKERS"
echo ""

cd "$OH_DIR"
uv run python "$SCRIPT_DIR/oh_gt_startupmode_wrapper.py" "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --workspace docker \
    --max-iterations 100 \
    --num-workers "$NUM_WORKERS" \
    --prompt-path gt_startupmode.j2 \
    --output-dir "$OUTPUT_DIR" \
    --hooks="$HOOKS_MODE" \
    "${EXTRA_ARGS[@]}"

echo ""
echo "=== GT Startupmode Run Complete ==="
echo "Finished: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
if [ -f "$OUTPUT_DIR/output.jsonl" ]; then
    echo "Tasks completed: $(wc -l < "$OUTPUT_DIR/output.jsonl")"
fi
