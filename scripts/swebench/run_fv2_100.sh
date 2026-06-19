#!/bin/bash
set -euo pipefail

REPO_DIR="$HOME/groundtruth"
RESULTS_DIR="$HOME/foundation_v2"
MANIFEST="$RESULTS_DIR/manifests/hundred_tasks.txt"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "============================================"
echo "  Foundation v2 — 100-Task A/B Experiment"
echo "  Started: $(date -u) UTC"
echo "  Manifest: $MANIFEST ($(wc -l < $MANIFEST) tasks)"
echo "============================================"

# Ensure proxy is running
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running. Run start_proxy.sh first."
    exit 1
fi
echo "Proxy: OK"

# Convert manifest to comma-separated instance list
INSTANCES=$(paste -sd ',' "$MANIFEST")

# Save experiment metadata
cat > "$RESULTS_DIR/EXPERIMENT.json" << METAEOF
{
  "date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "branch": "$(cd $REPO_DIR && git branch --show-current)",
  "commit": "$(cd $REPO_DIR && git rev-parse HEAD)",
  "model": "vertex_ai/qwen3-coder-480b",
  "scaffold": "openhands",
  "tasks": 100,
  "conditions": ["baseline", "gt_phase3"],
  "manifest": "hundred_tasks.txt",
  "seed": 2026
}
METAEOF

# --- Condition A: Baseline ---
echo ""
echo "============================================"
echo "  CONDITION A: Baseline (no GT)"
echo "  Started: $(date -u) UTC"
echo "============================================"
mkdir -p "$RESULTS_DIR/condition_a"

bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_baseline.sh" \
    --instances "$INSTANCES" \
    --output-dir "$RESULTS_DIR/condition_a" \
    --max-iterations 100 2>&1 | tee "$RESULTS_DIR/condition_a/run.log"

echo ""
echo "Condition A complete: $(date -u) UTC"
echo "Disk usage: $(df -h / | tail -1 | awk '{print $5}')"

# Clean up stopped containers between conditions
echo "Cleaning containers..."
docker container prune -f 2>/dev/null | tail -1

# --- Condition B: GT Phase 3 ---
echo ""
echo "============================================"
echo "  CONDITION B: GT Phase 3 (pre-computed analysis)"
echo "  Started: $(date -u) UTC"
echo "============================================"
mkdir -p "$RESULTS_DIR/condition_b"

bash "$REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh" \
    --instances "$INSTANCES" \
    --output-dir "$RESULTS_DIR/condition_b" \
    --max-iterations 100 2>&1 | tee "$RESULTS_DIR/condition_b/run.log"

echo ""
echo "Condition B complete: $(date -u) UTC"

# --- Summary ---
echo ""
echo "============================================"
echo "  EXPERIMENT COMPLETE"
echo "  Finished: $(date -u) UTC"
echo "============================================"
echo ""

# Count outputs
for cond in condition_a condition_b; do
    output_file=$(find "$RESULTS_DIR/$cond" -name "output.jsonl" | head -1)
    if [ -n "$output_file" ]; then
        count=$(wc -l < "$output_file")
        echo "$cond: $count tasks completed"
    else
        echo "$cond: no output.jsonl found"
    fi
done

echo ""
echo "Disk usage: $(df -h / | tail -1)"
echo ""
echo "Next: run evaluation with scripts/swebench/run_eval.sh"
