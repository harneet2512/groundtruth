#!/usr/bin/env bash
# Run OpenHands SWE-bench inference — Baseline (no GT tools) with Vertex AI Qwen3-Coder.
#
# Usage:
#   bash scripts/swebench/openhands_run_vertex_baseline.sh [options]
#   bash scripts/swebench/openhands_run_vertex_baseline.sh --instances "django__django-12856,django__django-14608"
#   bash scripts/swebench/openhands_run_vertex_baseline.sh --output-dir results/baseline --max-iterations 300
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"

# Copy baseline prompt template
BASELINE_PROMPT_SRC="$REPO_DIR/benchmarks/swebench/prompts/baseline_vertex.j2"
BASELINE_PROMPT_DST="$OH_DIR/benchmarks/swebench/prompts/baseline_vertex.j2"
if [ -f "$BASELINE_PROMPT_SRC" ]; then
    cp "$BASELINE_PROMPT_SRC" "$BASELINE_PROMPT_DST"
    echo "Copied baseline prompt to $BASELINE_PROMPT_DST"
else
    echo "ERROR: Baseline prompt not found at $BASELINE_PROMPT_SRC"
    exit 1
fi

# Parse arguments
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
echo "=== Running OpenHands SWE-bench Baseline (Vertex AI Qwen3-Coder) ==="
echo "Prompt: baseline_vertex.j2"
echo "Max iterations: $MAX_ITER"
[ -n "$INSTANCES" ] && echo "Instances: $INSTANCES"
[ -n "$OUTPUT_DIR" ] && echo "Output: $OUTPUT_DIR"
echo ""

CMD=(uv run swebench-infer
    .llm_config/vertex_qwen3.json
    --dataset princeton-nlp/SWE-bench_Lite
    --split test
    --max-iterations "$MAX_ITER"
    --prompt-path baseline_vertex.j2
    --workspace docker
    --n-critic-runs 1
    --max-retries 1
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
