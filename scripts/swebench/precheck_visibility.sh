#!/bin/bash
set -e

# PRECHECK VISIBILITY RUN
# Runs 1 task (beets-5495) with full GT logging, then checks every bug fix.
# Must pass ALL checks before 5-task proof run.

echo "=========================================="
echo "  GT PRECHECK: 1-task visibility run"
echo "  Task: beetbox__beets-5495"
echo "  Purpose: verify BUG-1 through BUG-13 fixes"
echo "=========================================="

# --- Setup ---
source /opt/oh-env/bin/activate 2>/dev/null || true
cd /opt/groundtruth || cd /tmp/groundtruth

git pull origin jedi__branch --ff-only 2>/dev/null || git fetch origin jedi__branch && git checkout jedi__branch
pip install -e . -q 2>/dev/null

export GT_ROUTER_V2=live
export GT_DEBUG_DIR=/tmp/gt_precheck
export GT_REBUILD_L1=1 GT_REBUILD_L3=1 GT_REBUILD_L3B=1 GT_REBUILD_L5=1
export GT_LAYER_EVENTS=1 GT_STRUCTURED_EVENTS=1 GT_STRUCTURAL_NEXT_ACTION=1
export GT_L3B_PRIMARY_EDGE=1 GT_L5_STRUCTURAL_UNVERIFIED=1 GT_L5_GOKU_EVENTS=1
export GT_DEEP_LAYER_GROUNDED_METRICS=1 GT_REGISTER_TOOLS=1
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY}"
export PYTHONPATH="$(pwd)/src:$(pwd)/scripts/swebench:/opt/OpenHands"

rm -rf /tmp/gt_precheck /tmp/precheck_results
mkdir -p /tmp/gt_precheck /tmp/precheck_results

# Write config with thinking DISABLED (BUG-13)
cat > /tmp/config.toml << 'TOML'
[core]
max_iterations = 30
default_agent = "CodeActAgent"

[llm.eval]
model = "deepseek/deepseek-v4-flash"
api_key_env = "DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com"
temperature = 1.0
top_p = 1.0
max_output_tokens = 65536
drop_params = true
num_retries = 10
timeout = 300

[sandbox]
runtime_container_image = "ghcr.io/all-hands-ai/runtime:0.54-nikolaik"
TOML
cp /tmp/config.toml /opt/OpenHands/evaluation/benchmarks/swe_bench/config.toml 2>/dev/null || true

echo ""
echo "=== RUNNING 1-TASK: beets-5495 ==="
echo ""

python scripts/swebench/oh_gt_full_wrapper.py \
    --instance-ids 'beetbox__beets-5495' \
    -l eval -i 30 --eval-num-workers 1 \
    --eval-output-dir /tmp/precheck_results \
    --dataset 'SWE-bench-Live/SWE-bench-Live' --split lite \
    2>&1 | tee /tmp/gt_precheck/full_run.log

echo ""
echo "=========================================="
echo "  PRECHECK RESULTS"
echo "=========================================="

LOG=/tmp/gt_precheck/full_run.log
PASS=0
FAIL=0

# CHECK 1: BUG-1 — has_evidence gate recognizes behavioral contract
if grep -q "BEHAVIORAL CONTRACT:" "$LOG" 2>/dev/null; then
    echo "CHECK 1 (BUG-1 gate): PASS — BEHAVIORAL CONTRACT in output"
    PASS=$((PASS+1))
else
    echo "CHECK 1 (BUG-1 gate): FAIL — no BEHAVIORAL CONTRACT found"
    FAIL=$((FAIL+1))
fi

# CHECK 2: BUG-2 — semantic check fires
if grep -q "mech=semantic_check" "$LOG" 2>/dev/null; then
    echo "CHECK 2 (BUG-2 semantic): PASS — semantic_check trace found"
    PASS=$((PASS+1))
    if grep -q "mech=semantic_check.*action=emit.*visible=True" "$LOG" 2>/dev/null; then
        echo "  → agent_visible=true CONFIRMED"
    fi
else
    echo "CHECK 2 (BUG-2 semantic): FAIL — no semantic_check trace"
    FAIL=$((FAIL+1))
fi

# CHECK 3: BUG-4 — structured trace fields present
if grep -q "\[GT_TRACE\]" "$LOG" 2>/dev/null; then
    TRACE_COUNT=$(grep -c "\[GT_TRACE\]" "$LOG")
    echo "CHECK 3 (BUG-4 traces): PASS — $TRACE_COUNT [GT_TRACE] lines"
    PASS=$((PASS+1))
else
    echo "CHECK 3 (BUG-4 traces): FAIL — no [GT_TRACE] lines"
    FAIL=$((FAIL+1))
fi

# CHECK 4: BUG-5 — graph.db downloaded to host
if grep -q "B-7 copy_from: OK" "$LOG" 2>/dev/null; then
    echo "CHECK 4 (BUG-5 B-7): PASS — copy_from succeeded"
    PASS=$((PASS+1))
elif grep -q "B-7 fallback: OK" "$LOG" 2>/dev/null; then
    echo "CHECK 4 (BUG-5 B-7): PASS — fallback succeeded"
    PASS=$((PASS+1))
elif grep -q "B-7 pre-fetch: graph.db downloaded" "$LOG" 2>/dev/null; then
    echo "CHECK 4 (BUG-5 B-7): PASS — pre-fetch succeeded"
    PASS=$((PASS+1))
else
    echo "CHECK 4 (BUG-5 B-7): FAIL — graph.db not on host"
    grep "B-7\|copy_from\|download" "$LOG" 2>/dev/null | head -5
    FAIL=$((FAIL+1))
fi

# CHECK 5: BUG-9 — adaptive L5 threshold logged
if grep -q "mech=adaptive_L5.*threshold=" "$LOG" 2>/dev/null; then
    THRESH=$(grep "mech=adaptive_L5.*threshold=" "$LOG" | head -1)
    echo "CHECK 5 (BUG-9 L5): PASS — $THRESH"
    PASS=$((PASS+1))
else
    echo "CHECK 5 (BUG-9 L5): FAIL — no threshold log"
    FAIL=$((FAIL+1))
fi

# CHECK 6: No UNKNOWN_ERROR suppression reasons
if grep -q "reason=UNKNOWN_ERROR" "$LOG" 2>/dev/null; then
    echo "CHECK 6 (no unknown): FAIL — UNKNOWN_ERROR found"
    FAIL=$((FAIL+1))
else
    echo "CHECK 6 (no unknown): PASS — no UNKNOWN_ERROR"
    PASS=$((PASS+1))
fi

# CHECK 7: L3 post-edit evidence delivered
if grep -q "GT_DELIVERY.*L3 LIVE post_edit" "$LOG" 2>/dev/null; then
    DELIVERIES=$(grep -c "GT_DELIVERY.*L3 LIVE post_edit" "$LOG")
    echo "CHECK 7 (L3 delivery): PASS — $DELIVERIES deliveries"
    PASS=$((PASS+1))
else
    echo "CHECK 7 (L3 delivery): FAIL — no L3 deliveries"
    FAIL=$((FAIL+1))
fi

# CHECK 8: L1 brief injected
if grep -q "L1 brief injected\|GT_DELIVERY.*L1" "$LOG" 2>/dev/null; then
    echo "CHECK 8 (L1 brief): PASS"
    PASS=$((PASS+1))
else
    echo "CHECK 8 (L1 brief): FAIL"
    FAIL=$((FAIL+1))
fi

echo ""
echo "=========================================="
echo "  SCORE: $PASS pass / $FAIL fail / $((PASS+FAIL)) total"
echo "=========================================="

if [ "$FAIL" -eq 0 ]; then
    echo "ALL CHECKS PASSED — ready for 5-task proof run"
else
    echo "BLOCKED — fix $FAIL failing checks before scaling"
    echo ""
    echo "Diagnostic lines:"
    grep -i "error\|fail\|FAIL\|B-7\|copy_from\|GATE_MISMATCH\|NO_GRAPH_DB\|SNIPPET_ERROR" "$LOG" | head -20
fi
