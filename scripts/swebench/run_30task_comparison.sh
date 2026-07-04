#!/bin/bash
# =============================================================================
# 30-Task Comparison Run: GT+Agent (L1+L3+L3b) vs Historical Baseline (4/30)
# =============================================================================
#
# COST ESTIMATE:
#   LLM:  30 tasks × max_iter=100 × ~$0.12/task = ~$3.60
#   VM:   ~2 hours total across both VMs at ~$1.50/hr = ~$3.00
#   Total: ~$6.60
#   Budget remaining before: ~$75
#   Budget after: ~$68.40
#
# PREREQUISITES:
#   - gt-t0 and gt-v1 VMs running
#   - litellm proxy on port 4000 (both VMs)
#   - groundtruth src + oh_gt_full_wrapper.py deployed
#   - graph.db pre-indexed for each task repo
#   - gcloud auth via metadata server (SA token)
#
# LAYERS ACTIVE: L1 (V1R brief) + L3 (post-edit evidence) + L3b (post-view nav)
# GT_PHASE=full enables all three
#
# BASELINE COMPARISON: D:\tmp\gt_test\results_final/baseline_t0.jsonl + baseline_v1.jsonl
# (4/30 resolved at max_iter=100, same 30 tasks, same model)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
export GT_PHASE="full"
export GT_MAX_ITER=100
export OPENAI_API_KEY="sk-gt-local"
export OPENAI_API_BASE="http://localhost:4000/v1"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RUN_ID="30task_comparison_${TIMESTAMP}"

# SA token from metadata server (reliable, no user credential issues)
TOKEN=$(curl -s -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
export VERTEX_TOKEN="$TOKEN"

# LLM config — Qwen3-Coder 480B on Vertex MaaS global endpoint
LLM_CONFIG="$REPO_DIR/.llm_config/vertex_qwen3_v105.json"

# --------------------------------------------------------------------------
# Task IDs — 30 SWE-bench Live Lite tasks
# --------------------------------------------------------------------------

# gt-t0 (20 tasks, --eval-num-workers 4)
TASKS_T0=(
  "aiogram__aiogram-1594"
  "aws-cloudformation__cfn-lint-3789"
  "aws-cloudformation__cfn-lint-3798"
  "aws-cloudformation__cfn-lint-3821"
  "aws-cloudformation__cfn-lint-3854"
  "aws-cloudformation__cfn-lint-3856"
  "aws-cloudformation__cfn-lint-3862"
  "aws-cloudformation__cfn-lint-3866"
  "aws-cloudformation__cfn-lint-3875"
  "aws-cloudformation__cfn-lint-3890"
  "aws-cloudformation__cfn-lint-4002"
  "aws-cloudformation__cfn-lint-4023"
  "aws-cloudformation__cfn-lint-4032"
  "beancount__beancount-931"
  "beetbox__beets-5495"
  "beeware__briefcase-2075"
  "beeware__briefcase-2085"
  "bridgecrewio__checkov-6893"
  "bridgecrewio__checkov-6895"
  "bridgecrewio__checkov-7002"
)

# gt-v1 (10 tasks, --eval-num-workers 2)
TASKS_V1=(
  "arviz-devs__arviz-2413"
  "aws-cloudformation__cfn-lint-3779"
  "aws-cloudformation__cfn-lint-3805"
  "aws-cloudformation__cfn-lint-4016"
  "delgan__loguru-1306"
  "kozea__weasyprint-2303"
  "pydata__xarray-9760"
  "pydata__xarray-9971"
  "pylint-dev__pylint-10044"
  "pypa__twine-1225"
)

# --------------------------------------------------------------------------
# Output directories
# --------------------------------------------------------------------------
OUT_T0="$HOME/results/${RUN_ID}/gt_t0"
OUT_V1="$HOME/results/${RUN_ID}/gt_v1"
mkdir -p "$OUT_T0" "$OUT_V1"

echo "============================================================"
echo " 30-Task Comparison Run: GT+Agent (L1+L3+L3b)"
echo "============================================================"
echo "Run ID:    $RUN_ID"
echo "GT_PHASE:  $GT_PHASE"
echo "Max iter:  $GT_MAX_ITER"
echo "Model:     qwen3-coder-480b (Vertex MaaS global)"
echo "Output T0: $OUT_T0  (20 tasks, 4 workers)"
echo "Output V1: $OUT_V1  (10 tasks, 2 workers)"
echo "Started:   $(date -u) UTC"
echo "============================================================"

# --------------------------------------------------------------------------
# Helper: join array into pipe-separated filter string
# --------------------------------------------------------------------------
join_pipe() {
  local IFS="|"
  echo "$*"
}

FILTER_T0=$(join_pipe "${TASKS_T0[@]}")
FILTER_V1=$(join_pipe "${TASKS_V1[@]}")

# --------------------------------------------------------------------------
# Detect which VM we're on and run the appropriate shard
# --------------------------------------------------------------------------
HOSTNAME=$(hostname)

run_shard() {
  local shard_name="$1"
  local output_dir="$2"
  local num_workers="$3"
  local filter="$4"

  echo ""
  echo "--- Launching shard: $shard_name ($num_workers workers) ---"
  echo "Filter: $filter"
  echo ""

  cd "$HOME/oh-benchmarks" 2>/dev/null || cd "$HOME/OpenHands" 2>/dev/null || {
    echo "ERROR: Cannot find OpenHands directory"
    exit 1
  }

  python3 "$SCRIPT_DIR/oh_gt_full_wrapper.py" "$LLM_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Live_Lite \
    --split test \
    --workspace docker \
    --max-iterations "$GT_MAX_ITER" \
    --num-workers "$num_workers" \
    --output-dir "$output_dir" \
    --instances.filter="$filter" \
    2>&1 | tee "$output_dir/run.log"

  echo ""
  echo "--- Shard $shard_name complete ---"
  echo "Finished: $(date -u) UTC"
  if [ -f "$output_dir/output.jsonl" ]; then
    echo "Tasks completed: $(wc -l < "$output_dir/output.jsonl")"
  fi
}

# --------------------------------------------------------------------------
# Run based on hostname
# --------------------------------------------------------------------------
case "$HOSTNAME" in
  *t0*|*gt-t0*)
    run_shard "gt-t0" "$OUT_T0" 4 "$FILTER_T0"
    ;;
  *v1*|*gt-v1*)
    run_shard "gt-v1" "$OUT_V1" 2 "$FILTER_V1"
    ;;
  *)
    echo "WARNING: Unknown hostname '$HOSTNAME'. Defaulting to gt-t0 shard."
    echo "To run gt-v1 shard, set hostname or manually choose."
    echo ""
    echo "Select shard:"
    echo "  1) gt-t0 (20 tasks, 4 workers)"
    echo "  2) gt-v1 (10 tasks, 2 workers)"
    echo "  3) BOTH sequentially"
    read -r -p "Choice [1/2/3]: " choice
    case "$choice" in
      1) run_shard "gt-t0" "$OUT_T0" 4 "$FILTER_T0" ;;
      2) run_shard "gt-v1" "$OUT_V1" 2 "$FILTER_V1" ;;
      3)
        run_shard "gt-t0" "$OUT_T0" 4 "$FILTER_T0"
        run_shard "gt-v1" "$OUT_V1" 2 "$FILTER_V1"
        ;;
      *) echo "Invalid choice. Exiting."; exit 1 ;;
    esac
    ;;
esac

# --------------------------------------------------------------------------
# Post-run: verify_report
# --------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Run complete. Running verify_report..."
echo "============================================================"

if [ -f "$SCRIPT_DIR/../swebench/verify_report.py" ]; then
  python3 "$SCRIPT_DIR/../swebench/verify_report.py" append \
    --run-dir "$HOME/results/${RUN_ID}" 2>&1 || true
fi

echo ""
echo "Next: merge output.jsonl from both VMs and run:"
echo "  python3 scripts/analysis/compare_30task.py \\"
echo "    --gt-t0 $OUT_T0/output.jsonl \\"
echo "    --gt-v1 $OUT_V1/output.jsonl \\"
echo "    --baseline-t0 D:\\tmp\\gt_test\\results_final/baseline_t0.jsonl \\"
echo "    --baseline-v1 D:\\tmp\\gt_test\\results_final/baseline_v1.jsonl"
echo ""
echo "DONE: $(date -u) UTC"
