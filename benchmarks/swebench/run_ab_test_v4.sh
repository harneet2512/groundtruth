#!/bin/bash
# A/B test: baseline (no GT) vs GT v4 (on-demand tools) on all 10 diagnostic tasks.
#
# Runs both conditions sequentially, then evaluates with swebench harness
# and generates a comparison table.
#
# Usage: bash run_ab_test_v4.sh
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

AB_DIR="benchmarks/swebench/results/ab_v4_$(date +%Y%m%d_%H%M)"
mkdir -p "$AB_DIR"

TASKS_FILE="benchmarks/swebench/diagnostic_tasks.txt"
# Build regex filter from task list: "task1|task2|task3"
FILTER_REGEX=$(tr -d '\r' < "$TASKS_FILE" | tr '\n' '|' | sed 's/|$//')

MODEL="openai/gpt-5.4-nano"
WORKERS=2

echo "============================================================"
echo "  A/B Test: Baseline vs GT v4 On-Demand Tools"
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

# ─── Phase B: GT v4 on-demand tools ───
echo "=== Phase B: GT v4 (on-demand tools) ==="
echo "Started: $(date)"

python3 benchmarks/swebench/run_mini_gt.py \
  -c benchmarks/swebench/mini_swebench_gt_v4.yaml \
  -m "$MODEL" \
  --subset lite --split test \
  --filter "$FILTER_REGEX" \
  -o "$AB_DIR/gt_v4" \
  -w "$WORKERS" \
  2>&1 | tee "$AB_DIR/gt_v4_run.log"

echo "GT v4 complete: $(date)"
echo ""

# ─── Phase C: Evaluate with swebench harness ───
echo "=== Phase C: Evaluation ==="

for CONDITION in baseline gt_v4; do
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
gt_preds, gt_resolved = load_results('gt_v4')

tasks = sorted(set(list(baseline_preds.keys()) + list(gt_preds.keys())))

print(f\"{'Task':<45} {'Baseline':>10} {'GT v4':>10}\")
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

# ─── GT Tool Usage Summary ───
echo ""
echo "=== GT Tool Usage Evidence ==="
python3 -c "
import json
from pathlib import Path

ab_dir = Path('$AB_DIR')
gt_dir = ab_dir / 'gt_v4'
trajs = list(gt_dir.rglob('*.traj.json'))

used_count = 0
total = 0
command_counts = {}
all_symbols = []

for traj_path in sorted(trajs):
    try:
        with open(traj_path) as f:
            d = json.load(f)
        info = d.get('info', {})
        usage = info.get('gt_tool_usage', {})
        tid = d.get('instance_id', traj_path.stem)
        total += 1
        any_call = usage.get('any_call', False)
        if any_call:
            used_count += 1
        calls = usage.get('total_calls', 0)
        cmds = ', '.join(usage.get('commands_used', [])) or '-'
        syms = ', '.join(usage.get('symbols_queried', [])[:3]) or '-'
        first = usage.get('first_call_turn', '-')
        status = f'{calls} calls, first@{first}' if any_call else 'not used'
        print(f'  {tid:<45} {status:<25} cmds=[{cmds}]  syms=[{syms}]')

        for cmd in usage.get('commands_used', []):
            command_counts[cmd] = command_counts.get(cmd, 0) + 1
        all_symbols.extend(usage.get('symbols_queried', []))
    except Exception as e:
        print(f'  {traj_path.name}: error: {e}')

print(f'')
print(f'  Tool adoption: {used_count}/{total} tasks ({used_count*100//max(total,1)}%)')
if command_counts:
    print(f'  Command distribution: {dict(sorted(command_counts.items(), key=lambda x: -x[1]))}')
if all_symbols:
    unique = sorted(set(all_symbols))
    print(f'  Unique symbols queried: {len(unique)} — {unique[:10]}')
if total > 0 and used_count < total * 0.3:
    print(f'  [WARN] Low tool adoption — consider strengthening system prompt instruction')
" 2>&1 | tee -a "$AB_DIR/comparison.txt"

echo ""
echo "============================================================"
echo "  A/B Test Complete"
echo "============================================================"
echo "Finished: $(date)"
echo "Results:  $AB_DIR/comparison.txt"
echo "Review:   cat $AB_DIR/comparison.txt"
