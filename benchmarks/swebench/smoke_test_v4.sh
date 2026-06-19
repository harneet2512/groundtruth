#!/bin/bash
# Smoke test for v4 on-demand GT tools.
# Runs 1 task (django__django-11049) and verifies:
#   1. gt_version contains 'v4' (not old context injection)
#   2. No pre-computed context was generated
#   3. Agent called gt_tool.py at least once
#   4. Task completes and submits a patch
#
# Usage: bash smoke_test_v4.sh
# Run on VM with mini-swe-agent installed.
set -e

REPO_ROOT="${REPO_ROOT:-$HOME/groundtruth}"
cd "$REPO_ROOT"
git pull 2>/dev/null || true

# Source API keys from bashrc (non-interactive shells skip bashrc guards)
# shellcheck disable=SC1090
eval "$(grep '^export.*API_KEY' "$HOME/.bashrc" 2>/dev/null)" || true

# Ensure mini-swe-agent is on PYTHONPATH
export PYTHONPATH="${HOME}/mini-swe-agent/src:${PYTHONPATH:-}"

SMOKE_DIR="benchmarks/swebench/results/smoke_v4_$(date +%Y%m%d_%H%M)"
mkdir -p "$SMOKE_DIR"
echo "============================================================"
echo "  Smoke Test: GT v4 On-Demand Tools"
echo "============================================================"
echo "Output: $SMOKE_DIR"
echo "Task: django__django-11049"
echo "Started: $(date)"
echo ""

# Run single task with GT v4 tool delivery
python3 benchmarks/swebench/run_mini_gt.py \
  -c benchmarks/swebench/mini_swebench_gt_v4.yaml \
  -m openai/gpt-5.4-nano \
  --subset lite --split test \
  --filter "django__django-11049" \
  -o "$SMOKE_DIR/gt_v4" \
  -w 1 \
  2>&1 | tee "$SMOKE_DIR/run.log"

echo ""
echo "=== Smoke Test Verification ==="

# Find trajectory file
TRAJ=$(find "$SMOKE_DIR" -name "*.traj.json" -type f | head -1)
if [ -z "$TRAJ" ]; then
  echo "[FAIL] No trajectory file found in $SMOKE_DIR"
  exit 1
fi
echo "Trajectory: $TRAJ"
echo ""

# Run all checks
python3 << VERIFY
import json, sys

with open('$TRAJ') as f:
    traj = json.load(f)

info = traj.get('info', {})
passed = 0
total = 4

# Check 1: GT version is v4
version = info.get('gt_version', 'MISSING')
if 'v4' in str(version) or 'tool' in str(version):
    print(f"[PASS] gt_version = {version}")
    passed += 1
else:
    print(f"[FAIL] gt_version = {version} — expected 'v4_ondemand_tools'")

# Check 2: No pre-computed context
context = info.get('gt_context', info.get('context_block', ''))
delivery = info.get('gt_delivery', 'MISSING')
if not context and delivery == 'tool':
    print(f"[PASS] No pre-computed context, gt_delivery = {delivery}")
    passed += 1
else:
    if context:
        print(f"[FAIL] Pre-computed context found ({len(context)} chars) — wrapper still generating context!")
    else:
        print(f"[WARN] gt_delivery = {delivery} (expected 'tool')")

# Check 3: Agent called gt_tool.py
usage = info.get('gt_tool_usage', {})
if usage.get('any_call'):
    cmds = ', '.join(usage.get('commands_used', []))
    syms = ', '.join(usage.get('symbols_queried', []))
    turn = usage.get('first_call_turn', '?')
    total_calls = usage.get('total_calls', 0)
    print(f"[PASS] Agent called gt_tool.py: {total_calls} calls, first at turn {turn}")
    print(f"       Commands: {cmds}")
    print(f"       Symbols: {syms}")
    passed += 1
else:
    print(f"[WARN] Agent did NOT call gt_tool.py")
    print(f"       Tool available: {info.get('gt_tool_available', 'unknown')}")
    print(f"       Check system prompt — are tool instructions visible?")

# Check 4: Task submitted a patch
submission = info.get('submission', '')
if submission and len(submission) > 0:
    print(f"[PASS] Task submitted patch ({len(submission)} chars)")
    passed += 1
else:
    exit_status = info.get('exit_status', 'unknown')
    print(f"[WARN] No patch submitted (exit_status: {exit_status})")

# Summary
print(f"")
print(f"=== {passed}/{total} checks passed ===")

# Show GT tool interactions from trajectory
msgs = traj.get('history', traj.get('messages', traj.get('trajectory', [])))
if isinstance(msgs, list):
    gt_interactions = []
    for i, msg in enumerate(msgs):
        content = str(msg.get('content', '') if isinstance(msg, dict) else msg)
        if 'gt_tool' in content:
            role = msg.get('role', '?') if isinstance(msg, dict) else '?'
            snippet = content[:300].replace('\n', ' ')
            gt_interactions.append(f"  Turn {i} [{role}]: {snippet}")
    if gt_interactions:
        print(f"")
        print(f"--- Agent GT interactions ---")
        for line in gt_interactions[:10]:
            print(line)
        if len(gt_interactions) > 10:
            print(f"  ... and {len(gt_interactions) - 10} more")
    else:
        print(f"")
        print(f"--- No GT tool mentions found in trajectory ---")

# Index info
print(f"")
print(f"--- Setup stats ---")
print(f"  Tool available: {info.get('gt_tool_available', '?')}")

if passed < 3:
    sys.exit(1)
VERIFY

echo ""
echo "=== Smoke Test Complete ==="
echo "Finished: $(date)"
echo "Review: cat $TRAJ | python3 -m json.tool | less"
