#!/usr/bin/env bash
# Phase 3 A/B: Qwen3-Coder baseline vs Qwen3-Coder + GroundTruth
# Full orchestrator: smoke test → audit → full 300 → eval
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

RESULTS_DIR="${RESULTS_DIR:-results/vertex_ab_$(date +%Y%m%d_%H%M)}"
mkdir -p "$RESULTS_DIR"/{baseline,gt}/{smoke,full}

echo "============================================"
echo "  Phase 3 A/B: Vertex AI Qwen3-Coder"
echo "  Results: $RESULTS_DIR"
echo "============================================"
echo ""

# Smoke test tasks
SMOKE_TASKS="django__django-12856,django__django-14608,sympy__sympy-17655,django__django-10914"

# ── Phase 1: Smoke Test ──────────────────────────────────────────────

echo "=== SMOKE TEST: Baseline (4 tasks) ==="
echo ""
bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_baseline.sh" \
    --instances "$SMOKE_TASKS" \
    --output-dir "$RESULTS_DIR/baseline/smoke" \
    --max-iterations 300

echo ""
echo "=== SMOKE TEST: GT (4 tasks) ==="
echo ""
bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh" \
    --instances "$SMOKE_TASKS" \
    --output-dir "$RESULTS_DIR/gt/smoke" \
    --max-iterations 300

# ── Phase 2: Smoke Audit ─────────────────────────────────────────────

echo ""
echo "=== SMOKE AUDIT ==="
echo ""

# Check GT tool calls in trajectories
echo "--- GT Tool Call Analysis ---"
for task_dir in "$RESULTS_DIR"/gt/smoke/*/; do
    task_id=$(basename "$task_dir")
    traj="$task_dir/${task_id}.traj.json"
    if [ -f "$traj" ]; then
        gt_calls=$(grep -c "groundtruth_" "$traj" 2>/dev/null || echo 0)
        turns=$(python3 -c "
import json
with open('$traj') as f:
    d = json.load(f)
print(len(d.get('trajectory', d.get('history', []))))
" 2>/dev/null || echo "?")
        echo "  $task_id: GT calls=$gt_calls, turns=$turns"
    else
        echo "  $task_id: NO TRAJECTORY"
    fi
done

echo ""
echo "--- Baseline Turn Counts ---"
for task_dir in "$RESULTS_DIR"/baseline/smoke/*/; do
    task_id=$(basename "$task_dir")
    traj="$task_dir/${task_id}.traj.json"
    if [ -f "$traj" ]; then
        turns=$(python3 -c "
import json
with open('$traj') as f:
    d = json.load(f)
print(len(d.get('trajectory', d.get('history', []))))
" 2>/dev/null || echo "?")
        echo "  $task_id: turns=$turns"
    else
        echo "  $task_id: NO TRAJECTORY"
    fi
done

echo ""
echo "--- Patch Presence ---"
for condition in baseline gt; do
    echo "  [$condition]"
    for task_dir in "$RESULTS_DIR/$condition/smoke"/*/; do
        task_id=$(basename "$task_dir")
        traj="$task_dir/${task_id}.traj.json"
        if [ -f "$traj" ]; then
            has_patch=$(python3 -c "
import json
with open('$traj') as f:
    d = json.load(f)
patch = d.get('info', {}).get('submission', '')
print('YES' if patch and patch.strip() else 'NO')
" 2>/dev/null || echo "?")
            echo "    $task_id: patch=$has_patch"
        fi
    done
done

echo ""
echo "================================================================"
echo "  SMOKE TEST COMPLETE"
echo "  Review above. If smoke looks good, continue to full 300."
echo "  Press Ctrl+C to abort, or wait 10 seconds to continue."
echo "================================================================"
sleep 10

# ── Phase 3: Full 300-Task Run ────────────────────────────────────────

echo ""
echo "=== FULL 300: Baseline ==="
echo ""
bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_baseline.sh" \
    --output-dir "$RESULTS_DIR/baseline/full"

echo ""
echo "=== FULL 300: GT ==="
echo ""
bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh" \
    --output-dir "$RESULTS_DIR/gt/full"

# ── Phase 4: Docker Evaluation ────────────────────────────────────────

echo ""
echo "=== EVAL: Baseline ==="
bash "$REPO_DIR/scripts/swebench/run_eval.sh" "$RESULTS_DIR/baseline/full"

echo ""
echo "=== EVAL: GT ==="
bash "$REPO_DIR/scripts/swebench/run_eval.sh" "$RESULTS_DIR/gt/full"

# ── Phase 5: Results Summary ──────────────────────────────────────────

echo ""
echo "============================================"
echo "  A/B EXPERIMENT COMPLETE"
echo "  Results: $RESULTS_DIR"
echo "============================================"
echo ""
echo "Eval results:"
for condition in baseline gt; do
    report="$RESULTS_DIR/$condition/full/eval_results.json"
    if [ -f "$report" ]; then
        resolved=$(python3 -c "
import json
with open('$report') as f:
    d = json.load(f)
r = d.get('resolved', d.get('results', {}).get('resolved', 0))
t = d.get('total', d.get('results', {}).get('total', 300))
print(f'{r}/{t}')
" 2>/dev/null || echo "see $report")
        echo "  $condition: $resolved"
    else
        echo "  $condition: eval results not found at $report"
    fi
done

# Budget summary
echo ""
echo "=== Budget Summary ==="
python3 "$REPO_DIR/scripts/swebench/budget_tracker.py" "$RESULTS_DIR" 2>/dev/null || echo "Budget tracker unavailable"
