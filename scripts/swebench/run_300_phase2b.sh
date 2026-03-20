#!/bin/bash
set -e
source ~/gt-venv/bin/activate
source ~/gt-env.sh
cd ~/groundtruth

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# ───────────────────────────────────────────────────
# Phase 2B: Both conditions in parallel
# Condition A: Clean baseline (no GT)
# Condition B: Phase 2B (post-processing only + bounded test feedback)
# ───────────────────────────────────────────────────

BASELINE_DIR=~/phase2b_baseline_${TIMESTAMP}
GT_DIR=~/phase2b_gt_${TIMESTAMP}
mkdir -p "$BASELINE_DIR" "$GT_DIR"

echo "=== Phase 2B Full 300-Task Run ==="
echo "Started: $(date -u) UTC"
echo "Baseline output: $BASELINE_DIR"
echo "GT output: $GT_DIR"

# Save manifests
for DIR in "$BASELINE_DIR" "$GT_DIR"; do
    CONDITION="baseline"
    CONFIG="benchmarks/swebench/mini_swebench_baseline.yaml"
    if [ "$DIR" = "$GT_DIR" ]; then
        CONDITION="phase2b"
        CONFIG="benchmarks/swebench/mini_swebench_phase2b.yaml"
    fi
    cat > "$DIR/MANIFEST.txt" <<MANIFEST
Phase 2B — $CONDITION
Date (UTC): $(date -u)
Git branch: $(git branch --show-current)
Git commit: $(git rev-parse HEAD)
Model: openai/gpt-5.4-nano
Scaffold: mini-swe-agent
Config: $CONFIG
Condition: $CONDITION
MANIFEST
    # Save exact system prompt
    cp "$CONFIG" "$DIR/config_used.yaml"
done

# Save GT-specific artifacts
cp benchmarks/swebench/gt_autocorrect.py "$GT_DIR/gt_autocorrect_snapshot.py"
cp benchmarks/swebench/gt_runtime_kb.py "$GT_DIR/gt_runtime_kb_snapshot.py"
cp benchmarks/swebench/run_mini_gt.py "$GT_DIR/run_mini_gt_snapshot.py"

echo ""
echo "=== Starting BASELINE run (4 workers) ==="
echo "Started: $(date)"

python3 -m minisweagent.run.benchmarks.swebench \
  -c benchmarks/swebench/mini_swebench_baseline.yaml \
  --model openai/gpt-5.4-nano \
  --subset lite --split test \
  -w 4 \
  -o "$BASELINE_DIR" \
  2>&1 | tee "$BASELINE_DIR/run.log" &
BASELINE_PID=$!

echo ""
echo "=== Starting GT Phase 2B run (4 workers) ==="
echo "Started: $(date)"

python3 benchmarks/swebench/run_mini_gt.py \
  -c benchmarks/swebench/mini_swebench_phase2b.yaml \
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

    # Check if processes are still running
    if ! kill -0 $BASELINE_PID 2>/dev/null; then
        BASELINE_DONE=1
    fi
    if ! kill -0 $GT_PID 2>/dev/null; then
        GT_DONE=1
    fi

    # Count completed tasks
    BASELINE_TASKS=$(find "$BASELINE_DIR" -name "*.traj.json" 2>/dev/null | wc -l)
    GT_TASKS=$(find "$GT_DIR" -name "*.traj.json" 2>/dev/null | wc -l)

    # Count patches
    BASELINE_PATCHES=$(python3 -c "
import json, os
count = 0
for d in os.listdir('$BASELINE_DIR'):
    traj = os.path.join('$BASELINE_DIR', d, f'{d}.traj.json')
    if os.path.isfile(traj):
        try:
            with open(traj) as f:
                data = json.load(f)
            sub = data.get('info', {}).get('submission', '')
            if sub and sub.strip():
                count += 1
        except: pass
print(count)
" 2>/dev/null || echo "?")

    GT_PATCHES=$(python3 -c "
import json, os
count = 0
for d in os.listdir('$GT_DIR'):
    traj = os.path.join('$GT_DIR', d, f'{d}.traj.json')
    if os.path.isfile(traj):
        try:
            with open(traj) as f:
                data = json.load(f)
            sub = data.get('info', {}).get('submission', '')
            if sub and sub.strip():
                count += 1
        except: pass
print(count)
" 2>/dev/null || echo "?")

    # GT-specific stats
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

    echo "[$(date +%H:%M:%S)] Baseline: $BASELINE_TASKS tasks, $BASELINE_PATCHES patches | GT: $GT_TASKS tasks, $GT_PATCHES patches, $GT_CORRECTIONS corrections, $GT_TOOL_CALLS tool_calls"

    # Mid-run audit at ~100 tasks
    if [ "$GT_TASKS" -ge 100 ] && [ ! -f "$GT_DIR/.audit_100_done" ]; then
        echo ""
        echo "=== MID-RUN AUDIT (GT at $GT_TASKS tasks) ==="
        if [ "$GT_TOOL_CALLS" != "0" ]; then
            echo "CRITICAL: GT tool calls detected ($GT_TOOL_CALLS)! Stopping GT run."
            kill $GT_PID 2>/dev/null || true
            echo "GT run killed. Fix the tool injection leak."
            touch "$GT_DIR/.audit_100_done"
            exit 1
        else
            echo "  GT tool calls: $GT_TOOL_CALLS (GOOD - zero)"
            echo "  Corrections: $GT_CORRECTIONS"
            echo "  Patches: $GT_PATCHES/$GT_TASKS"
        fi
        touch "$GT_DIR/.audit_100_done"
        echo ""
    fi

    # Both done?
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

echo "Baseline: $BASELINE_FINAL tasks completed"
echo "GT Phase 2B: $GT_FINAL tasks completed"

# Verify zero GT tool calls
echo ""
echo "GT tool call verification:"
python3 -c "
import json, os
total_calls = 0
tasks_with_calls = []
for d in sorted(os.listdir('$GT_DIR')):
    traj = os.path.join('$GT_DIR', d, f'{d}.traj.json')
    if os.path.isfile(traj):
        try:
            with open(traj) as f:
                data = json.load(f)
            usage = data.get('info', {}).get('gt_tool_usage', {})
            calls = usage.get('total_calls', 0)
            if calls > 0:
                total_calls += calls
                tasks_with_calls.append((d, calls))
        except: pass
if total_calls == 0:
    print('  PASS: Zero GT tool calls across all tasks')
else:
    print(f'  FAIL: {total_calls} GT tool calls in {len(tasks_with_calls)} tasks:')
    for task, calls in tasks_with_calls:
        print(f'    {task}: {calls} calls')
"

# Correction summary
echo ""
echo "Correction summary:"
python3 -c "
import json, os
total_corrections = 0
total_gated = 0
by_type = {}
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
            total_gated += report.get('gated_out', 0)
            if n > 0:
                tasks_corrected += 1
            for ct, count in report.get('by_type', {}).items():
                by_type[ct] = by_type.get(ct, 0) + count
        except: pass
print(f'  Total corrections applied: {total_corrections}')
print(f'  Total gated out: {total_gated}')
print(f'  Tasks with corrections: {tasks_corrected}')
print(f'  By type: {by_type}')
"

# Test execution summary
echo ""
echo "Bounded test execution summary:"
python3 -c "
import json, os
tested = 0
not_tested = 0
for d in sorted(os.listdir('$GT_DIR')):
    traj = os.path.join('$GT_DIR', d, f'{d}.traj.json')
    if os.path.isfile(traj):
        try:
            with open(traj) as f:
                data = json.load(f)
            te = data.get('info', {}).get('test_execution', {})
            if te.get('test_executed'):
                tested += 1
            else:
                not_tested += 1
        except: pass
total = tested + not_tested
pct = 100 * tested / total if total > 0 else 0
print(f'  Tasks with test execution: {tested}/{total} ({pct:.1f}%)')
"

echo ""
echo "=== Ready for Docker evaluation ==="
echo "Baseline dir: $BASELINE_DIR"
echo "GT dir: $GT_DIR"
echo ""
echo "Next steps:"
echo "  1. Run: bash scripts/swebench/run_eval.sh $BASELINE_DIR"
echo "  2. Run: bash scripts/swebench/run_eval.sh $GT_DIR"
echo "  3. Compare results"
