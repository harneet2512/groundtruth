#!/bin/bash
# Run this on the VM (swebench-ab) to start diagnostic runs.
#
# Usage:
#   bash start_diagnostic_vm.sh              # Full A/B test (baseline + GT v3.1)
#   bash start_diagnostic_vm.sh smoke        # Smoke test (1 task only)
#   bash start_diagnostic_vm.sh gt-only      # GT v3.1 only on all 10 tasks
#
# Model: gpt-5.4-nano
set -e

REPO_ROOT="${REPO_ROOT:-$HOME/groundtruth}"
cd "$REPO_ROOT"
git pull 2>/dev/null || true

MODE="${1:-ab}"

case "$MODE" in
  smoke)
    echo "=== Running smoke test (1 task) ==="
    bash benchmarks/swebench/smoke_test_v31.sh
    ;;
  gt-only)
    echo "=== Running GT v3.1 only (all 10 diagnostic tasks) ==="
    DIAG_DIR="benchmarks/swebench/results/gt_v31_$(date +%Y%m%d_%H%M)"
    mkdir -p "$DIAG_DIR"
    TASKS_FILE="benchmarks/swebench/diagnostic_tasks.txt"
    INSTANCE_IDS=$(cat "$TASKS_FILE" | tr '\n' ' ')

    nohup python3 benchmarks/swebench/run_mini_gt.py swebench \
      -c benchmarks/swebench/mini_swebench_gt.yaml \
      --model openai/gpt-5.4-nano \
      --subset lite --split test \
      --instance-id $INSTANCE_IDS \
      -o "$DIAG_DIR" \
      -w 2 \
      > "$DIAG_DIR/run.log" 2>&1 &

    PID=$!
    echo "Started with PID: $PID"
    echo "Output: $DIAG_DIR"
    echo "Monitor: tail -f $DIAG_DIR/run.log"
    sleep 3
    tail -30 "$DIAG_DIR/run.log" 2>/dev/null || true
    ;;
  ab|*)
    echo "=== Running full A/B test (baseline + GT v3.1) ==="
    nohup bash benchmarks/swebench/run_ab_test.sh \
      > "benchmarks/swebench/results/ab_test_$(date +%Y%m%d_%H%M).log" 2>&1 &
    PID=$!
    echo "Started A/B test with PID: $PID"
    echo "Monitor: tail -f benchmarks/swebench/results/ab_test_*.log"
    sleep 3
    tail -30 benchmarks/swebench/results/ab_test_*.log 2>/dev/null || true
    ;;
esac
