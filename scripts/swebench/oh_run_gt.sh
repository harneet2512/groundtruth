#!/bin/bash
set -euo pipefail

# OpenHands GT gt_check Run — Qwen3-Coder via Vertex AI
# Injects gt_tool.py into containers, uses gt_check_only.j2 prompt
# Usage: bash oh_run_gt.sh [--select instances_a.txt] [extra args...]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="$OH_DIR/.llm_config/vertex_qwen3.json"
OUTPUT_DIR="$HOME/results/gt"
GT_TOOL_PATH="$REPO_DIR/benchmarks/swebench/gt_tool.py"

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

# Verify gt_tool.py exists
if [ ! -f "$GT_TOOL_PATH" ]; then
    echo "ERROR: gt_tool.py not found at $GT_TOOL_PATH"
    exit 1
fi

# Base64-encode gt_tool.py for container injection
export GT_TOOL_B64=$(base64 -w0 "$GT_TOOL_PATH")
echo "GT tool payload: ${#GT_TOOL_B64} bytes (base64)"

# Copy GT prompt template to oh-benchmarks prompts dir
cp "$SCRIPT_DIR/prompts/gt_check_only.j2" "$OH_DIR/benchmarks/swebench/prompts/"
echo "Copied gt_check_only.j2 to OpenHands prompts dir"

mkdir -p "$OUTPUT_DIR"

echo ""
echo "=== OpenHands GT gt_check Run ==="
echo "Started: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
echo "Config: $LLM_CONFIG"
echo "GT tool: $GT_TOOL_PATH"
echo ""

cd "$OH_DIR"
uv run python "$SCRIPT_DIR/oh_gt_check_wrapper.py" "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Verified \
    --split test \
    --workspace docker \
    --max-iterations 100 \
    --num-workers 4 \
    --prompt-path gt_check_only.j2 \
    --output-dir "$OUTPUT_DIR" \
    "$@"

echo ""
echo "=== GT Run Complete ==="
echo "Finished: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
if [ -f "$OUTPUT_DIR/output.jsonl" ]; then
    echo "Tasks completed: $(wc -l < "$OUTPUT_DIR/output.jsonl")"
fi
