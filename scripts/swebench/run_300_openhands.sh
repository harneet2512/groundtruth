#!/usr/bin/env bash
# Full 300-task A/B run on OpenHands: Baseline vs GT Phase 3.
#
# Usage:
#   bash scripts/swebench/run_300_openhands.sh               # sequential
#   bash scripts/swebench/run_300_openhands.sh --gt-only      # GT only
#   bash scripts/swebench/run_300_openhands.sh --baseline-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"
WORKERS="${OH_WORKERS:-4}"

source "$HOME/.local/bin/env" 2>/dev/null || true
source "$HOME/gt-env.sh" 2>/dev/null || true

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/oh_300_${TIMESTAMP}"
BASELINE_DIR="$OUTPUT_DIR/baseline"
GT_DIR="$OUTPUT_DIR/gt"
mkdir -p "$BASELINE_DIR" "$GT_DIR"

LLM_CONFIG="$OH_DIR/.llm_config/openai_gpt54nano.json"

echo "=== OpenHands 300-Task A/B Run ==="
echo "Output:  $OUTPUT_DIR"
echo "Workers: $WORKERS"
echo "LLM:     $LLM_CONFIG"
echo "Started: $(date)"

if [ ! -f "$LLM_CONFIG" ]; then
    echo "ERROR: LLM config not found. Run openhands_setup_vm.sh first."
    exit 1
fi

# ── Base64-encode gt_tool.py ──────────────────────────────────────────
GT_TOOL="$REPO_DIR/benchmarks/swebench/gt_tool.py"
GT_B64=$(base64 -w0 "$GT_TOOL")
GT_SETUP_CMD="echo '$GT_B64' | base64 -d > /tmp/gt_tool.py && chmod +x /tmp/gt_tool.py"

# Copy GT prompt
cp "$REPO_DIR/benchmarks/swebench/prompts/gt_phase3.j2" \
   "$OH_DIR/benchmarks/swebench/prompts/gt_phase3.j2" 2>/dev/null || true

cd "$OH_DIR"

RUN_BASELINE=true
RUN_GT=true
if [ "${1:-}" = "--gt-only" ]; then RUN_BASELINE=false; fi
if [ "${1:-}" = "--baseline-only" ]; then RUN_GT=false; fi

# ── Condition A: Baseline ─────────────────────────────────────────────
if [ "$RUN_BASELINE" = true ]; then
    echo ""
    echo "=========================================="
    echo "=== Condition A: Baseline (no GT) ==="
    echo "=========================================="
    echo "Started: $(date)"

    uv run swebench-infer "$LLM_CONFIG" \
        --dataset princeton-nlp/SWE-bench_Lite --split test \
        --workspace docker \
        --max-iterations 300 \
        --num-workers "$WORKERS" \
        --prompt-path default.j2 \
        --output-dir "$BASELINE_DIR" \
        2>&1 | tee "$BASELINE_DIR/run.log"

    echo "Baseline finished: $(date)"
fi

# ── Condition B: GT Phase 3 ──────────────────────────────────────────
if [ "$RUN_GT" = true ]; then
    echo ""
    echo "=========================================="
    echo "=== Condition B: GT Phase 3 ==="
    echo "=========================================="
    echo "Started: $(date)"

    # Create GT injection wrapper
    GT_WRAPPER=$(mktemp /tmp/gt_oh_300_XXXXXX.py)
    cat > "$GT_WRAPPER" << PYEOF
import sys, os
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

GT_SETUP_CMD = '''$GT_SETUP_CMD'''

import benchmarks.swebench.run_infer as run_infer_mod
_orig_prepare = run_infer_mod.SWEBenchEvaluation.prepare_workspace

def _patched_prepare(self, instance, *args, **kwargs):
    workspace = _orig_prepare(self, instance, *args, **kwargs)
    result = workspace.execute_command(GT_SETUP_CMD)
    if result.exit_code == 0:
        print(f"[GT] gt_tool.py injected for {instance.id}")
    else:
        print(f"[GT] WARNING: injection failed for {instance.id}: {result.stderr}")
    return workspace

run_infer_mod.SWEBenchEvaluation.prepare_workspace = _patched_prepare
run_infer_mod.main()
PYEOF

    uv run python "$GT_WRAPPER" "$LLM_CONFIG" \
        --dataset princeton-nlp/SWE-bench_Lite --split test \
        --workspace docker \
        --max-iterations 300 \
        --num-workers "$WORKERS" \
        --prompt-path gt_phase3.j2 \
        --output-dir "$GT_DIR" \
        2>&1 | tee "$GT_DIR/run.log"

    rm -f "$GT_WRAPPER"
    echo "GT finished: $(date)"
fi

# ── Post-Run Summary ──────────────────────────────────────────────────
echo ""
echo "============================================"
echo "=== POST-RUN SUMMARY ==="
echo "============================================"

for label_dir in "Baseline:$BASELINE_DIR" "GT_Phase3:$GT_DIR"; do
    label="${label_dir%%:*}"
    dir="${label_dir##*:}"
    count=$(find "$dir" -name "*.json" -o -name "*.jsonl" 2>/dev/null | wc -l)
    echo "$label: $count output files in $dir"
done

echo ""
echo "=== Next Steps ==="
echo "1. Run Docker evaluation:"
echo "   # Convert OpenHands output to SWE-bench predictions format"
echo "   # Then evaluate:"
echo "   bash $REPO_DIR/scripts/swebench/run_eval.sh $BASELINE_DIR/preds.json baseline_oh"
echo "   bash $REPO_DIR/scripts/swebench/run_eval.sh $GT_DIR/preds.json phase3_oh"
echo ""
echo "Output: $OUTPUT_DIR"
echo "Finished: $(date)"
