#!/bin/bash
# Smoke test: GT Hybrid v1 on 10 v1.0.4 regression tasks
#
# Runs 2 conditions (baseline is known: BL=5/10 from v1.0.4 analysis):
#   Condition 2: Tools-Only GT (tools available, no enforcement)
#   Condition 3: Hybrid v1 (all 4 checkpoints enforced)
#
# Compares against known baseline:
#   Always resolved: 12907, 13453, 14309 (must still resolve)
#   v3 regression:   13579 (MUST NOT regress)
#   GT-only flip:    13236 (stochastic, 33% flip rate)
#   Never resolved:  13033, 13398, 13977, 14182
#   Infra-dep:       14096 (resolved when Docker works)
#
# Usage:
#   bash scripts/swebench/run_hybrid_smoke.sh [output_dir]
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUTDIR="${1:-$REPO_ROOT/results/hybrid_smoke_$(date +%Y%m%d_%H%M)}"

# Build regex OR pattern for --filter
TASKS=$(cat "$SCRIPT_DIR/instances_v104_regression.txt" | tr '\n' '|' | sed 's/|$//')

echo "=== GT Hybrid v1 Smoke Test ==="
echo "Output: $OUTDIR"
echo "Tasks: $TASKS"
echo "Known baseline: 5/10 (12907, 13453, 13579, 14096, 14309)"
echo ""

mkdir -p "$OUTDIR"

# ── Condition 2: Tools-Only GT ──────────────────────────────────────────

echo "=== Condition 2: Tools-Only GT ==="
echo "Start: $(date)"

python3 "$REPO_ROOT/benchmarks/swebench/run_mini_gt_hybrid_v1.py" \
    -c "$REPO_ROOT/benchmarks/swebench/swebench_deepseek_v3_gt_tools.yaml" \
    --subset lite --split test \
    --filter "$TASKS" \
    -w 2 -o "$OUTDIR/tools_only" \
    --mode tools-only \
    2>&1 | tee "$OUTDIR/tools_only.log"

echo "Tools-Only done: $(date)"

# ── Condition 3: Hybrid v1 ──────────────────────────────────────────────

echo "=== Condition 3: Hybrid v1 ==="
echo "Start: $(date)"

python3 "$REPO_ROOT/benchmarks/swebench/run_mini_gt_hybrid_v1.py" \
    -c "$REPO_ROOT/benchmarks/swebench/swebench_deepseek_v3_hybrid_v1.yaml" \
    --subset lite --split test \
    --filter "$TASKS" \
    -w 2 -o "$OUTDIR/hybrid_v1" \
    --mode hybrid \
    2>&1 | tee "$OUTDIR/hybrid_v1.log"

echo "Hybrid v1 done: $(date)"

# ── Evaluate both ───────────────────────────────────────────────────────

echo "=== Evaluating ==="

for cond in tools_only hybrid_v1; do
    echo "--- Evaluating $cond ---"
    if [ -f "$OUTDIR/$cond/preds.json" ]; then
        python3 -m swebench.harness.run_evaluation \
            --predictions_path "$OUTDIR/$cond/preds.json" \
            --swe_bench_tasks princeton-nlp/SWE-bench_Lite \
            --log_dir "$OUTDIR/$cond/eval_logs" \
            --testbed /tmp/eval_testbed 2>&1 | tail -5
    else
        echo "No preds.json for $cond"
    fi
done

# ── Summary ─────────────────────────────────────────────────────────────

echo ""
echo "=== REGRESSION CHECK ==="
echo ""
echo "Known baseline (v1.0.4): 5/10 resolved"
echo "  Always resolved: 12907, 13453, 14309"
echo "  v3 regression:   13579 (must NOT regress)"
echo "  Infra-dep:       14096"
echo ""

for cond in tools_only hybrid_v1; do
    echo "--- $cond ---"
    if [ -f "$OUTDIR/$cond/eval_logs/report.json" ]; then
        python3 -c "
import json
with open('$OUTDIR/$cond/eval_logs/report.json') as f:
    data = json.load(f)
resolved = data.get('resolved', [])
print(f'  Resolved: {len(resolved)}/10')
for r in sorted(resolved): print(f'    + {r}')

# Regression check
must_resolve = ['astropy__astropy-12907', 'astropy__astropy-13453', 'astropy__astropy-14309']
must_not_regress = ['astropy__astropy-13579']
for t in must_resolve:
    if t not in resolved:
        print(f'  *** REGRESSION: {t} not resolved (was always-resolved) ***')
for t in must_not_regress:
    if t not in resolved:
        print(f'  *** WARNING: {t} not resolved (v3 regression indicator) ***')
"
    else
        echo "  No eval report"
    fi
done

# GT utilization from trajectory info
echo ""
echo "=== GT UTILIZATION ==="
for cond in tools_only hybrid_v1; do
    echo "--- $cond ---"
    python3 -c "
import json, glob
trajs = glob.glob('$OUTDIR/$cond/*/astropy*/*.traj.json')
orient_count = 0
gt_decisions = {}
for t in trajs:
    try:
        data = json.load(open(t))
        info = data.get('info', {})
        if info.get('orient_shown'): orient_count += 1
        d = info.get('gt_decision', 'n/a')
        gt_decisions[d] = gt_decisions.get(d, 0) + 1
    except: pass
print(f'  Tasks with orient: {orient_count}/{len(trajs)}')
print(f'  GT decisions: {gt_decisions}')
" 2>/dev/null || echo "  (no trajectory data)"
done

echo ""
echo "=== SMOKE TEST COMPLETE ==="
echo "Output: $OUTDIR"
