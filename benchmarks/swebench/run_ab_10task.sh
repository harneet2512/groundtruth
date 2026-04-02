#!/bin/bash
# A/B test: 10 tasks, baseline vs GT with <gt-evidence> format.
#
# Usage: bash run_ab_10task.sh [MODEL]
# Default model: vertex_ai/gemini-2.5-flash on Vertex AI
set -e

MODEL="${1:-vertex_ai/gemini-2.5-flash}"
WORKERS=1  # sequential to avoid Vertex AI rate limits

REPO_ROOT="${REPO_ROOT:-$HOME/groundtruth}"
cd "$REPO_ROOT"
git pull origin new_imp 2>/dev/null || true

export PYTHONPATH="${HOME}/mini-swe-agent/src:${PYTHONPATH:-}"
source ~/gt-venv/bin/activate 2>/dev/null || true

AB_DIR="$HOME/eval_10task_$(date +%Y%m%d_%H%M)"
mkdir -p "$AB_DIR"

# 10 tasks — all django (fast, share env images)
TASKS=(
    "django__django-10097"
    "django__django-10554"
    "django__django-10880"
    "django__django-10914"
    "django__django-10973"
    "django__django-10999"
    "django__django-11066"
    "matplotlib__matplotlib-13989"
    "scikit-learn__scikit-learn-10297"
    "sympy__sympy-11618"
)

FILTER_REGEX=$(IFS='|'; echo "${TASKS[*]}")

echo "============================================================"
echo "  A/B Test: 10 Tasks — Baseline vs GT <gt-evidence>"
echo "============================================================"
echo "Output:    $AB_DIR"
echo "Tasks:     ${#TASKS[@]}"
echo "Model:     $MODEL"
echo "Workers:   $WORKERS"
echo "Started:   $(date)"
echo ""

# ─── Phase A: Baseline (no GT) ───
echo "=== Phase A: BASELINE (no GT) ==="
echo "Started: $(date)"

python3 -m minisweagent.run.benchmarks.swebench \
  -c benchmarks/swebench/mini_swebench_baseline.yaml \
  -m "$MODEL" \
  --subset princeton-nlp/SWE-bench_Verified --split test \
  --filter "$FILTER_REGEX" \
  -o "$AB_DIR/baseline" \
  -w "$WORKERS" \
  2>&1 | tee "$AB_DIR/baseline_run.log"

echo "Baseline complete: $(date)"
echo ""

# ─── Phase B: GT hooked (with <gt-evidence>) ───
echo "=== Phase B: GT HOOKED (<gt-evidence> format) ==="
echo "Started: $(date)"

python3 benchmarks/swebench/run_mini_gt_hooked.py \
  -c benchmarks/swebench/mini_swebench_gt.yaml \
  -m "$MODEL" \
  --subset princeton-nlp/SWE-bench_Verified --split test \
  --filter "$FILTER_REGEX" \
  -o "$AB_DIR/gt_hooked" \
  -w "$WORKERS" \
  2>&1 | tee "$AB_DIR/gt_hooked_run.log"

echo "GT hooked complete: $(date)"
echo ""

# ─── Phase C: Evaluate with swebench harness ───
echo "=== Phase C: Docker Evaluation ==="

for CONDITION in baseline gt_hooked; do
  PREDS_FILE="$AB_DIR/$CONDITION/preds.json"
  if [ ! -f "$PREDS_FILE" ]; then
    echo "[SKIP] No predictions for $CONDITION"
    continue
  fi

  echo "--- Evaluating: $CONDITION ---"
  python3 -m swebench.harness.run_evaluation \
    --predictions_path "$PREDS_FILE" \
    --swe_bench_tasks princeton-nlp/SWE-bench_Verified \
    --log_dir "$AB_DIR/$CONDITION/eval_logs" \
    --testbed /tmp/swebench_testbed \
    --skip_existing \
    --timeout 300 \
    2>&1 | tee "$AB_DIR/${CONDITION}_eval.log" || true
done

# ─── Phase D: Compare results ───
echo ""
echo "=== Phase D: Comparison ==="
python3 scripts/swebench/compare_ab_local.py "$AB_DIR" 2>&1 | tee "$AB_DIR/comparison.txt"

echo ""
echo "============================================================"
echo "  A/B Test Complete — $(date)"
echo "============================================================"
echo "Results: $AB_DIR/comparison.txt"
