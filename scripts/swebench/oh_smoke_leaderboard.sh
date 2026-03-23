#!/bin/bash
set -euo pipefail

# Leaderboard Smoke Test — Gate 0
# Validates the full pipeline: proxy, model, injection, check execution, eval.
# Runs 1 task baseline + 1 task GT.
# Usage: bash oh_smoke_leaderboard.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
RESULTS_DIR="$HOME/results/leaderboard/smoke_$(date +%Y%m%d_%H%M%S)"

# One well-known task for smoke testing
SMOKE_INSTANCES="$RESULTS_DIR/smoke_instances.txt"

mkdir -p "$RESULTS_DIR"
echo "django__django-12856" > "$SMOKE_INSTANCES"

echo "============================================"
echo "  Leaderboard Smoke Test (Gate 0)"
echo "  Started: $(date -u) UTC"
echo "  Task: django__django-12856"
echo "  Results: $RESULTS_DIR"
echo "============================================"

# --- Pre-flight Checks ---
echo ""
echo "=== Pre-flight Checks ==="
PREFLIGHT_OK=true

# Check proxy
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "  FAIL: litellm proxy not running on port 4000"
    PREFLIGHT_OK=false
else
    echo "  Proxy: OK"
fi

# Check LLM config
if [ ! -f "$OH_DIR/.llm_config/vertex_qwen3.json" ]; then
    echo "  FAIL: vertex_qwen3.json not found"
    PREFLIGHT_OK=false
else
    echo "  LLM config: OK"
fi

# Check gt_tool_check_only.py
GT_TOOL="$REPO_DIR/benchmarks/swebench/gt_tool_check_only.py"
if [ ! -f "$GT_TOOL" ]; then
    echo "  FAIL: gt_tool_check_only.py not found at $GT_TOOL"
    PREFLIGHT_OK=false
else
    SIZE=$(wc -c < "$GT_TOOL")
    LINES=$(wc -l < "$GT_TOOL")
    echo "  GT tool: OK ($SIZE bytes, $LINES lines)"
fi

# Check prompt template
if [ ! -f "$SCRIPT_DIR/prompts/gt_check_hardgate.j2" ]; then
    echo "  FAIL: gt_check_hardgate.j2 not found"
    PREFLIGHT_OK=false
else
    echo "  Prompt: OK"
fi

# Check disk
echo "  Disk free: $(df -h / | tail -1 | awk '{print $4}')"

if [ "$PREFLIGHT_OK" = false ]; then
    echo ""
    echo "PREFLIGHT FAILED — fix issues above before proceeding."
    exit 1
fi

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

# Record initial load average
LOAD_BEFORE=$(cat /proc/loadavg 2>/dev/null | awk '{print $1}' || echo "N/A")
echo ""
echo "Load average before: $LOAD_BEFORE"

# --- Smoke A: Baseline ---
echo ""
echo "============================================"
echo "  SMOKE A: Baseline (1 task)"
echo "  Started: $(date -u) UTC"
echo "============================================"

bash "$SCRIPT_DIR/oh_run_leaderboard_baseline.sh" \
    --select "$SMOKE_INSTANCES" \
    --output-dir "$RESULTS_DIR/baseline" \
    --num-workers 1 2>&1 | tee "$RESULTS_DIR/baseline.log"

echo "Smoke A complete: $(date -u) UTC"

if [ -f "$RESULTS_DIR/baseline/output.jsonl" ]; then
    BASELINE_COUNT=$(wc -l < "$RESULTS_DIR/baseline/output.jsonl")
    echo "  Baseline tasks completed: $BASELINE_COUNT"
else
    echo "  WARNING: No output.jsonl found for baseline"
fi

# --- Smoke B: GT ---
echo ""
echo "============================================"
echo "  SMOKE B: GT gt_check hardgate (1 task)"
echo "  Started: $(date -u) UTC"
echo "============================================"

bash "$SCRIPT_DIR/oh_run_leaderboard_gt.sh" \
    --select "$SMOKE_INSTANCES" \
    --output-dir "$RESULTS_DIR/gt" \
    --num-workers 1 2>&1 | tee "$RESULTS_DIR/gt.log"

echo "Smoke B complete: $(date -u) UTC"

if [ -f "$RESULTS_DIR/gt/output.jsonl" ]; then
    GT_COUNT=$(wc -l < "$RESULTS_DIR/gt/output.jsonl")
    echo "  GT tasks completed: $GT_COUNT"
else
    echo "  WARNING: No output.jsonl found for GT"
fi

# --- Gate 0 Audit ---
echo ""
echo "============================================"
echo "  GATE 0 AUDIT"
echo "============================================"

# Check GT tool injection evidence
echo ""
echo "--- GT Tool Injection ---"
GT_CHECK_CALLS=$(grep -r "groundtruth_check\|gt_tool.py\|GT_READY" "$RESULTS_DIR/gt/" 2>/dev/null | wc -l || echo 0)
echo "  GT tool references in GT output: $GT_CHECK_CALLS"

if [ "$GT_CHECK_CALLS" -gt 0 ]; then
    echo "  GT tool injection: CONFIRMED"
else
    echo "  GT tool injection: NOT DETECTED — check trajectories manually"
fi

# Check load average
LOAD_AFTER=$(cat /proc/loadavg 2>/dev/null | awk '{print $1}' || echo "N/A")
echo ""
echo "--- System Health ---"
echo "  Load before: $LOAD_BEFORE"
echo "  Load after: $LOAD_AFTER"

echo ""
echo "--- Disk Usage ---"
df -h / | tail -1

echo ""
echo "============================================"
echo "  GATE 0 COMPLETE: $(date -u) UTC"
echo "  Results: $RESULTS_DIR"
echo ""
echo "  Gate 0 pass criteria:"
echo "    [ ] Both runs completed (check output.jsonl)"
echo "    [ ] GT tool injection confirmed"
echo "    [ ] Load average stayed < 10.0"
echo "    [ ] No crashes or timeouts"
echo "============================================"
