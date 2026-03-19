#!/bin/bash
set -e
source ~/gt-venv/bin/activate
source ~/gt-env.sh
cd ~/groundtruth

OUTPUT_DIR=~/phase1_300_$(date +%Y%m%d_%H%M%S)
mkdir -p $OUTPUT_DIR

echo "Starting Phase 1 full 300-task run at $(date)"
echo "Output: $OUTPUT_DIR"

python3 benchmarks/swebench/run_mini_gt.py \
  -c benchmarks/swebench/mini_swebench_gt.yaml \
  --model openai/gpt-5.4-nano \
  --subset lite --split test \
  -w 4 \
  -o "$OUTPUT_DIR" \
  2>&1 | tee "$OUTPUT_DIR/run_300.log"

echo ""
echo "=== 300-TASK RUN COMPLETE at $(date) ==="
echo "Output directory: $OUTPUT_DIR"

# Generate MANIFEST.txt
python3 -c "
import os, json

output_dir = '$OUTPUT_DIR'
total = 0
with_patch = 0
empty = 0
errors = 0

for task_dir in sorted(os.listdir(output_dir)):
    task_path = os.path.join(output_dir, task_dir)
    if not os.path.isdir(task_path):
        continue
    traj = os.path.join(task_path, f'{task_dir}.traj.json')
    if not os.path.exists(traj):
        continue
    total += 1
    try:
        with open(traj) as f:
            data = json.load(f)
        submission = data.get('info', {}).get('submission', '')
        if submission and submission.strip():
            with_patch += 1
        else:
            empty += 1
    except Exception:
        errors += 1

manifest = f'''Phase 1 Obligation Workflow — 300-Task Run
Total tasks: {total}
With patches: {with_patch}
Empty patches: {empty}
Errors: {errors}
'''
print(manifest)
with open(os.path.join(output_dir, 'MANIFEST.txt'), 'w') as f:
    f.write(manifest)
print(f'MANIFEST.txt written to {output_dir}')
"

# Correction audit
echo ""
echo "=== CORRECTION AUDIT ==="
python3 -c "
import json, os

output_dir = '$OUTPUT_DIR'
total_tasks = 0
tasks_with_corrections = 0
total_corrections = 0
total_gated_out = 0
by_type = {}
correction_details = []

for task_dir in sorted(os.listdir(output_dir)):
    task_path = os.path.join(output_dir, task_dir)
    if not os.path.isdir(task_path):
        continue
    traj = os.path.join(task_path, f'{task_dir}.traj.json')
    if not os.path.exists(traj):
        continue
    total_tasks += 1
    try:
        with open(traj) as f:
            data = json.load(f)
        ac = data.get('info', {}).get('autocorrect_report', {})
        corr = ac.get('total_corrections', 0)
        gated = ac.get('gated_out', 0)
        total_gated_out += gated
        if corr > 0:
            tasks_with_corrections += 1
            total_corrections += corr
            for c in ac.get('corrections', []):
                ct = c.get('check_type', '?')
                by_type[ct] = by_type.get(ct, 0) + 1
                correction_details.append({
                    'task': task_dir,
                    'type': ct,
                    'old': c.get('old_name', '?'),
                    'new': c.get('new_name', '?'),
                    'line': c.get('line', '?'),
                    'file': c.get('file', '?'),
                })
    except Exception:
        pass

print(f'Tasks processed: {total_tasks}')
print(f'Tasks with corrections: {tasks_with_corrections}')
print(f'Total corrections applied: {total_corrections}')
print(f'Total gated out: {total_gated_out}')
print(f'By type: {by_type}')
print()

bad = [d for d in correction_details if d['type'] in ('class_ref', 'func_call')]
if bad:
    print('!!! WARNING: class_ref or func_call corrections found !!!')
    for d in bad:
        print(f'  {d[\"task\"]}: {d[\"type\"]} {d[\"old\"]} -> {d[\"new\"]}')
else:
    print('CLEAN: No class_ref or func_call corrections')

if correction_details:
    print()
    print('All corrections:')
    for d in correction_details:
        print(f'  {d[\"task\"]}: {d[\"type\"]} {d[\"old\"]} -> {d[\"new\"]} (line {d[\"line\"]})')
"

# Spin audit
echo ""
echo "=== SPIN AUDIT ==="
python3 -c "
import json, os

output_dir = '$OUTPUT_DIR'
total_tasks = 0
tasks_with_spins = 0
total_spin_redirects = 0

for task_dir in sorted(os.listdir(output_dir)):
    task_path = os.path.join(output_dir, task_dir)
    if not os.path.isdir(task_path):
        continue
    traj = os.path.join(task_path, f'{task_dir}.traj.json')
    if not os.path.exists(traj):
        continue
    total_tasks += 1
    try:
        with open(traj) as f:
            data = json.load(f)
        usage = data.get('info', {}).get('gt_tool_usage', {})
        spins = usage.get('spin_redirects', 0)
        if spins > 0:
            tasks_with_spins += 1
            total_spin_redirects += spins
            print(f'  {task_dir}: {spins} spin redirect(s)')
    except Exception:
        pass

print()
print(f'Tasks: {total_tasks}')
print(f'Tasks with spin redirects: {tasks_with_spins}')
print(f'Total spin redirects: {total_spin_redirects}')
"

# Tool breakdown
echo ""
echo "=== TOOL BREAKDOWN ==="
python3 -c "
import json, os

output_dir = '$OUTPUT_DIR'
cmd_totals = {}
total_tasks = 0
obligations_tasks = 0
search_tasks = 0

for task_dir in sorted(os.listdir(output_dir)):
    task_path = os.path.join(output_dir, task_dir)
    if not os.path.isdir(task_path):
        continue
    traj = os.path.join(task_path, f'{task_dir}.traj.json')
    if not os.path.exists(traj):
        continue
    total_tasks += 1
    try:
        with open(traj) as f:
            data = json.load(f)
        usage = data.get('info', {}).get('gt_tool_usage', {})
        for cmd, count in usage.get('command_counts', {}).items():
            cmd_totals[cmd] = cmd_totals.get(cmd, 0) + count
            if cmd == 'obligations' and count > 0:
                obligations_tasks += 1
            if cmd == 'search' and count > 0:
                search_tasks += 1
    except Exception:
        pass

print(f'Total tasks: {total_tasks}')
print(f'Tasks using obligations: {obligations_tasks} ({100*obligations_tasks/max(total_tasks,1):.1f}%)')
print(f'Tasks using search: {search_tasks} ({100*search_tasks/max(total_tasks,1):.1f}%)')
print()
print('Command totals:')
for cmd, count in sorted(cmd_totals.items(), key=lambda x: -x[1]):
    print(f'  {cmd}: {count}')
"

# JSONL streaming summary
python3 -c "
import json, os

output_dir = '$OUTPUT_DIR'
jsonl_path = os.path.join(output_dir, 'summary.jsonl')
with open(jsonl_path, 'w') as out:
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
            info = data.get('info', {})
            usage = info.get('gt_tool_usage', {})
            ac = info.get('autocorrect_report', {})
            row = {
                'instance_id': task_dir,
                'exit_status': info.get('exit_status'),
                'has_patch': bool(info.get('submission', '').strip()),
                'gt_calls': usage.get('total_calls', 0),
                'commands': usage.get('command_counts', {}),
                'spin_redirects': usage.get('spin_redirects', 0),
                'corrections': ac.get('total_corrections', 0),
            }
            out.write(json.dumps(row) + '\n')
        except Exception:
            pass
print(f'Summary JSONL written to {jsonl_path}')
"
