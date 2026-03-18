#!/bin/bash
# Smoke test for v3.1 file-based GT delivery.
# Runs 1 task (django__django-11049) and verifies:
#   1. gt_file_written == true in trajectory info
#   2. Agent trajectory contains "gt_analysis" (read the file)
#   3. Task completes and submits a patch
#
# Usage: bash smoke_test_v31.sh
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

SMOKE_DIR="benchmarks/swebench/results/smoke_v31_$(date +%Y%m%d_%H%M)"
mkdir -p "$SMOKE_DIR"
echo "=== Smoke Test v3.1 — File-Based GT Delivery ==="
echo "Output: $SMOKE_DIR"
echo "Task: django__django-11049"
echo "Started: $(date)"

# Run single task with GT file delivery
python3 benchmarks/swebench/run_mini_gt.py \
  -c benchmarks/swebench/mini_swebench_gt.yaml \
  -m openai/gpt-5.4-nano \
  --subset lite --split test \
  --filter "django__django-11049" \
  -o "$SMOKE_DIR/gt" \
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

# Check 1: gt_file_written
GT_WRITTEN=$(python3 -c "
import json, sys
with open('$TRAJ') as f:
    d = json.load(f)
info = d.get('info', {})
print(info.get('gt_file_written', False))
")
if [ "$GT_WRITTEN" = "True" ]; then
  echo "[PASS] gt_file_written = True"
else
  echo "[FAIL] gt_file_written = $GT_WRITTEN (expected True)"
fi

# Check 2: gt_delivery mode
GT_DELIVERY=$(python3 -c "
import json
with open('$TRAJ') as f:
    d = json.load(f)
print(d.get('info', {}).get('gt_delivery', 'NOT SET'))
")
echo "       gt_delivery = $GT_DELIVERY"

# Check 3: Agent read /tmp/gt_analysis.md
GT_READ=$(python3 -c "
import json
with open('$TRAJ') as f:
    d = json.load(f)
evidence = d.get('info', {}).get('gt_read_evidence', {})
print(json.dumps(evidence, indent=2))
")
echo "       gt_read_evidence = $GT_READ"

READ_FILE=$(python3 -c "
import json
with open('$TRAJ') as f:
    d = json.load(f)
print(d.get('info', {}).get('gt_read_evidence', {}).get('read_file', False))
")
if [ "$READ_FILE" = "True" ]; then
  echo "[PASS] Agent read /tmp/gt_analysis.md"
else
  echo "[WARN] Agent did NOT read /tmp/gt_analysis.md — may need stronger instruction"
fi

# Check 4: Task completed with submission
SUBMISSION=$(python3 -c "
import json
with open('$TRAJ') as f:
    d = json.load(f)
sub = d.get('info', {}).get('submission', '')
print(len(sub) if sub else 0)
")
if [ "$SUBMISSION" -gt 0 ] 2>/dev/null; then
  echo "[PASS] Task submitted patch ($SUBMISSION chars)"
else
  echo "[WARN] No patch submitted (submission length: $SUBMISSION)"
fi

# Check 5: GT version
GT_VERSION=$(python3 -c "
import json
with open('$TRAJ') as f:
    d = json.load(f)
print(d.get('info', {}).get('gt_version', 'NOT SET'))
")
echo "       gt_version = $GT_VERSION"

echo ""
echo "=== Smoke Test Complete ==="
echo "Finished: $(date)"
echo "Review: cat $TRAJ | python3 -m json.tool | less"
