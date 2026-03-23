#!/usr/bin/env bash
# Run OpenHands SWE-bench with GroundTruth gt_check + Qwen3-Coder.
#
# Usage:
#   bash scripts/swebench/openhands_run_qwen_gt.sh [options]
#   bash scripts/swebench/openhands_run_qwen_gt.sh --instances "django__django-12856,django__django-14608"
#   bash scripts/swebench/openhands_run_qwen_gt.sh --output-dir results/gt --max-iterations 100
#
# Prerequisites:
#   1. OpenHands benchmarks cloned at $OH_DIR (default: ~/oh-benchmarks)
#   2. litellm proxy running (this script starts it if not running)
#   3. SWE-bench eval images built (with GT layer baked in)
#   4. Vertex AI credentials configured
#
# Qwen3-Coder parameters:
#   temperature=0.7, top_p=0.8     → OpenHands TOML config
#   top_k=20, repetition_penalty=1.05 → litellm proxy config
#   max_iterations=100              → OpenHands TOML config
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"

# ── Verify prerequisites ─────────────────────────────────────────────
if [ ! -d "$OH_DIR" ]; then
    echo "ERROR: OpenHands benchmarks not found at $OH_DIR"
    echo "Run: bash scripts/swebench/openhands_setup_vertex.sh"
    exit 1
fi

# ── Start litellm proxy if not running ────────────────────────────────
if ! curl -s http://localhost:4000/health > /dev/null 2>&1; then
    echo "Starting litellm proxy..."
    LITELLM_CONFIG="$SCRIPT_DIR/litellm_qwen_gtcheck.yaml"
    if [ ! -f "$LITELLM_CONFIG" ]; then
        echo "ERROR: litellm config not found at $LITELLM_CONFIG"
        exit 1
    fi
    nohup uv run litellm --config "$LITELLM_CONFIG" --port 4000 --host 0.0.0.0 > /tmp/litellm_proxy.log 2>&1 &
    echo "Waiting for proxy..."
    for i in $(seq 1 15); do
        if curl -s http://localhost:4000/health > /dev/null 2>&1; then
            echo "Proxy healthy"
            break
        fi
        sleep 2
    done
fi

# ── Copy prompt template ─────────────────────────────────────────────
GT_PROMPT_SRC="$REPO_DIR/benchmarks/swebench/prompts/gt_check_only.j2"
GT_PROMPT_DST="$OH_DIR/benchmarks/swebench/prompts/gt_check_only.j2"
mkdir -p "$(dirname "$GT_PROMPT_DST")"
if [ -f "$GT_PROMPT_SRC" ]; then
    cp "$GT_PROMPT_SRC" "$GT_PROMPT_DST"
    echo "Copied GT prompt template"
else
    echo "ERROR: GT prompt template not found at $GT_PROMPT_SRC"
    exit 1
fi

# ── Prepare OpenHands config ─────────────────────────────────────────
BRIDGE_PATH="$REPO_DIR/benchmarks/swebench/gt_mcp_bridge.py"
OH_CONFIG="$OH_DIR/openhands_config_qwen_gt.toml"
cp "$REPO_DIR/benchmarks/swebench/openhands_config_qwen_gt.toml" "$OH_CONFIG"
sed -i "s|BRIDGE_PATH_PLACEHOLDER|$BRIDGE_PATH|g" "$OH_CONFIG"
echo "OpenHands config ready at $OH_CONFIG"

# ── Parse arguments ──────────────────────────────────────────────────
EXTRA_ARGS=()
OUTPUT_DIR=""
INSTANCES=""
MAX_ITER="100"

while [[ $# -gt 0 ]]; do
    case $1 in
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --instances) INSTANCES="$2"; shift 2 ;;
        --max-iterations) MAX_ITER="$2"; shift 2 ;;
        *) EXTRA_ARGS+=("$1"); shift ;;
    esac
done

cd "$OH_DIR"
source ~/.local/bin/env 2>/dev/null || true

echo ""
echo "=== Running OpenHands SWE-bench with GroundTruth gt_check (Qwen3-Coder) ==="
echo "Mode: MCP bridge (gt_check only)"
echo "Model: Qwen3-Coder-480B via Vertex AI"
echo "Params: temp=0.7 top_p=0.8 top_k=20 rep_penalty=1.05"
echo "Max iterations: $MAX_ITER"
[ -n "$INSTANCES" ] && echo "Instances: $INSTANCES"
[ -n "$OUTPUT_DIR" ] && echo "Output: $OUTPUT_DIR"
echo ""

# ── Build run command ────────────────────────────────────────────────
CMD=(uv run swebench-infer
    .llm_config/qwen_gtcheck.json
    --dataset princeton-nlp/SWE-bench_Lite
    --split test
    --max-iterations "$MAX_ITER"
    --prompt-path gt_check_only.j2
    --workspace docker
    --config "$OH_CONFIG"
)

[ -n "$OUTPUT_DIR" ] && CMD+=(--output-dir "$OUTPUT_DIR")

# Handle --instances: file path or comma-separated list
if [ -n "$INSTANCES" ]; then
    if [ -f "$INSTANCES" ]; then
        CMD+=(--select "$INSTANCES")
    else
        SELECT_FILE=$(mktemp /tmp/gt_select_XXXXXX.txt)
        echo "$INSTANCES" | tr ',' '\n' > "$SELECT_FILE"
        CMD+=(--select "$SELECT_FILE")
    fi
fi

[ ${#EXTRA_ARGS[@]} -gt 0 ] && CMD+=("${EXTRA_ARGS[@]}")

"${CMD[@]}"

# Cleanup
[ -n "${SELECT_FILE:-}" ] && rm -f "$SELECT_FILE"
