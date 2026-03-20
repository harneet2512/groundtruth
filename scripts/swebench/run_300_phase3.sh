#!/bin/bash
set -e
source ~/gt-venv/bin/activate
source ~/gt-env.sh
cd ~/groundtruth

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# ───────────────────────────────────────────────────
# Phase 3: A/B evaluation
# Condition A: Phase 2B baseline (no GT tools, test feedback only)
# Condition B: Phase 3 (3 GT tools + test feedback)
# ───────────────────────────────────────────────────

BASELINE_DIR=~/phase3_baseline_${TIMESTAMP}
GT_DIR=~/phase3_gt_${TIMESTAMP}
mkdir -p "$BASELINE_DIR" "$GT_DIR"

echo "=== Phase 3 Full 300-Task A/B Run ==="
echo "Started: $(date -u) UTC"
echo "Condition A (baseline): $BASELINE_DIR"
echo "Condition B (Phase 3 GT): $GT_DIR"

# Save manifests
for DIR in "$BASELINE_DIR" "$GT_DIR"; do
    CONDITION="baseline_phase2b"
    CONFIG="benchmarks/swebench/mini_swebench_phase2b.yaml"
    RUNNER="benchmarks/swebench/run_mini_gt.py"
    if [ "$DIR" = "$GT_DIR" ]; then
        CONDITION="phase3"
        CONFIG="benchmarks/swebench/mini_swebench_phase3.yaml"
        RUNNER="benchmarks/swebench/run_mini_phase3.py"
    fi
    cat > "$DIR/MANIFEST.txt" <<MANIFEST
Phase 3 A/B — $CONDITION
Date (UTC): $(date -u)
Git branch: $(git branch --show-current)
Git commit: $(git rev-parse HEAD)
Model: openai/gpt-5.4-nano
Scaffold: mini-swe-agent
Config: $CONFIG
Runner: $RUNNER
Condition: $CONDITION
MANIFEST
    cp "$CONFIG" "$DIR/config_used.yaml"
done

# Save GT-specific artifacts
cp benchmarks/swebench/gt_tool.py "$GT_DIR/gt_tool_snapshot.py"
cp benchmarks/swebench/gt_autocorrect.py "$GT_DIR/gt_autocorrect_snapshot.py"
cp benchmarks/swebench/gt_runtime_kb.py "$GT_DIR/gt_runtime_kb_snapshot.py"
cp benchmarks/swebench/run_mini_phase3.py "$GT_DIR/run_mini_phase3_snapshot.py"

echo ""
echo "=== Starting Condition A: BASELINE (Phase 2B, 4 workers) ==="
echo "Started: $(date)"

python3 benchmarks/swebench/run_mini_gt.py \
  -c benchmarks/swebench/mini_swebench_phase2b.yaml \
  --model openai/gpt-5.4-nano \
  --subset lite --split test \
  -w 4 \
  -o "$BASELINE_DIR" \
  2>&1 | tee "$BASELINE_DIR/run.log" &
BASELINE_PID=$!

echo ""
echo "=== Starting Condition B: PHASE 3 GT (4 workers) ==="
echo "Started: $(date)"

python3 benchmarks/swebench/run_mini_phase3.py \
  -c benchmarks/swebench/mini_swebench_phase3.yaml \
  --model openai/gpt-5.4-nano \
  --subset lite --split test \
  -w 4 \
  -o "$GT_DIR" \
  2>&1 | tee "$GT_DIR/run.log" &
GT_PID=$!

echo ""
echo "Both runs started. Baseline PID=$BASELINE_PID, GT PID=$GT_PID"
echo "Monitoring progress..."

# ───────────────────────────────────────────────────
# Progress monitoring loop
# ───────────────────────────────────────────────────

while true; do
    BASELINE_DONE=0
    GT_DONE=0

    if ! kill -0 $BASELINE_PID 2>/dev/null; then
        BASELINE_DONE=1
    fi
    if ! kill -0 $GT_PID 2>/dev/null; then
        GT_DONE=1
    fi

    BASELINE_TASKS=$(find "$BASELINE_DIR" -name "*.traj.json" 2>/dev/null | wc -l)
    GT_TASKS=$(find "$GT_DIR" -name "*.traj.json" 2>/dev/null | wc -l)

    # GT usage stats
    GT_TOOL_CALLS=$(python3 -c "
import json, os
total = 0
for d in os.listdir('$GT_DIR'):
    traj = os.path.join('$GT_DIR', d, f'{d}.traj.json')
    if os.path.isfile(traj):
        try:
            with open(traj) as f:
                data = json.load(f)
            usage = data.get('info', {}).get('gt_tool_usage', {})
            total += usage.get('total_calls', 0)
        except: pass
print(total)
" 2>/dev/null || echo "?")

    GT_CORRECTIONS=$(python3 -c "
import json, os
total = 0
for d in os.listdir('$GT_DIR'):
    traj = os.path.join('$GT_DIR', d, f'{d}.traj.json')
    if os.path.isfile(traj):
        try:
            with open(traj) as f:
                data = json.load(f)
            report = data.get('info', {}).get('autocorrect_report', {})
            total += report.get('total_corrections', 0)
        except: pass
print(total)
" 2>/dev/null || echo "?")

    echo "[$(date +%H:%M:%S)] Baseline: $BASELINE_TASKS tasks | GT: $GT_TASKS tasks, $GT_TOOL_CALLS gt_calls, $GT_CORRECTIONS corrections"

    # Mid-run audit at ~100 tasks
    if [ "$GT_TASKS" -ge 100 ] && [ ! -f "$GT_DIR/.audit_100_done" ]; then
        echo ""
        echo "=== MID-RUN AUDIT (GT at $GT_TASKS tasks) ==="

        # Check GT call density
        python3 -c "
import json, os
total_calls = 0
total_tasks = 0
excessive = []
for d in sorted(os.listdir('$GT_DIR')):
    traj = os.path.join('$GT_DIR', d, f'{d}.traj.json')
    if os.path.isfile(traj):
        total_tasks += 1
        try:
            with open(traj) as f:
                data = json.load(f)
            usage = data.get('info', {}).get('gt_tool_usage', {})
            calls = usage.get('total_calls', 0)
            total_calls += calls
            if calls > 6:
                excessive.append((d, calls))
        except: pass
avg = total_calls / total_tasks if total_tasks else 0
print(f'  Total GT calls: {total_calls} across {total_tasks} tasks (avg {avg:.1f}/task)')
if excessive:
    print(f'  WARNING: {len(excessive)} tasks with >6 calls:')
    for task, calls in excessive[:5]:
        print(f'    {task}: {calls} calls')
else:
    print('  All tasks within GT call budget')
"
        touch "$GT_DIR/.audit_100_done"
        echo ""
    fi

    if [ "$BASELINE_DONE" -eq 1 ] && [ "$GT_DONE" -eq 1 ]; then
        echo ""
        echo "=== BOTH RUNS COMPLETE at $(date) ==="
        break
    fi

    sleep 60
done

# ───────────────────────────────────────────────────
# Final audit
# ───────────────────────────────────────────────────

echo ""
echo "=== FINAL AUDIT ==="

BASELINE_FINAL=$(find "$BASELINE_DIR" -name "*.traj.json" | wc -l)
GT_FINAL=$(find "$GT_DIR" -name "*.traj.json" | wc -l)

echo "Condition A (Baseline): $BASELINE_FINAL tasks completed"
echo "Condition B (Phase 3 GT): $GT_FINAL tasks completed"

# GT tool usage summary
echo ""
echo "GT tool usage summary:"
python3 -c "
import json, os
total_calls = 0
cmd_counts = {}
tasks_with_calls = 0
compliant = 0
noncompliant = 0
for d in sorted(os.listdir('$GT_DIR')):
    traj = os.path.join('$GT_DIR', d, f'{d}.traj.json')
    if os.path.isfile(traj):
        try:
            with open(traj) as f:
                data = json.load(f)
            usage = data.get('info', {}).get('gt_tool_usage', {})
            calls = usage.get('total_calls', 0)
            total_calls += calls
            if calls > 0:
                tasks_with_calls += 1
            for cmd, count in usage.get('command_counts', {}).items():
                cmd_counts[cmd] = cmd_counts.get(cmd, 0) + count
            if usage.get('workflow_compliance', True):
                compliant += 1
            else:
                noncompliant += 1
        except: pass
total = compliant + noncompliant
print(f'  Total GT calls: {total_calls}')
print(f'  Tasks using GT: {tasks_with_calls}/{total}')
print(f'  Command breakdown: {cmd_counts}')
print(f'  Workflow compliant: {compliant}/{total}')
if noncompliant > 0:
    print(f'  Non-compliant: {noncompliant} tasks (>4 GT calls)')
"

# Correction summary
echo ""
echo "Post-processing correction summary:"
python3 -c "
import json, os
total_corrections = 0
tasks_corrected = 0
for d in sorted(os.listdir('$GT_DIR')):
    traj = os.path.join('$GT_DIR', d, f'{d}.traj.json')
    if os.path.isfile(traj):
        try:
            with open(traj) as f:
                data = json.load(f)
            report = data.get('info', {}).get('autocorrect_report', {})
            n = report.get('total_corrections', 0)
            total_corrections += n
            if n > 0:
                tasks_corrected += 1
        except: pass
print(f'  Total corrections: {total_corrections}')
print(f'  Tasks corrected: {tasks_corrected}')
"

# Test execution summary
echo ""
echo "Bounded test execution summary:"
python3 -c "
import json, os
for label, dirname in [('Baseline', '$BASELINE_DIR'), ('Phase 3', '$GT_DIR')]:
    tested = 0
    total = 0
    for d in sorted(os.listdir(dirname)):
        traj = os.path.join(dirname, d, f'{d}.traj.json')
        if os.path.isfile(traj):
            total += 1
            try:
                with open(traj) as f:
                    data = json.load(f)
                te = data.get('info', {}).get('test_execution', {})
                if te.get('test_executed'):
                    tested += 1
            except: pass
    pct = 100 * tested / total if total > 0 else 0
    print(f'  {label}: {tested}/{total} tasks ran tests ({pct:.1f}%)')
"

echo ""
echo "=== Ready for Docker evaluation ==="
echo "Condition A (Baseline): $BASELINE_DIR"
echo "Condition B (Phase 3 GT): $GT_DIR"
echo ""
echo "Next steps:"
echo "  1. Run: bash scripts/swebench/run_eval.sh $BASELINE_DIR"
echo "  2. Run: bash scripts/swebench/run_eval.sh $GT_DIR"
echo "  3. Compare results for A/B analysis"
