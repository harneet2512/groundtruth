#!/usr/bin/env bash
# Phase 3 Gated A/B: Qwen3-Coder baseline vs Qwen3-Coder + GroundTruth
#
# Gated timeline:
#   Phase 1: Build images (pre-requisite)
#   Phase 2: Smoke test (4 tasks × 2 conditions)
#   Phase 3: 50-task A/B → Decision Gate 1
#   Phase 4: Next 50-task A/B → Decision Gate 2
#   Phase 5: Remaining 200 → Full eval
#
# Usage:
#   bash scripts/swebench/run_ab_vertex.sh                    # Full gated run
#   bash scripts/swebench/run_ab_vertex.sh --skip-smoke       # Skip smoke test
#   bash scripts/swebench/run_ab_vertex.sh --start-phase 3    # Resume from phase 3
#   bash scripts/swebench/run_ab_vertex.sh --workers 3        # Set worker count
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"

RESULTS_DIR="${RESULTS_DIR:-results/vertex_ab_$(date +%Y%m%d_%H%M)}"
NUM_WORKERS="${NUM_WORKERS:-3}"
MAX_ITER="100"
START_PHASE=1
SKIP_SMOKE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-smoke) SKIP_SMOKE=true; shift ;;
        --start-phase) START_PHASE="$2"; shift 2 ;;
        --workers) NUM_WORKERS="$2"; shift 2 ;;
        --max-iterations) MAX_ITER="$2"; shift 2 ;;
        --results-dir) RESULTS_DIR="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

mkdir -p "$RESULTS_DIR"/{baseline,gt}/{smoke,fifty_1,fifty_2,full}

echo "============================================"
echo "  Phase 3 Gated A/B: Vertex AI Qwen3-Coder"
echo "  Results: $RESULTS_DIR"
echo "  Workers: $NUM_WORKERS"
echo "  Max iterations: $MAX_ITER"
echo "============================================"
echo ""

# Smoke test tasks (known to produce patches)
SMOKE_TASKS="django__django-12856,django__django-14608,sympy__sympy-17655,django__django-10914"

# ── Helper: count resolved tasks from eval results ──────────────────────
count_resolved() {
    local report="$1"
    if [ -f "$report" ]; then
        python3 -c "
import json
with open('$report') as f:
    d = json.load(f)
r = d.get('resolved', d.get('results', {}).get('resolved', 0))
print(r)
" 2>/dev/null || echo "0"
    else
        echo "0"
    fi
}

# ── Helper: count patches produced ──────────────────────────────────────
count_patches() {
    local output_dir="$1"
    local count=0
    for traj in "$output_dir"/*/*.traj.json "$output_dir"/trajs/*.json; do
        [ -f "$traj" ] || continue
        has_patch=$(python3 -c "
import json
with open('$traj') as f:
    d = json.load(f)
patch = d.get('info', {}).get('submission', d.get('test_result', {}).get('git_patch', ''))
print('1' if patch and patch.strip() else '0')
" 2>/dev/null || echo "0")
        count=$((count + has_patch))
    done
    echo "$count"
}

# ── Helper: decision gate ───────────────────────────────────────────────
decision_gate() {
    local gate_name="$1"
    local baseline_dir="$2"
    local gt_dir="$3"

    echo ""
    echo "════════════════════════════════════════════"
    echo "  DECISION GATE: $gate_name"
    echo "════════════════════════════════════════════"

    local b_patches=$(count_patches "$baseline_dir")
    local g_patches=$(count_patches "$gt_dir")

    echo "  Baseline patches: $b_patches"
    echo "  GT patches:       $g_patches"

    # Run eval if possible
    if command -v swebench-eval &>/dev/null || [ -f "$REPO_DIR/scripts/swebench/run_eval.sh" ]; then
        echo "  Running eval..."
        bash "$REPO_DIR/scripts/swebench/run_eval.sh" "$baseline_dir" 2>/dev/null || true
        bash "$REPO_DIR/scripts/swebench/run_eval.sh" "$gt_dir" 2>/dev/null || true

        local b_resolved=$(count_resolved "$baseline_dir/eval_results.json")
        local g_resolved=$(count_resolved "$gt_dir/eval_results.json")
        local delta=$((g_resolved - b_resolved))

        echo ""
        echo "  Baseline resolved: $b_resolved"
        echo "  GT resolved:       $g_resolved"
        echo "  Delta (GT - BL):   $delta"
        echo ""

        if [ "$delta" -ge 2 ]; then
            echo "  ✓ DECISION: GO — GT leads by $delta"
            return 0
        elif [ "$delta" -ge 1 ]; then
            echo "  ~ DECISION: CAUTIOUS GO — GT leads by $delta"
            return 0
        elif [ "$delta" -eq 0 ]; then
            echo "  ✗ DECISION: STOP — No improvement"
            echo "  Debug GT analysis quality before continuing."
            return 1
        else
            echo "  ✗ DECISION: STOP — GT is HURTING (delta=$delta)"
            echo "  GT injection may be confusing the model."
            return 1
        fi
    else
        echo "  (eval not available — continue based on patch counts)"
        echo "  Press Ctrl+C within 30s to abort, or wait to continue."
        sleep 30
        return 0
    fi
}

# ── Phase 1: Generate 50-task selection ─────────────────────────────────

FIFTY_TASKS_1="$RESULTS_DIR/fifty_tasks_1.txt"
FIFTY_TASKS_2="$RESULTS_DIR/fifty_tasks_2.txt"

if [ ! -f "$FIFTY_TASKS_1" ]; then
    echo "=== Generating 50-task subsets ==="
    cd "$REPO_DIR"

    # Generate first 50 with seed 42
    python3 scripts/swebench/select_50.py \
        --output "$FIFTY_TASKS_1" --seed 42 --show-distribution

    # Generate second 50 with seed 43 (different tasks)
    python3 scripts/swebench/select_50.py \
        --output "$FIFTY_TASKS_2" --seed 43

    # Remove overlap: keep only tasks in set 2 that aren't in set 1
    if [ -f "$FIFTY_TASKS_2" ]; then
        comm -23 <(sort "$FIFTY_TASKS_2") <(sort "$FIFTY_TASKS_1") > "${FIFTY_TASKS_2}.tmp"
        head -50 "${FIFTY_TASKS_2}.tmp" > "$FIFTY_TASKS_2"
        rm "${FIFTY_TASKS_2}.tmp"
    fi

    echo ""
fi

# ── Phase 2: Smoke Test ─────────────────────────────────────────────────

if [ "$START_PHASE" -le 2 ] && [ "$SKIP_SMOKE" = false ]; then
    echo "=== PHASE 2: Smoke Test (4 tasks × 2 conditions) ==="
    echo ""

    echo "--- Smoke: Baseline ---"
    bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_baseline.sh" \
        --instances "$SMOKE_TASKS" \
        --output-dir "$RESULTS_DIR/baseline/smoke" \
        --max-iterations "$MAX_ITER" || true

    echo ""
    echo "--- Smoke: GT ---"
    bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh" \
        --instances "$SMOKE_TASKS" \
        --output-dir "$RESULTS_DIR/gt/smoke" \
        --max-iterations "$MAX_ITER" || true

    # Smoke audit
    echo ""
    echo "=== SMOKE AUDIT ==="
    echo ""

    echo "--- GT Analysis in Trajectories ---"
    for traj in "$RESULTS_DIR"/gt/smoke/*/*.traj.json "$RESULTS_DIR"/gt/smoke/trajs/*.json; do
        [ -f "$traj" ] || continue
        task_id=$(basename "$traj" .traj.json)
        gt_analysis=$(grep -c "gt_analysis\|Pre-computed.*Analysis\|obligation site\|groundtruth_impact" "$traj" 2>/dev/null || echo 0)
        gt_calls=$(grep -c "gt_tool.py" "$traj" 2>/dev/null || echo 0)
        echo "  $task_id: analysis_refs=$gt_analysis, gt_calls=$gt_calls"
    done

    echo ""
    echo "--- Patch Presence ---"
    for condition in baseline gt; do
        b_patches=$(count_patches "$RESULTS_DIR/$condition/smoke")
        echo "  $condition: $b_patches/4 patches"
    done

    echo ""
    echo "════════════════════════════════════════════"
    echo "  SMOKE COMPLETE — review above"
    echo "  Ctrl+C to abort, or wait 15s to continue"
    echo "════════════════════════════════════════════"
    sleep 15
fi

# ── Phase 3: First 50-Task A/B ──────────────────────────────────────────

if [ "$START_PHASE" -le 3 ]; then
    echo ""
    echo "=== PHASE 3: First 50-Task A/B ==="
    echo ""

    echo "--- 50-task: Baseline ---"
    bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_baseline.sh" \
        --instances "$FIFTY_TASKS_1" \
        --output-dir "$RESULTS_DIR/baseline/fifty_1" \
        --max-iterations "$MAX_ITER" || true

    echo ""
    echo "--- 50-task: GT ---"
    bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh" \
        --instances "$FIFTY_TASKS_1" \
        --output-dir "$RESULTS_DIR/gt/fifty_1" \
        --max-iterations "$MAX_ITER" || true

    # Decision Gate 1
    if ! decision_gate "Gate 1 (first 50)" \
        "$RESULTS_DIR/baseline/fifty_1" "$RESULTS_DIR/gt/fifty_1"; then
        echo ""
        echo "Stopping at Gate 1. Results in $RESULTS_DIR"
        exit 0
    fi
fi

# ── Phase 4: Second 50-Task A/B ─────────────────────────────────────────

if [ "$START_PHASE" -le 4 ]; then
    echo ""
    echo "=== PHASE 4: Second 50-Task A/B ==="
    echo ""

    echo "--- 50-task: Baseline ---"
    bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_baseline.sh" \
        --instances "$FIFTY_TASKS_2" \
        --output-dir "$RESULTS_DIR/baseline/fifty_2" \
        --max-iterations "$MAX_ITER" || true

    echo ""
    echo "--- 50-task: GT ---"
    bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh" \
        --instances "$FIFTY_TASKS_2" \
        --output-dir "$RESULTS_DIR/gt/fifty_2" \
        --max-iterations "$MAX_ITER" || true

    # Decision Gate 2
    if ! decision_gate "Gate 2 (second 50)" \
        "$RESULTS_DIR/baseline/fifty_2" "$RESULTS_DIR/gt/fifty_2"; then
        echo ""
        echo "Stopping at Gate 2. Results in $RESULTS_DIR"
        exit 0
    fi
fi

# ── Phase 5: Remaining 200 Tasks ────────────────────────────────────────

if [ "$START_PHASE" -le 5 ]; then
    echo ""
    echo "=== PHASE 5: Full 300-Task Run ==="
    echo ""

    # Run all 300 (swebench-infer has resume support, skips completed tasks)
    echo "--- Full: Baseline ---"
    bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_baseline.sh" \
        --output-dir "$RESULTS_DIR/baseline/full" \
        --max-iterations "$MAX_ITER" || true

    echo ""
    echo "--- Full: GT ---"
    bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh" \
        --output-dir "$RESULTS_DIR/gt/full" \
        --max-iterations "$MAX_ITER" || true
fi

# ── Phase 6: Final Evaluation ───────────────────────────────────────────

echo ""
echo "=== PHASE 6: Docker Evaluation ==="

echo "--- Eval: Baseline ---"
bash "$REPO_DIR/scripts/swebench/run_eval.sh" "$RESULTS_DIR/baseline/full" 2>/dev/null || true

echo ""
echo "--- Eval: GT ---"
bash "$REPO_DIR/scripts/swebench/run_eval.sh" "$RESULTS_DIR/gt/full" 2>/dev/null || true

# ── Results Summary ─────────────────────────────────────────────────────

echo ""
echo "============================================"
echo "  GATED A/B EXPERIMENT COMPLETE"
echo "  Results: $RESULTS_DIR"
echo "============================================"
echo ""

echo "=== Final Scores ==="
for condition in baseline gt; do
    report="$RESULTS_DIR/$condition/full/eval_results.json"
    resolved=$(count_resolved "$report")
    echo "  $condition: $resolved resolved"
done

# Per-phase breakdown
echo ""
echo "=== Phase Breakdown ==="
for phase in smoke fifty_1 fifty_2; do
    for condition in baseline gt; do
        patches=$(count_patches "$RESULTS_DIR/$condition/$phase")
        echo "  $condition/$phase: $patches patches"
    done
done

# Budget summary
echo ""
echo "=== Budget Summary ==="
python3 "$REPO_DIR/scripts/swebench/budget_tracker.py" "$RESULTS_DIR" 2>/dev/null || echo "Budget tracker unavailable"
