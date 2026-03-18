#!/bin/bash
# A/B test: baseline (no GT) vs GT (v3.1 file delivery) on all 10 diagnostic tasks.
#
# Runs both conditions sequentially, then compares with swebench harness.
#
# Usage: bash run_ab_test.sh
# Run on VM with mini-swe-agent and swebench installed.
set -e

REPO_ROOT="${REPO_ROOT:-$HOME/groundtruth}"
cd "$REPO_ROOT"
git pull 2>/dev/null || true

# Source API keys from bashrc (non-interactive shells skip bashrc guards)
# shellcheck disable=SC1090
eval "$(grep '^export.*API_KEY' "$HOME/.bashrc" 2>/dev/null)" || true

# Ensure mini-swe-agent is on PYTHONPATH
export PYTHONPATH="${HOME}/mini-swe-agent/src:${PYTHONPATH:-}"

AB_DIR="benchmarks/swebench/results/ab_v31_$(date +%Y%m%d_%H%M)"
mkdir -p "$AB_DIR"

TASKS_FILE="benchmarks/swebench/diagnostic_tasks.txt"
# Build regex filter from task list: "task1|task2|task3"
FILTER_REGEX=$(cat "$TASKS_FILE" | tr '\n' '|' | sed 's/|$//')

MODEL="openai/gpt-5.4-nano"
WORKERS=2

echo "============================================================"
echo "  A/B Test: Baseline vs GT v3.1 File Delivery"
echo "============================================================"
echo "Output:    $AB_DIR"
echo "Tasks:     $(wc -l < "$TASKS_FILE") from $TASKS_FILE"
echo "Filter:    $FILTER_REGEX"
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
  --subset lite --split test \
  --filter "$FILTER_REGEX" \
  -o "$AB_DIR/baseline" \
  -w "$WORKERS" \
  2>&1 | tee "$AB_DIR/baseline_run.log"

echo "Baseline complete: $(date)"
echo ""

# ─── Phase B: GT v3.1 file delivery ───
echo "=== Phase B: GT v3.1 (file delivery) ==="
echo "Started: $(date)"

python3 benchmarks/swebench/run_mini_gt.py \
  -c benchmarks/swebench/mini_swebench_gt.yaml \
  -m "$MODEL" \
  --subset lite --split test \
  --filter "$FILTER_REGEX" \
  -o "$AB_DIR/gt_v31" \
  -w "$WORKERS" \
  2>&1 | tee "$AB_DIR/gt_v31_run.log"

echo "GT v3.1 complete: $(date)"
echo ""

# ─── Phase C: Evaluate with swebench harness ───
echo "=== Phase C: Evaluation ==="

# Find predictions files
BASELINE_PREDS=$(find "$AB_DIR/baseline" -name "preds.json" -type f | head -1)
GT_PREDS=$(find "$AB_DIR/gt_v31" -name "preds.json" -type f | head -1)

if [ -z "$BASELINE_PREDS" ]; then
  echo "[ERROR] Baseline predictions not found"
  BASELINE_PREDS="NOT_FOUND"
fi
if [ -z "$GT_PREDS" ]; then
  echo "[ERROR] GT predictions not found"
  GT_PREDS="NOT_FOUND"
fi

echo "Baseline preds: $BASELINE_PREDS"
echo "GT preds:       $GT_PREDS"

# Run swebench evaluation on both
for CONDITION in baseline gt_v31; do
  PREDS_FILE=$(find "$AB_DIR/$CONDITION" -name "preds.json" -type f | head -1)
  if [ -z "$PREDS_FILE" ] || [ ! -f "$PREDS_FILE" ]; then
    echo "[SKIP] No predictions for $CONDITION"
    continue
  fi

  echo ""
  echo "--- Evaluating: $CONDITION ---"
  python3 -m swebench.harness.run_evaluation \
    --predictions_path "$PREDS_FILE" \
    --swe_bench_tasks princeton-nlp/SWE-bench_Lite \
    --log_dir "$AB_DIR/$CONDITION/eval_logs" \
    --testbed /tmp/swebench_testbed \
    --skip_existing \
    --timeout 300 \
    2>&1 | tee "$AB_DIR/${CONDITION}_eval.log" || true
done

# ─── Phase D: Compare results ───
echo ""
echo "=== Phase D: Comparison ==="

python3 -c "
import json, sys
from pathlib import Path

ab_dir = Path('$AB_DIR')

def load_results(condition):
    \"\"\"Load eval results for a condition.\"\"\"
    preds_file = list((ab_dir / condition).rglob('preds.json'))
    if not preds_file:
        return {}, set()
    preds = {}
    with open(preds_file[0]) as f:
        preds = json.load(f)
    if isinstance(preds, list):
        preds = {p['instance_id']: p for p in preds}
    elif isinstance(preds, dict) and 'instance_id' not in preds:
        pass  # already keyed
    else:
        preds = {preds['instance_id']: preds}

    # Check eval logs for resolved
    eval_dir = ab_dir / condition / 'eval_logs'
    resolved = set()
    if eval_dir.exists():
        for log_file in eval_dir.rglob('*.json'):
            try:
                data = json.loads(log_file.read_text())
                if data.get('resolved', False):
                    resolved.add(data.get('instance_id', log_file.stem))
            except Exception:
                pass
    return preds, resolved

baseline_preds, baseline_resolved = load_results('baseline')
gt_preds, gt_resolved = load_results('gt_v31')

tasks = sorted(set(list(baseline_preds.keys()) + list(gt_preds.keys())))

print(f\"{'Task':<45} {'Baseline':>10} {'GT v3.1':>10}\")
print(f\"{'-'*45} {'-'*10} {'-'*10}\")

baseline_pass = 0
gt_pass = 0
for tid in tasks:
    b_status = 'RESOLVED' if tid in baseline_resolved else ('patch' if baseline_preds.get(tid, {}).get('model_patch', '').strip() else 'no_patch')
    g_status = 'RESOLVED' if tid in gt_resolved else ('patch' if gt_preds.get(tid, {}).get('model_patch', '').strip() else 'no_patch')
    if tid in baseline_resolved: baseline_pass += 1
    if tid in gt_resolved: gt_pass += 1
    print(f'{tid:<45} {b_status:>10} {g_status:>10}')

print(f\"\")
print(f\"{'TOTAL RESOLVED':<45} {baseline_pass:>10} {gt_pass:>10}\")
delta = gt_pass - baseline_pass
sign = '+' if delta > 0 else ''
print(f\"{'DELTA':<45} {'':>10} {sign}{delta:>9}\")
" 2>&1 | tee "$AB_DIR/comparison.txt"

# ─── GT Read Evidence Summary ───
echo ""
echo "=== GT File Read Evidence ==="
python3 -c "
import json
from pathlib import Path

ab_dir = Path('$AB_DIR')
gt_dir = ab_dir / 'gt_v31'
trajs = list(gt_dir.rglob('*.traj.json'))

read_count = 0
total = 0
for traj_path in sorted(trajs):
    try:
        with open(traj_path) as f:
            d = json.load(f)
        info = d.get('info', {})
        evidence = info.get('gt_read_evidence', {})
        tid = d.get('instance_id', traj_path.stem)
        total += 1
        read = evidence.get('read_file', False)
        turn = evidence.get('read_turn', '?')
        written = info.get('gt_file_written', False)
        if read:
            read_count += 1
        status = 'READ' if read else 'NOT READ'
        print(f'  {tid:<45} written={written}  {status}  turn={turn}')
    except Exception as e:
        print(f'  {traj_path.name}: error: {e}')

print(f'')
print(f'  Agent read GT file: {read_count}/{total}')
if total > 0 and read_count < total * 0.5:
    print(f'  [WARN] Low read rate — consider strengthening instruction (Step 3 fallback)')
" 2>&1 | tee -a "$AB_DIR/comparison.txt"

echo ""
echo "============================================================"
echo "  A/B Test Complete"
echo "============================================================"
echo "Finished: $(date)"
echo "Results:  $AB_DIR/comparison.txt"
echo "Review:   cat $AB_DIR/comparison.txt"
