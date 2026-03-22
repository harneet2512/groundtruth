#!/usr/bin/env bash
# Orchestrate: pre-build images, then run inference on one VM.
#
# Usage:
#   bash scripts/swebench/prebuild_and_run.sh --shard /tmp/my_shard.txt --condition baseline
#   bash scripts/swebench/prebuild_and_run.sh --shard /tmp/my_shard.txt --condition gt --num-workers 4
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"

SHARD_FILE=""
CONDITION="baseline"
NUM_WORKERS=4
MAX_ITER=100
BUILD_WORKERS=2
OUTPUT_DIR=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --shard) SHARD_FILE="$2"; shift 2 ;;
        --condition) CONDITION="$2"; shift 2 ;;
        --num-workers) NUM_WORKERS="$2"; shift 2 ;;
        --build-workers) BUILD_WORKERS="$2"; shift 2 ;;
        --max-iterations) MAX_ITER="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

if [ -z "$SHARD_FILE" ] || [ ! -f "$SHARD_FILE" ]; then
    echo "ERROR: --shard <file> required"
    exit 1
fi

TASK_COUNT=$(wc -l < "$SHARD_FILE")
OUTPUT_DIR="${OUTPUT_DIR:-$HOME/results/v3/$CONDITION}"

echo "============================================"
echo "  SWE-bench: Pre-build + Run"
echo "  Condition: $CONDITION"
echo "  Shard: $SHARD_FILE ($TASK_COUNT tasks)"
echo "  Build workers: $BUILD_WORKERS"
echo "  Inference workers: $NUM_WORKERS"
echo "  Output: $OUTPUT_DIR"
echo "============================================"
echo ""

# ── Phase 1: Verify litellm ──────────────────────────────────────────
echo "=== Checking litellm proxy ==="
if ! curl -sf http://localhost:4000/health > /dev/null 2>&1; then
    echo "Starting litellm..."
    cat > /tmp/litellm_config.yaml << 'LITECFG'
model_list:
  - model_name: "qwen3-coder"
    litellm_params:
      model: "vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas"
      vertex_project: "regal-scholar-442803-e1"
      vertex_location: "global"
LITECFG
    cd "$OH_DIR"
    nohup uv run litellm --config /tmp/litellm_config.yaml --port 4000 --host 0.0.0.0 > /tmp/litellm.log 2>&1 &
    sleep 15
    curl -sf http://localhost:4000/health > /dev/null || { echo "FATAL: litellm failed to start"; exit 1; }
fi
echo "litellm OK"
echo ""

# ── Phase 2: Pre-build images ────────────────────────────────────────
echo "=== Pre-building images ($TASK_COUNT tasks, $BUILD_WORKERS workers) ==="
IMAGES_BEFORE=$(docker images | grep -c eval-agent-server 2>/dev/null || echo 0)
echo "Images before: $IMAGES_BEFORE"

cd "$OH_DIR"
export BUILDKIT_PRUNE_KEEP_GB=30

uv run python -m benchmarks.swebench.build_images \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --image ghcr.io/openhands/eval-agent-server \
    --target source-minimal \
    --max-workers "$BUILD_WORKERS" \
    --select "$SHARD_FILE"

IMAGES_AFTER=$(docker images | grep -c eval-agent-server 2>/dev/null || echo 0)
echo ""
echo "Images after: $IMAGES_AFTER"
echo "Disk usage:"
df -h / | tail -1
echo ""

# ── Phase 3: Run inference ────────────────────────────────────────────
echo "=== Running inference ($CONDITION, $NUM_WORKERS workers) ==="
mkdir -p "$OUTPUT_DIR"

# Copy prompt template
if [ "$CONDITION" = "gt" ]; then
    PROMPT="gt_phase3.j2"
    cp "$REPO_DIR/benchmarks/swebench/prompts/gt_phase3.j2" "$OH_DIR/benchmarks/swebench/prompts/"
else
    PROMPT="baseline_vertex.j2"
    cp "$REPO_DIR/benchmarks/swebench/prompts/baseline_vertex.j2" "$OH_DIR/benchmarks/swebench/prompts/"
fi

if [ "$CONDITION" = "gt" ]; then
    # GT: use the inject script for prompt-only Jinja2 patching
    export OH_DIR
    bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh" \
        --instances "$SHARD_FILE" \
        --output-dir "$OUTPUT_DIR" \
        --max-iterations "$MAX_ITER" \
        --num-workers "$NUM_WORKERS"
else
    # Baseline: direct swebench-infer
    OPENHANDS_SUPPRESS_BANNER=1 uv run swebench-infer \
        .llm_config/vertex_qwen3.json \
        --dataset princeton-nlp/SWE-bench_Lite \
        --split test \
        --max-iterations "$MAX_ITER" \
        --prompt-path "$PROMPT" \
        --workspace docker \
        --n-critic-runs 1 \
        --max-retries 1 \
        --num-workers "$NUM_WORKERS" \
        --select "$SHARD_FILE" \
        --output-dir "$OUTPUT_DIR"
fi

echo ""
echo "=== COMPLETE ==="
echo "Output: $OUTPUT_DIR"
echo "Disk:"
df -h / | tail -1
