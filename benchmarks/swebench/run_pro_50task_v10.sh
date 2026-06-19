#!/bin/bash
# v10 Pro 50-Task A/B Comparison: Baseline vs GT hook delivery.
#
# Phase A: Baseline (no GT) — clean control
# Phase B: GT v10 (hook delivery) — same prompt template
# Phase C: Evaluate both with swebench harness
# Phase D: Run analysis script
#
# CRITICAL: Both conditions use the SAME prompt template (baseline yaml).
# The only difference is GT context appended after file edits in Phase B.
#
# Usage: bash run_pro_50task_v10.sh
# Run on VM with mini-swe-agent, LiteLLM proxy on :4000, swebench installed.
set -e

REPO_ROOT="${REPO_ROOT:-$HOME/groundtruth}"
cd "$REPO_ROOT"

# Source API keys
eval "$(grep '^export.*API_KEY' "$HOME/.bashrc" 2>/dev/null)" || true
export PYTHONPATH="${HOME}/mini-swe-agent/src:${PYTHONPATH:-}"

AB_DIR="benchmarks/swebench/results/pro_v10_50task_$(date +%Y%m%d_%H%M)"
mkdir -p "$AB_DIR"

MODEL="${MODEL:-openai/qwen3-coder}"
WORKERS="${WORKERS:-6}"
SLICE="${SLICE:-0:50}"

echo "============================================================"
echo "  v10 Pro 50-Task A/B: Baseline vs GT Hook"
echo "============================================================"
echo "Output:    $AB_DIR"
echo "Model:     $MODEL"
echo "Workers:   $WORKERS"
echo "Slice:     $SLICE"
echo "Started:   $(date)"
echo ""

# ─── Phase A: Baseline (no GT) ───
echo "=== Phase A: BASELINE (no GT) ==="
echo "Config: mini_swebench_pro_baseline.yaml"
echo "Started: $(date)"

python3 benchmarks/swebench/run_v7_baseline.py \
  -c benchmarks/swebench/mini_swebench_pro_baseline.yaml \
  -m "$MODEL" \
  --subset ScaleAI/SWE-bench_Pro --split test \
  --slice "$SLICE" \
  -o "$AB_DIR/baseline" \
  -w "$WORKERS" \
  2>&1 | tee "$AB_DIR/baseline_run.log"

echo "Baseline complete: $(date)"
echo ""

# ─── Phase B: GT v10 (hook delivery) ───
echo "=== Phase B: GT v10 (hook delivery) ==="
echo "Config: mini_swebench_pro_gt_v10_hooked.yaml (identical prompt to baseline)"
echo "Started: $(date)"

python3 benchmarks/swebench/run_mini_gt_hooked.py \
  -c benchmarks/swebench/mini_swebench_pro_gt_v10_hooked.yaml \
  -m "$MODEL" \
  --subset ScaleAI/SWE-bench_Pro --split test \
  --slice "$SLICE" \
  -o "$AB_DIR/gt_v10" \
  -w "$WORKERS" \
  2>&1 | tee "$AB_DIR/gt_v10_run.log"

echo "GT v10 complete: $(date)"
echo ""

# ─── Phase C: Evaluate with swebench ───
echo "=== Phase C: Evaluation ==="

BASELINE_PREDS="$AB_DIR/baseline/preds.json"
GT_PREDS="$AB_DIR/gt_v10/preds.json"

if [ -f "$BASELINE_PREDS" ] && [ -f "$GT_PREDS" ]; then
  echo "Evaluating baseline..."
  python3 -m swe_bench_pro_eval \
    --patch_path="$BASELINE_PREDS" \
    --output_dir="$AB_DIR/eval_baseline" \
    --num_workers=8 \
    --use_local_docker 2>&1 | tee "$AB_DIR/eval_baseline.log" || echo "Baseline eval failed"

  echo ""
  echo "Evaluating GT v10..."
  python3 -m swe_bench_pro_eval \
    --patch_path="$GT_PREDS" \
    --output_dir="$AB_DIR/eval_gt_v10" \
    --num_workers=8 \
    --use_local_docker 2>&1 | tee "$AB_DIR/eval_gt_v10.log" || echo "GT eval failed"
else
  echo "WARNING: Missing predictions files."
  [ ! -f "$BASELINE_PREDS" ] && echo "  Missing: $BASELINE_PREDS"
  [ ! -f "$GT_PREDS" ] && echo "  Missing: $GT_PREDS"
fi

echo ""

# ─── Phase D: Analysis ───
echo "=== Phase D: Analysis ==="
if [ -f "benchmarks/swebench/analyze_pro_v10.py" ]; then
  python3 benchmarks/swebench/analyze_pro_v10.py \
    --ab-dir="$AB_DIR" \
    2>&1 | tee "$AB_DIR/analysis.log"
else
  echo "Analysis script not found. Run manually:"
  echo "  python3 benchmarks/swebench/analyze_pro_v10.py --ab-dir=$AB_DIR"
fi

echo ""
echo "============================================================"
echo "  50-Task A/B Complete"
echo "============================================================"
echo "Results at: $AB_DIR"
echo "Finished:   $(date)"
