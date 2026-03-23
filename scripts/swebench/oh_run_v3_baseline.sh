#!/bin/bash
set -euo pipefail

# V3 Baseline Run — SWE-bench Lite (no GT tools)
# Identical scaffold, same model, same params — just no GT tool injection.
# Usage: bash oh_run_v3_baseline.sh [--select instances.txt] [extra args...]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="$OH_DIR/.llm_config/vertex_qwen3.json"
OUTPUT_DIR="$HOME/results/v3/baseline"

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

mkdir -p "$OUTPUT_DIR"

echo "=== V3 Baseline Run (SWE-bench Lite) ==="
echo "Started: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
echo "Config: $LLM_CONFIG"
echo "NOTE: This is the BASELINE condition — no GT tools."
echo ""

cd "$OH_DIR"
uv run swebench-infer "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --workspace docker \
    --max-iterations 100 \
    --num-workers 4 \
    --output-dir "$OUTPUT_DIR" \
    "$@"

echo ""
echo "=== V3 Baseline Run Complete ==="
echo "Finished: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
if [ -f "$OUTPUT_DIR/output.jsonl" ]; then
    echo "Tasks completed: $(wc -l < "$OUTPUT_DIR/output.jsonl")"
fi
