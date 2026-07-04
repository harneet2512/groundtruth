#!/bin/bash
set -euo pipefail

# Full OpenHands evaluation orchestrator for one VM
# Usage: bash oh_run_full.sh --condition baseline|gt --shard instances_a.txt
#
# Runs one condition (baseline or gt) with the specified shard.
# For parallel evaluation: run baseline on VM-A, gt on VM-B.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Parse args
CONDITION=""
SHARD=""
NUM_WORKERS=4

while [[ $# -gt 0 ]]; do
    case $1 in
        --condition) CONDITION="$2"; shift 2 ;;
        --shard) SHARD="$2"; shift 2 ;;
        --num-workers) NUM_WORKERS="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [ -z "$CONDITION" ] || [ -z "$SHARD" ]; then
    echo "Usage: bash oh_run_full.sh --condition baseline|gt --shard /path/to/instances.txt"
    exit 1
fi

if [ "$CONDITION" != "baseline" ] && [ "$CONDITION" != "gt" ]; then
    echo "ERROR: condition must be 'baseline' or 'gt'"
    exit 1
fi

if [ ! -f "$SHARD" ]; then
    echo "ERROR: shard file not found: $SHARD"
    exit 1
fi

TASK_COUNT=$(wc -l < "$SHARD")
RESULTS_DIR="$HOME/results/${CONDITION}_${TIMESTAMP}"

echo "============================================"
echo "  OpenHands Full Run: $CONDITION"
echo "  Started: $(date -u) UTC"
echo "  Shard: $SHARD ($TASK_COUNT tasks)"
echo "  Workers: $NUM_WORKERS"
echo "  Results: $RESULTS_DIR"
echo "============================================"

# Save metadata
mkdir -p "$RESULTS_DIR"
cat > "$RESULTS_DIR/METADATA.json" << METAEOF
{
  "date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "condition": "$CONDITION",
  "shard": "$(basename $SHARD)",
  "task_count": $TASK_COUNT,
  "num_workers": $NUM_WORKERS,
  "model": "vertex_ai/qwen3-coder-480b",
  "scaffold": "openhands",
  "max_iterations": 100,
  "gt_branch": "$(cd $REPO_DIR && git branch --show-current 2>/dev/null || echo 'unknown')",
  "gt_commit": "$(cd $REPO_DIR && git rev-parse HEAD 2>/dev/null || echo 'unknown')",
  "oh_commit": "$(cd $OH_DIR && git rev-parse HEAD 2>/dev/null || echo 'unknown')",
  "hostname": "$(hostname)"
}
METAEOF

# Run the appropriate condition
if [ "$CONDITION" = "baseline" ]; then
    bash "$SCRIPT_DIR/oh_run_baseline.sh" \
        --select "$SHARD" \
        --output-dir "$RESULTS_DIR" \
        --num-workers "$NUM_WORKERS" \
        2>&1 | tee "$RESULTS_DIR/run.log"
else
    bash "$SCRIPT_DIR/oh_run_gt.sh" \
        --select "$SHARD" \
        --output-dir "$RESULTS_DIR" \
        --num-workers "$NUM_WORKERS" \
        2>&1 | tee "$RESULTS_DIR/run.log"
fi

echo ""
echo "============================================"
echo "  Run Complete: $CONDITION"
echo "  Finished: $(date -u) UTC"
echo "============================================"

if [ -f "$RESULTS_DIR/output.jsonl" ]; then
    echo "Tasks completed: $(wc -l < "$RESULTS_DIR/output.jsonl") / $TASK_COUNT"
fi
echo "Disk: $(df -h / | tail -1)"
