#!/bin/bash
# v10 Pro Smoke Test: 5 tasks with GT hook delivery.
#
# Verifies: hook fires, all 3 signals appear, output within line budget.
# Uses BASELINE prompt template (hook injects GT as command stdout).
#
# Usage: bash run_pro_smoke_v10.sh
# Run on VM with mini-swe-agent, LiteLLM proxy on :4000.
set -e

REPO_ROOT="${REPO_ROOT:-$HOME/groundtruth}"
cd "$REPO_ROOT"

# Source API keys
eval "$(grep '^export.*API_KEY' "$HOME/.bashrc" 2>/dev/null)" || true
export PYTHONPATH="${HOME}/mini-swe-agent/src:${PYTHONPATH:-}"

SMOKE_DIR="benchmarks/swebench/results/pro_v10_smoke_$(date +%Y%m%d_%H%M)"
mkdir -p "$SMOKE_DIR"

MODEL="${MODEL:-openai/qwen3-coder}"
WORKERS=2
SLICE="0:5"

echo "============================================================"
echo "  v10 Pro Smoke Test — 5 Tasks with GT Hook"
echo "============================================================"
echo "Output:    $SMOKE_DIR"
echo "Model:     $MODEL"
echo "Workers:   $WORKERS"
echo "Slice:     $SLICE"
echo "Config:    benchmarks/swebench/mini_swebench_pro_gt_v10_hooked.yaml"
echo "Started:   $(date)"
echo ""

# ─── Run GT condition (hook delivery) ───
echo "=== Running GT v10 (hook delivery) ==="
python3 benchmarks/swebench/run_mini_gt_hooked.py swebench \
  -c benchmarks/swebench/mini_swebench_pro_gt_v10_hooked.yaml \
  -m "$MODEL" \
  --subset ScaleAI/SWE-bench_Pro --split test \
  --slice "$SLICE" \
  -o "$SMOKE_DIR/gt_v10" \
  -w "$WORKERS" \
  2>&1 | tee "$SMOKE_DIR/gt_v10_run.log"

echo ""
echo "=== Smoke Complete: $(date) ==="
echo ""

# ─── Verify GT utilization ───
echo "=== GT Utilization Check ==="

GT_LOGS="$SMOKE_DIR/gt_v10/gt_logs"
if [ -d "$GT_LOGS" ]; then
  TOTAL_LOGS=$(ls -1 "$GT_LOGS"/*.jsonl 2>/dev/null | wc -l)
  echo "GT log files found: $TOTAL_LOGS"

  for logfile in "$GT_LOGS"/*.jsonl; do
    instance=$(basename "$logfile" .jsonl)
    entries=$(wc -l < "$logfile")
    has_tests=$(grep -c '"test_assertions"' "$logfile" 2>/dev/null || echo "0")
    has_ego=$(grep -c '"ego_graph"' "$logfile" 2>/dev/null || echo "0")
    has_sibling=$(grep -c '"found": true' "$logfile" 2>/dev/null || echo "0")
    echo "  $instance: $entries hook events, tests=$has_tests, ego=$has_ego, sibling=$has_sibling"
  done
else
  echo "WARNING: No GT logs found at $GT_LOGS"
  echo "Hook may not have fired. Check run log for injection errors."
fi

echo ""
echo "=== Trajectories ==="
for traj in "$SMOKE_DIR/gt_v10"/*/*.traj.json; do
  instance=$(basename "$(dirname "$traj")")
  gt_mentions=$(grep -c "GT CODEBASE INTELLIGENCE" "$traj" 2>/dev/null || echo "0")
  test_mentions=$(grep -c "TESTS FOR" "$traj" 2>/dev/null || echo "0")
  similar_mentions=$(grep -c "SIMILAR:" "$traj" 2>/dev/null || echo "0")
  echo "  $instance: GT=$gt_mentions, TESTS=$test_mentions, SIMILAR=$similar_mentions"
done

echo ""
echo "Smoke test complete. Review logs at: $SMOKE_DIR"
