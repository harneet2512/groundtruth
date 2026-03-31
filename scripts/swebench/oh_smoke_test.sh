#!/bin/bash
set -euo pipefail

# Smoke test: 2 tasks baseline + 2 tasks GT
# Validates the full pipeline before launching 500-task runs
# Usage: bash oh_smoke_test.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
RESULTS_DIR="$HOME/results/smoke_$(date +%Y%m%d_%H%M%S)"

# Two well-known tasks for smoke testing
SMOKE_INSTANCES="$RESULTS_DIR/smoke_instances.txt"

mkdir -p "$RESULTS_DIR"

# Pick 2 tasks that are known to have images available
cat > "$SMOKE_INSTANCES" << 'EOF'
django__django-12856
django__django-13158
EOF

echo "============================================"
echo "  OpenHands Smoke Test"
echo "  Started: $(date -u) UTC"
echo "  Tasks: $(wc -l < $SMOKE_INSTANCES)"
echo "  Results: $RESULTS_DIR"
echo "============================================"

# --- Pre-flight checks ---
echo ""
echo "=== Pre-flight Checks ==="

# Check proxy
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "FAIL: litellm proxy not running on port 4000"
    exit 1
fi
echo "  Proxy: OK"

# Check LLM config
if [ ! -f "$OH_DIR/.llm_config/vertex_qwen3.json" ]; then
    echo "FAIL: vertex_qwen3.json not found"
    exit 1
fi
echo "  LLM config: OK"

# Check gt_tool.py
if [ ! -f "$REPO_DIR/benchmarks/swebench/gt_tool.py" ]; then
    echo "FAIL: gt_tool.py not found"
    exit 1
fi
echo "  gt_tool.py: OK"

# Check disk
DISK_FREE=$(df / | tail -1 | awk '{print $4}')
echo "  Disk free: $(df -h / | tail -1 | awk '{print $4}')"

# Test model inference
echo ""
echo "=== Testing Model Inference ==="
RESPONSE=$(curl -s --max-time 30 http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer dummy" \
    -d '{"model":"qwen3-coder","messages":[{"role":"user","content":"Say hi"}],"max_tokens":10}' 2>&1)

if echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])" 2>/dev/null; then
    echo "  Model inference: OK"
else
    echo "  Model inference: FAIL"
    echo "  Response: $RESPONSE"
    exit 1
fi

# --- Smoke A: Baseline ---
echo ""
echo "============================================"
echo "  SMOKE A: Baseline (2 tasks)"
echo "  Started: $(date -u) UTC"
echo "============================================"

bash "$SCRIPT_DIR/oh_run_baseline.sh" \
    --select "$SMOKE_INSTANCES" \
    --output-dir "$RESULTS_DIR/baseline" \
    --num-workers 2 2>&1 | tee "$RESULTS_DIR/baseline.log"

echo ""
echo "Smoke A complete: $(date -u) UTC"

# Verify baseline output
if [ -f "$RESULTS_DIR/baseline/output.jsonl" ]; then
    BASELINE_COUNT=$(wc -l < "$RESULTS_DIR/baseline/output.jsonl")
    echo "  Baseline tasks completed: $BASELINE_COUNT"
else
    echo "  WARNING: No output.jsonl found for baseline"
fi

# --- Smoke B: GT ---
echo ""
echo "============================================"
echo "  SMOKE B: GT gt_check (2 tasks)"
echo "  Started: $(date -u) UTC"
echo "============================================"

bash "$SCRIPT_DIR/oh_run_gt.sh" \
    --select "$SMOKE_INSTANCES" \
    --output-dir "$RESULTS_DIR/gt" \
    --num-workers 2 2>&1 | tee "$RESULTS_DIR/gt.log"

echo ""
echo "Smoke B complete: $(date -u) UTC"

# Verify GT output
if [ -f "$RESULTS_DIR/gt/output.jsonl" ]; then
    GT_COUNT=$(wc -l < "$RESULTS_DIR/gt/output.jsonl")
    echo "  GT tasks completed: $GT_COUNT"
else
    echo "  WARNING: No output.jsonl found for GT"
fi

# --- Audit ---
echo ""
echo "============================================"
echo "  SMOKE AUDIT"
echo "============================================"

# Check for gt_check usage in GT trajectories
echo ""
echo "--- GT Tool Usage ---"
GT_CHECK_CALLS=$(grep -r "groundtruth_check\|gt_tool.py" "$RESULTS_DIR/gt/" 2>/dev/null | wc -l || echo 0)
echo "  gt_check references in GT output: $GT_CHECK_CALLS"

if [ "$GT_CHECK_CALLS" -gt 0 ]; then
    echo "  GT tool injection: CONFIRMED"
else
    echo "  GT tool injection: NOT DETECTED (check trajectories manually)"
fi

echo ""
echo "--- Disk Usage ---"
df -h / | tail -1

echo ""
echo "============================================"
echo "  SMOKE TEST COMPLETE: $(date -u) UTC"
echo "  Results: $RESULTS_DIR"
echo "============================================"
