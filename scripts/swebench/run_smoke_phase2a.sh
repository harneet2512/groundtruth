#!/bin/bash
set -e
source ~/gt-venv/bin/activate
source ~/gt-env.sh
cd ~/groundtruth
git pull

# Phase 2a smoke test — 5 tasks, sequential (1 worker)
# Tests: check cap, softened pipeline, pyright diagnostics, hard/soft split, bounded test feedback

FILTER="django__django-15902|django__django-16139|django__django-12856|scikit-learn__scikit-learn-14894|django__django-14608"

OUTPUT_DIR=~/smoke_phase2a_$(date +%Y%m%d_%H%M%S)
mkdir -p $OUTPUT_DIR

echo "Starting Phase 2a smoke test at $(date)"
echo "Output: $OUTPUT_DIR"
echo "Filter: $FILTER"

python3 benchmarks/swebench/run_mini_gt.py \
  -c benchmarks/swebench/mini_swebench_gt.yaml \
  --model openai/gpt-5.4-nano \
  --subset lite --split test \
  -w 1 \
  --filter "$FILTER" \
  -o "$OUTPUT_DIR" \
  2>&1 | tee "$OUTPUT_DIR/smoke.log"

echo ""
echo "=== PHASE 2a SMOKE TEST COMPLETE ==="
echo "Output directory: $OUTPUT_DIR"
echo ""

# Smoke check: validate phase2a-specific behaviors
python3 -c "
import json, os, re, sys

output_dir = '$OUTPUT_DIR'
tasks = []
for task_dir in sorted(os.listdir(output_dir)):
    task_path = os.path.join(output_dir, task_dir)
    if not os.path.isdir(task_path):
        continue
    traj = os.path.join(task_path, f'{task_dir}.traj.json')
    if not os.path.exists(traj):
        continue
    try:
        with open(traj) as f:
            data = json.load(f)
        tasks.append((task_dir, data))
    except Exception:
        pass

if not tasks:
    print('ERROR: No tasks found')
    sys.exit(1)

print(f'Tasks found: {len(tasks)}')
all_pass = True

# Check A: check command called at most 2 times per task (1 real + 1 blocked by cap)
check_overcall = 0
for task_id, data in tasks:
    usage = data.get('info', {}).get('gt_tool_usage', {})
    cmds = usage.get('command_counts', {})
    check_calls = cmds.get('check', 0)
    if check_calls > 2:
        check_overcall += 1
        print(f'  {task_id}: check called {check_calls} times')
check_a = check_overcall == 0
print(f'Check A (check cap — max 2 recorded per task): {\"PASS\" if check_a else \"FAIL\"} — {check_overcall} tasks exceeded')
if not check_a: all_pass = False

# Check B: gt_version is phase2a in all trajectories
wrong_version = 0
for task_id, data in tasks:
    info = data.get('info', {})
    ver = info.get('gt_version', '')
    if ver != 'phase2a':
        wrong_version += 1
        print(f'  {task_id}: gt_version={ver}')
check_b = wrong_version == 0
print(f'Check B (gt_version=phase2a): {\"PASS\" if check_b else \"FAIL\"}')
if not check_b: all_pass = False

# Check C: All tasks produce non-empty patches
empty_patches = 0
for task_id, data in tasks:
    submission = data.get('info', {}).get('submission', '')
    if not submission or not submission.strip():
        empty_patches += 1
        print(f'  Empty patch: {task_id}')
check_c = empty_patches == 0
print(f'Check C (non-empty patches): {\"PASS\" if check_c else \"FAIL\"} — {empty_patches} empty')
if not check_c: all_pass = False

# Check D: No task uses more than 5 total GT tool calls (pipeline efficiency)
heavy_tasks = 0
for task_id, data in tasks:
    usage = data.get('info', {}).get('gt_tool_usage', {})
    total = usage.get('total_calls', 0)
    if total > 6:
        heavy_tasks += 1
        print(f'  {task_id}: {total} GT calls')
check_d = heavy_tasks <= 1  # Allow at most 1 heavy task
print(f'Check D (pipeline efficiency — max 6 GT calls): {\"PASS\" if check_d else \"FAIL\"} — {heavy_tasks} heavy tasks')
if not check_d: all_pass = False

print()
if all_pass:
    print('ALL CHECKS PASS — ready for 300-task run')
else:
    print('SOME CHECKS FAILED — review before proceeding')
"
