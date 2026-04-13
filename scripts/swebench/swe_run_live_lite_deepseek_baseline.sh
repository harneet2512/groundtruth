#!/bin/bash
set -euo pipefail

# Canonical stage-1 baseline launcher:
# plain SWE-agent + DeepSeek V3.2 + SWE-bench-Live Lite
#
# Purpose:
#   - establish a believable non-GT baseline before reintroducing GT
#   - eliminate branch drift from mini-swe-agent-era DeepSeek configs

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_PATH="${CONFIG_PATH:-$SCRIPT_DIR/sweagent_deepseek_v32_live_lite_baseline.yaml}"
OUTPUT_DIR="${OUTPUT_DIR:-$HOME/results/sweagent_deepseek_v32_live_lite_baseline}"
DATASET="${DATASET:-SWE-bench-Live/SWE-bench-Live}"
SPLIT="${SPLIT:-lite}"
NUM_WORKERS="${NUM_WORKERS:-8}"
MAX_ITERATIONS="${MAX_ITERATIONS:-100}"

if ! command -v sweagent >/dev/null 2>&1; then
    echo "ERROR: sweagent CLI not found."
    echo "Install/configure real SWE-agent first, then rerun."
    exit 1
fi

if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000."
    echo "Start it: bash $SCRIPT_DIR/swe_setup_proxy_deepseek_v32.sh"
    exit 1
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo "ERROR: config not found at $CONFIG_PATH"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

echo "=== SWE-agent + DeepSeek V3.2 Live Lite BASELINE ==="
echo "Started: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
echo "Dataset: $DATASET"
echo "Split: $SPLIT"
echo "Workers: $NUM_WORKERS"
echo "Max iterations: $MAX_ITERATIONS"
echo "Parser: function_calling"
echo "Temperature: 1.0"
echo "Top-p: 0.95"
echo "GT: disabled"
echo ""

# This is intentionally plain baseline.
# Adjust CLI flags only to match the installed SWE-agent version if needed.
sweagent run-batch \
  --config "$CONFIG_PATH" \
  --output_dir "$OUTPUT_DIR" \
  --num_workers "$NUM_WORKERS" \
  --instances.type swe_bench \
  --instances.subset "$SPLIT" \
  --instances.split "$SPLIT" \
  --instances.dataset_name "$DATASET" \
  --agent.model.per_instance_call_limit 100 \
  --agent.model.temperature 1.0 \
  --agent.model.top_p 0.95 \
  --max_iterations "$MAX_ITERATIONS" \
  "$@"

echo ""
echo "=== Baseline run complete ==="
echo "Finished: $(date -u) UTC"
echo "Output: $OUTPUT_DIR"
