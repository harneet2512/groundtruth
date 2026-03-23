#!/bin/bash
set -euo pipefail

# Leaderboard GT Run — Qwen3-Coder via Vertex AI + gt_check hardgate
# Injects gt_tool_check_only.py into containers, uses gt_check_hardgate.j2 prompt.
# Usage: bash oh_run_leaderboard_gt.sh [--select instances.txt] [extra args...]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="$OH_DIR/.llm_config/vertex_qwen3.json"
OUTPUT_DIR="$HOME/results/leaderboard/gt"
GT_TOOL_PATH="$REPO_DIR/benchmarks/swebench/gt_tool_check_only.py"

# Pre-flight checks
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000."
    echo "Start it: bash $SCRIPT_DIR/oh_setup_proxy.sh"
    exit 1
fi
echo "Proxy: OK"

if [ ! -f "$LLM_CONFIG" ]; then
    echo "ERROR: LLM config not found at $LLM_CONFIG"
    exit 1
fi

if [ ! -f "$GT_TOOL_PATH" ]; then
    echo "ERROR: gt_tool_check_only.py not found at $GT_TOOL_PATH"
    echo "Did you create the stripped check-only tool?"
    exit 1
fi
echo "GT tool: $GT_TOOL_PATH ($(wc -c < "$GT_TOOL_PATH") bytes)"

# Copy GT prompt template to oh-benchmarks prompts dir
cp "$SCRIPT_DIR/prompts/gt_check_hardgate.j2" "$OH_DIR/benchmarks/swebench/prompts/"
echo "Copied gt_check_hardgate.j2 to OpenHands prompts dir"

mkdir -p "$OUTPUT_DIR"

echo ""
echo "=== Leaderboard GT Run ==="
echo "Started: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
echo "Config: $LLM_CONFIG"
echo "GT tool: $GT_TOOL_PATH"
echo "Prompt: gt_check_hardgate.j2"
echo "NOTE: This is the GT condition — gt_check hardgate enabled."
echo ""

cd "$OH_DIR"
GT_TOOL_PATH="$GT_TOOL_PATH" uv run python "$SCRIPT_DIR/oh_gt_mount_wrapper.py" "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --workspace docker \
    --max-iterations 100 \
    --num-workers 4 \
    --prompt-path gt_check_hardgate.j2 \
    --output-dir "$OUTPUT_DIR" \
    "$@"

echo ""
echo "=== GT Run Complete ==="
echo "Finished: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
if [ -f "$OUTPUT_DIR/output.jsonl" ]; then
    echo "Tasks completed: $(wc -l < "$OUTPUT_DIR/output.jsonl")"
fi
