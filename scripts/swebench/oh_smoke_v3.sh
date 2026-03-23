#!/bin/bash
set -euo pipefail

# Smoke test for V3 3-endpoint architecture
# Runs 1 task baseline + 1 task GT to verify the pipeline works end-to-end.
# Usage: bash oh_smoke_v3.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="$OH_DIR/.llm_config/vertex_qwen3.json"

echo "=== V3 Smoke Test ==="
echo ""

# Pre-flight
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000."
    exit 1
fi
echo "Proxy: OK"

if [ ! -f "$LLM_CONFIG" ]; then
    echo "ERROR: LLM config not found at $LLM_CONFIG"
    exit 1
fi
echo "LLM config: OK"

GT_TOOL="$REPO_DIR/benchmarks/swebench/gt_tool_v3.py"
if [ ! -f "$GT_TOOL" ]; then
    echo "ERROR: gt_tool_v3.py not found"
    exit 1
fi
echo "GT tool v3: $(wc -c < "$GT_TOOL") bytes, $(wc -l < "$GT_TOOL") lines"

# Pick a known-good task for smoke testing
SMOKE_TASK="django__django-11039"

echo ""
echo "--- Smoke: Baseline (1 task) ---"
mkdir -p "$HOME/results/v3_smoke/baseline"
cd "$OH_DIR"
uv run swebench-infer "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --workspace docker \
    --max-iterations 30 \
    --num-workers 1 \
    --output-dir "$HOME/results/v3_smoke/baseline" \
    --select "$SMOKE_TASK" \
    2>&1 | tail -5
echo "Baseline smoke: DONE"

echo ""
echo "--- Smoke: GT v3 (1 task) ---"
cp "$SCRIPT_DIR/prompts/gt_v3_hardgate.j2" "$OH_DIR/benchmarks/swebench/prompts/"
mkdir -p "$HOME/results/v3_smoke/gt"
cd "$OH_DIR"
GT_TOOL_PATH="$GT_TOOL" uv run python "$SCRIPT_DIR/oh_gt_v3_wrapper.py" "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --workspace docker \
    --max-iterations 30 \
    --num-workers 1 \
    --prompt-path gt_v3_hardgate.j2 \
    --output-dir "$HOME/results/v3_smoke/gt" \
    --select "$SMOKE_TASK" \
    2>&1 | tail -10
echo "GT v3 smoke: DONE"

echo ""
echo "=== Smoke Test Complete ==="
echo "Baseline: $HOME/results/v3_smoke/baseline/"
echo "GT v3:    $HOME/results/v3_smoke/gt/"

# Quick comparison
for cond in baseline gt; do
    f="$HOME/results/v3_smoke/$cond/output.jsonl"
    if [ -f "$f" ]; then
        echo "$cond: $(wc -l < "$f") task(s) completed"
    else
        echo "$cond: no output.jsonl found"
    fi
done
