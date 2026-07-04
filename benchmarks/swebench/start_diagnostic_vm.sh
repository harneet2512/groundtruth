#!/bin/bash
# Run this on the VM (swebench-ab) to start diagnostic runs.
#
# Usage:
#   bash start_diagnostic_vm.sh              # Full A/B test (baseline + GT v4)
#   bash start_diagnostic_vm.sh smoke        # Smoke test v4 (1 task only)
#   bash start_diagnostic_vm.sh gt-only      # GT v4 only on all 10 tasks
#   bash start_diagnostic_vm.sh smoke-v31    # Smoke test v3.1 (legacy)
#   bash start_diagnostic_vm.sh ab-v31       # A/B test v3.1 (legacy)
#
# Model: gpt-5.4-nano
set -e

REPO_ROOT="${REPO_ROOT:-$HOME/groundtruth}"
cd "$REPO_ROOT"
git pull 2>/dev/null || true

# Source API keys from bashrc (non-interactive shells skip bashrc guards)
# shellcheck disable=SC1090
eval "$(grep '^export.*API_KEY' "$HOME/.bashrc" 2>/dev/null)" || true

# Ensure mini-swe-agent is on PYTHONPATH
export PYTHONPATH="${HOME}/mini-swe-agent/src:${PYTHONPATH:-}"

MODE="${1:-ab}"

case "$MODE" in
  smoke)
    echo "=== Running smoke test v4 (1 task) ==="
    bash benchmarks/swebench/smoke_test_v4.sh
    ;;
  gt-only)
    echo "=== Running GT v4 only (all 10 diagnostic tasks) ==="
    DIAG_DIR="benchmarks/swebench/results/gt_v4_$(date +%Y%m%d_%H%M)"
    mkdir -p "$DIAG_DIR"
    TASKS_FILE="benchmarks/swebench/diagnostic_tasks.txt"
    FILTER_REGEX=$(tr -d '\r' < "$TASKS_FILE" | tr '\n' '|' | sed 's/|$//')

    nohup python3 benchmarks/swebench/run_mini_gt.py \
      -c benchmarks/swebench/mini_swebench_gt_v4.yaml \
      -m openai/gpt-5.4-nano \
      --subset lite --split test \
      --filter "$FILTER_REGEX" \
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
  ab|ab-v4)
    echo "=== Running full A/B test (baseline + GT v4) ==="
    nohup bash benchmarks/swebench/run_ab_test_v4.sh \
      > "benchmarks/swebench/results/ab_v4_test_$(date +%Y%m%d_%H%M).log" 2>&1 &
    PID=$!
    echo "Started A/B test with PID: $PID"
    echo "Monitor: tail -f benchmarks/swebench/results/ab_v4_test_*.log"
    sleep 3
    tail -30 benchmarks/swebench/results/ab_v4_test_*.log 2>/dev/null || true
    ;;
  smoke-v31)
    echo "=== Running smoke test v3.1 (legacy, 1 task) ==="
    bash benchmarks/swebench/smoke_test_v31.sh
    ;;
  ab-v31)
    echo "=== Running full A/B test v3.1 (legacy) ==="
    nohup bash benchmarks/swebench/run_ab_test.sh \
      > "benchmarks/swebench/results/ab_v31_test_$(date +%Y%m%d_%H%M).log" 2>&1 &
    PID=$!
    echo "Started A/B test with PID: $PID"
    echo "Monitor: tail -f benchmarks/swebench/results/ab_v31_test_*.log"
    sleep 3
    tail -30 benchmarks/swebench/results/ab_v31_test_*.log 2>/dev/null || true
    ;;
  *)
    echo "Usage: bash start_diagnostic_vm.sh [smoke|gt-only|ab|smoke-v31|ab-v31]"
    echo ""
    echo "  smoke      — Smoke test v4 on-demand tools (1 task)"
    echo "  gt-only    — GT v4 only on all 10 diagnostic tasks"
    echo "  ab         — Full A/B test: baseline vs GT v4 (default)"
    echo "  smoke-v31  — Smoke test v3.1 file delivery (legacy)"
    echo "  ab-v31     — Full A/B test: baseline vs GT v3.1 (legacy)"
    exit 1
    ;;
esac
