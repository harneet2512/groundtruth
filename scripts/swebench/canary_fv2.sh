#!/bin/bash
set -euo pipefail

REPO_DIR="$HOME/groundtruth"
RESULTS_DIR="$HOME/foundation_v2"
SMOKE_TASKS="django__django-12856,django__django-13158,sympy__sympy-17655,django__django-10914"

echo "=== Foundation v2 Canary — 4 Tasks × 2 Conditions ==="
echo "Started: $(date -u) UTC"

# Ensure proxy is running
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running. Run start_proxy.sh first."
    exit 1
fi
echo "Proxy: OK"

# --- Canary A: Baseline ---
echo ""
echo "=== CANARY A: Baseline (no GT) ==="
echo "Started: $(date)"
mkdir -p "$RESULTS_DIR/canary_a"

bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_baseline.sh" \
    --instances "$SMOKE_TASKS" \
    --output-dir "$RESULTS_DIR/canary_a" \
    --max-iterations 100 2>&1 | tee "$RESULTS_DIR/canary_a/run.log"

echo "Canary A complete: $(date)"

# --- Canary B: GT Phase 3 ---
echo ""
echo "=== CANARY B: GT Phase 3 ==="
echo "Started: $(date)"
mkdir -p "$RESULTS_DIR/canary_b"

bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh" \
    --instances "$SMOKE_TASKS" \
    --output-dir "$RESULTS_DIR/canary_b" \
    --max-iterations 100 2>&1 | tee "$RESULTS_DIR/canary_b/run.log"

echo "Canary B complete: $(date)"

# --- Canary Audit ---
echo ""
echo "=== CANARY AUDIT ==="
echo ""

# Count trajectories
for cond in canary_a canary_b; do
    traj_count=$(find "$RESULTS_DIR/$cond" -name "*.traj.json" 2>/dev/null | wc -l)
    echo "$cond: $traj_count trajectories"
done

# Check GT analysis in canary_b trajectories
echo ""
echo "--- GT Analysis Presence (canary_b) ---"
for traj in "$RESULTS_DIR"/canary_b/*/*.traj.json "$RESULTS_DIR"/canary_b/trajs/*.json; do
    [ -f "$traj" ] || continue
    task_id=$(basename "$traj" .traj.json)
    gt_refs=$(grep -c "gt_analysis\|obligation site\|Pre-computed" "$traj" 2>/dev/null || echo 0)
    echo "  $task_id: gt_analysis_refs=$gt_refs"
done

echo ""
echo "--- Disk Usage ---"
df -h / | tail -1

echo ""
echo "=== CANARY COMPLETE at $(date -u) UTC ==="
