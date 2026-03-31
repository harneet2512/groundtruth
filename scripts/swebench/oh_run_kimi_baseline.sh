#!/bin/bash
set -euo pipefail

# OpenHands Baseline Run — Kimi K2-Thinking via Vertex AI MaaS
# Usage: bash oh_run_kimi_baseline.sh [--select instances.txt] [extra args...]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="$SCRIPT_DIR/oh_llm_config_vertex_kimi.json"
OUTPUT_DIR="$HOME/results/kimi-baseline"

# Ensure proxy is running
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000."
    echo "Start it: bash $SCRIPT_DIR/oh_setup_proxy_kimi.sh"
    exit 1
fi
echo "Proxy: OK"

mkdir -p "$OUTPUT_DIR"

echo "=== OpenHands Kimi K2-Thinking Baseline ==="
echo "Started: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
echo "Config: $LLM_CONFIG"
echo ""

cd "$OH_DIR"
uv run swebench-infer "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Verified \
    --split test \
    --workspace docker \
    --max-iterations 100 \
    --num-workers 2 \
    --output-dir "$OUTPUT_DIR" \
    "$@"

echo ""
echo "=== Kimi Baseline Complete ==="
echo "Finished: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
if [ -f "$OUTPUT_DIR/output.jsonl" ]; then
    echo "Tasks completed: $(wc -l < "$OUTPUT_DIR/output.jsonl")"
fi
