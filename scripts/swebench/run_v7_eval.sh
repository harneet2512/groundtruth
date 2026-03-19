#!/bin/bash
# run_v7_eval.sh — v7 SWE-bench eval: GT-only (reuse baseline predictions).
#
# v7 fix: _is_project_local_name() gate on autocorrect checks 5A/5B.
# Only re-runs the GT condition; reuses baseline predictions if available.
#
# Usage:
#   tmux new -s eval_v7
#   bash scripts/swebench/run_v7_eval.sh [workers]
#
# Run on swebench-ab VM with mini-swe-agent and swebench installed.
set -euo pipefail

WORKERS="${1:-4}"
EVAL_DIR="${HOME}/eval_v7"
REPO_ROOT="${REPO_ROOT:-$HOME/groundtruth}"
MODEL="openai/gpt-5.4-nano"
TIMESTAMP=$(date +%Y%m%d_%H%M)

mkdir -p "$EVAL_DIR"

cd "$REPO_ROOT"
git pull 2>/dev/null || true

# Source API keys
eval "$(grep '^export.*API_KEY' "$HOME/.bashrc" 2>/dev/null)" || true

# Ensure mini-swe-agent is on PYTHONPATH
export PYTHONPATH="${HOME}/mini-swe-agent/src:${PYTHONPATH:-}"

echo "============================================================"
echo "  v7 Eval: GT autocorrect with positive-evidence gate"
echo "============================================================"
echo "Output:    $EVAL_DIR"
echo "Model:     $MODEL"
echo "Workers:   $WORKERS"
echo "Started:   $(date)"
echo ""

# ---------------------------------------------------------------------------
# Phase 0: Check for reusable baseline predictions
# ---------------------------------------------------------------------------
BASELINE_DIR="$EVAL_DIR/baseline"
BASELINE_PREDS=""

# Look for existing baseline predictions (from v6 or earlier runs)
for candidate in \
    "$HOME/eval_v6/baseline/preds.json" \
    "$HOME/eval_v42/baseline/preds.json" \
    "$REPO_ROOT/benchmarks/swebench/results/baseline/preds.json" \
    "$HOME/eval_v6/baseline/predictions.jsonl" \
    "$HOME/eval_v42/baseline/predictions.jsonl" \
    ; do
    if [ -f "$candidate" ]; then
        BASELINE_PREDS="$candidate"
        break
    fi
done

# Also check for any ab_* result dirs
if [ -z "$BASELINE_PREDS" ]; then
    for d in "$REPO_ROOT"/benchmarks/swebench/results/ab_*/baseline; do
        for f in "$d"/preds.json "$d"/predictions.jsonl; do
            if [ -f "$f" ]; then
                BASELINE_PREDS="$f"
                break 2
            fi
        done
    done
fi

if [ -n "$BASELINE_PREDS" ]; then
    echo "=== Reusing baseline predictions ==="
    echo "Source: $BASELINE_PREDS"
    mkdir -p "$BASELINE_DIR"
    cp "$BASELINE_PREDS" "$BASELINE_DIR/preds.json"
    BASELINE_COUNT=$(wc -l < "$BASELINE_PREDS" 2>/dev/null || python3 -c "import json; d=json.load(open('$BASELINE_PREDS')); print(len(d) if isinstance(d,list) else 1)")
    echo "Tasks: $BASELINE_COUNT"
    echo ""
else
    echo "=== No existing baseline found — running baseline from scratch ==="
    echo "Started: $(date)"
    mkdir -p "$BASELINE_DIR"

    python3 -m minisweagent.run.benchmarks.swebench \
      -c benchmarks/swebench/mini_swebench_baseline.yaml \
      -m "$MODEL" \
      --subset lite --split test \
      -o "$BASELINE_DIR" \
      -w "$WORKERS" \
      2>&1 | tee "$EVAL_DIR/baseline_run.log"

    echo "Baseline complete: $(date)"
    BASELINE_PREDS="$BASELINE_DIR/preds.json"
    echo ""
fi

# ---------------------------------------------------------------------------
# Phase 1: Run GT v7 condition (with fixed autocorrect)
# ---------------------------------------------------------------------------
GT_DIR="$EVAL_DIR/gt_v7"
mkdir -p "$GT_DIR"

echo "=== Phase 1: GT v7 (positive-evidence autocorrect) ==="
echo "Started: $(date)"

python3 benchmarks/swebench/run_mini_gt.py \
  -c benchmarks/swebench/mini_swebench_gt_v6.yaml \
  -m "$MODEL" \
  --subset lite --split test \
  -o "$GT_DIR" \
  -w "$WORKERS" \
  2>&1 | tee "$EVAL_DIR/gt_v7_run.log"

echo "GT v7 complete: $(date)"
echo ""

# ---------------------------------------------------------------------------
# Phase 2: Evaluate both conditions with swebench harness
# ---------------------------------------------------------------------------
echo "=== Phase 2: SWE-bench Harness Evaluation ==="

export DOCKER_CLIENT_TIMEOUT=900
export COMPOSE_HTTP_TIMEOUT=900

# Clean stale containers
stale=$(docker ps -aq --filter "name=sweb.eval" 2>/dev/null || true)
if [ -n "$stale" ]; then
    docker rm -f $stale 2>/dev/null || true
fi

for CONDITION in baseline gt_v7; do
    COND_DIR="$EVAL_DIR/$CONDITION"
    PREDS_FILE=$(find "$COND_DIR" -name "preds.json" -type f 2>/dev/null | head -1)
    if [ -z "$PREDS_FILE" ] || [ ! -f "$PREDS_FILE" ]; then
        echo "[SKIP] No predictions for $CONDITION"
        continue
    fi

    echo ""
    echo "--- Evaluating: $CONDITION ---"
    echo "Predictions: $PREDS_FILE"

    python3 -m swebench.harness.run_evaluation \
      --predictions_path "$PREDS_FILE" \
      --run_id "${CONDITION}_v7_${TIMESTAMP}" \
      --max_workers 2 \
      --cache_level env \
      2>&1 | tee "$EVAL_DIR/${CONDITION}_eval.log" || true

    # Post-eval cleanup
    stale_post=$(docker ps -aq --filter "name=sweb.eval" 2>/dev/null || true)
    if [ -n "$stale_post" ]; then
        docker rm -f $stale_post 2>/dev/null || true
    fi
done

# ---------------------------------------------------------------------------
# Phase 3: Compare results
# ---------------------------------------------------------------------------
echo ""
echo "=== Phase 3: Comparison ==="

python3 -c "
import json, glob, os
from pathlib import Path

eval_dir = Path('$EVAL_DIR')

def count_resolved(condition):
    \"\"\"Count resolved tasks from swebench eval logs.\"\"\"
    resolved = set()
    # Check multiple possible log locations
    for pattern in [
        f'{eval_dir}/{condition}_eval.log',
    ]:
        for log_file in glob.glob(pattern):
            with open(log_file) as f:
                for line in f:
                    if 'RESOLVED' in line or 'resolved' in line.lower():
                        # Parse instance_id from log
                        pass

    # Check eval result files
    for result_dir in eval_dir.glob(f'{condition}*/eval_logs'):
        for log_file in result_dir.rglob('*.json'):
            try:
                data = json.loads(log_file.read_text())
                if data.get('resolved', False):
                    resolved.add(data.get('instance_id', log_file.stem))
            except Exception:
                pass

    # Also check report.json
    for report in eval_dir.rglob(f'*{condition}*report*.json'):
        try:
            data = json.loads(report.read_text())
            if isinstance(data, dict) and 'resolved' in data:
                if isinstance(data['resolved'], list):
                    resolved.update(data['resolved'])
        except Exception:
            pass

    return resolved

def load_preds(condition):
    preds_file = list(eval_dir.glob(f'{condition}/preds.json'))
    if not preds_file:
        return {}
    try:
        with open(preds_file[0]) as f:
            content = f.read().strip()
        if content.startswith('['):
            items = json.loads(content)
            return {p['instance_id']: p for p in items}
        elif content.startswith('{'):
            p = json.loads(content)
            return {p['instance_id']: p}
        else:
            # JSONL
            preds = {}
            for line in content.splitlines():
                if line.strip():
                    p = json.loads(line)
                    preds[p['instance_id']] = p
            return preds
    except Exception as e:
        print(f'Error loading {condition} preds: {e}')
        return {}

baseline_preds = load_preds('baseline')
gt_preds = load_preds('gt_v7')
baseline_resolved = count_resolved('baseline')
gt_resolved = count_resolved('gt_v7')

print(f'')
print(f'Baseline: {len(baseline_preds)} predictions, {len(baseline_resolved)} resolved')
print(f'GT v7:    {len(gt_preds)} predictions, {len(gt_resolved)} resolved')
print(f'')
delta = len(gt_resolved) - len(baseline_resolved)
sign = '+' if delta > 0 else ''
print(f'Delta: {sign}{delta}')

# Show per-task comparison for differences
only_baseline = baseline_resolved - gt_resolved
only_gt = gt_resolved - baseline_resolved
if only_baseline:
    print(f'')
    print(f'Resolved by baseline only ({len(only_baseline)}):')
    for t in sorted(only_baseline):
        print(f'  - {t}')
if only_gt:
    print(f'')
    print(f'Resolved by GT v7 only ({len(only_gt)}):')
    for t in sorted(only_gt):
        print(f'  - {t}')

# Autocorrect analysis
print(f'')
print(f'=== Autocorrect Impact ===')
ac_corrections = 0
ac_tasks = 0
for tid, pred in gt_preds.items():
    ac = pred.get('autocorrect_report', {})
    if not ac:
        # Check trajectory
        traj_path = eval_dir / 'gt_v7' / tid / f'{tid}.traj.json'
        if traj_path.exists():
            try:
                traj = json.loads(traj_path.read_text())
                ac = traj.get('info', {}).get('autocorrect_report', {})
            except Exception:
                pass
    corrections = ac.get('corrections', [])
    if corrections:
        ac_tasks += 1
        ac_corrections += len(corrections)
        types = [c.get('check_type', '?') for c in corrections]
        print(f'  {tid}: {len(corrections)} corrections ({types})')

print(f'')
print(f'Total: {ac_corrections} corrections across {ac_tasks} tasks')
print(f'(v6 had 54 corrections, 53 false positives. Target: near-zero FP)')
" 2>&1 | tee "$EVAL_DIR/comparison.txt"

echo ""
echo "============================================================"
echo "  v7 Eval Complete"
echo "============================================================"
echo "Finished:   $(date)"
echo "Results:    $EVAL_DIR/comparison.txt"
echo "GT log:     $EVAL_DIR/gt_v7_run.log"
echo "Eval logs:  $EVAL_DIR/*_eval.log"
