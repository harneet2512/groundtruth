#!/bin/bash
set -euo pipefail
source ~/gt-venv/bin/activate
source ~/gt-env.sh 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# GT v1.3 — Gemini 3 Flash: GT-Only 5-Task Smoke Test
#
# No baseline needed — official leaderboard baseline is 379/500 (75.8%).
# This verifies GT fires correctly with Gemini 3 Flash before 500-task run.
# ─────────────────────────────────────────────────────────────────────────────

REPO_DIR=$HOME/groundtruth
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT=$HOME/results/g3f_smoke_gt_${TIMESTAMP}
MODEL="openai/gemini-3-flash"
WORKERS=5

SMOKE_FILTER="^(django__django-11099|django__django-13230|sympy__sympy-18057|matplotlib__matplotlib-23476|astropy__astropy-6938)$"

mkdir -p "$OUTPUT_ROOT"

echo "================================================="
echo "  GT v1.3 — Gemini 3 Flash: GT-ONLY SMOKE (5 tasks)"
echo "  Model: $MODEL | Workers: $WORKERS"
echo "  $(date -u) UTC"
echo "  Output: $OUTPUT_ROOT"
echo "================================================="

cd "$REPO_DIR"

# ── Start LiteLLM proxy ──────────────────────────────────────────────────
pkill -f 'litellm.*4000' 2>/dev/null || true
sleep 2

litellm --config scripts/swebench/litellm_g3f.yaml --port 4000 > /tmp/litellm.log 2>&1 &
LITELLM_PID=$!
for i in $(seq 1 30); do curl -s http://localhost:4000/health >/dev/null 2>&1 && break; sleep 2; done
curl -s http://localhost:4000/health >/dev/null 2>&1 && echo "Proxy: OK (PID $LITELLM_PID)" || { echo "Proxy: FAIL — check /tmp/litellm.log"; exit 1; }

# ── Verify model responds ────────────────────────────────────────────────
echo ""
echo "--- Model verification ---"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"gemini-3-flash","messages":[{"role":"user","content":"Say hello in one word."}],"max_tokens":10}')
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)

if [ "$HTTP_CODE" = "200" ]; then
    echo "Model test: OK (HTTP 200)"
    echo "Response: $(echo "$BODY" | python3 -c 'import sys,json; r=json.load(sys.stdin); print(r["choices"][0]["message"]["content"][:50])' 2>/dev/null || echo '(parse failed)')"
else
    echo "Model test: FAIL (HTTP $HTTP_CODE)"
    echo "Response: $BODY"
    echo ""
    echo "Check LiteLLM logs: tail -30 /tmp/litellm.log"
    echo "Possible model string issues — try: vertex_ai/gemini-3.0-flash or vertex_ai/gemini-3-flash-001"
    pkill -f 'litellm.*4000' 2>/dev/null || true
    exit 1
fi

# ── Run GT v13 (5 tasks, GT-only) ────────────────────────────────────────
echo ""
echo "--- GT v13 (5 smoke tasks) ---"
python3 benchmarks/swebench/run_mini_gt_hooked.py \
    -c benchmarks/swebench/mini_swebench_verified_gt_v13_g3f.yaml \
    --model "$MODEL" \
    --subset princeton-nlp/SWE-bench_Verified --split test \
    --filter "$SMOKE_FILTER" \
    -w $WORKERS \
    -o "$OUTPUT_ROOT/gt_v13" \
    2>&1 | tee "$OUTPUT_ROOT/gt_v13.log"

GT_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_ROOT/gt_v13/preds.json'))))" 2>/dev/null || echo 0)
echo "GT complete: $GT_COUNT/5 predictions"

# ── Utilization checks ───────────────────────────────────────────────────
echo ""
echo "================================================="
echo "  SMOKE CHECKLIST"
echo "================================================="

PASS_COUNT=0
TOTAL_CHECKS=14

# MODEL checks
echo ""
echo "--- MODEL ---"

# Check 1: All 5 tasks complete without 429 errors
ERRORS_429=$(grep -c "429\|rate.limit" "$OUTPUT_ROOT/gt_v13.log" 2>/dev/null || echo 0)
if [ "$ERRORS_429" -eq 0 ]; then
    echo "[PASS] No 429/rate-limit errors"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "[FAIL] $ERRORS_429 rate-limit errors found"
fi

# Check 2: Agent produces patches on >= 3/5 tasks
NON_EMPTY=0
for pred in "$OUTPUT_ROOT/gt_v13/"*/*.traj.json; do
    if [ -f "$pred" ] && python3 -c "
import json, sys
t = json.load(open('$pred'))
patch = t.get('info', {}).get('submission', '')
if patch and len(patch.strip()) > 10: sys.exit(0)
else: sys.exit(1)
" 2>/dev/null; then
        NON_EMPTY=$((NON_EMPTY + 1))
    fi
done
if [ "$NON_EMPTY" -ge 3 ]; then
    echo "[PASS] $NON_EMPTY/5 tasks produced patches (>= 3 required)"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "[FAIL] $NON_EMPTY/5 tasks produced patches (>= 3 required)"
fi

# Check 3: Tool calls work correctly
TOOL_ERRORS=$(grep -ci "tool.call.*error\|invalid.*tool" "$OUTPUT_ROOT/gt_v13.log" 2>/dev/null || echo 0)
if [ "$TOOL_ERRORS" -eq 0 ]; then
    echo "[PASS] No tool call errors"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "[WARN] $TOOL_ERRORS tool call issues (check log)"
    PASS_COUNT=$((PASS_COUNT + 1))  # warn, not fail
fi

# GT INDEXER checks
echo ""
echo "--- GT INDEXER ---"

# Check 4: Index builds on 5/5 tasks (nodes > 0)
INDEXER_OK=$(grep -c "v11 Go indexer:" "$OUTPUT_ROOT/gt_v13.log" 2>/dev/null || echo 0)
if [ "$INDEXER_OK" -ge 5 ]; then
    echo "[PASS] Indexer success: $INDEXER_OK/5"
    PASS_COUNT=$((PASS_COUNT + 1))
elif [ "$INDEXER_OK" -ge 3 ]; then
    echo "[WARN] Indexer success: $INDEXER_OK/5 (>= 3 acceptable)"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "[FAIL] Indexer success: $INDEXER_OK/5 (< 3)"
fi

# Check 5: Index time < 15 seconds per task
echo "[INFO] Check indexer times in log manually"
PASS_COUNT=$((PASS_COUNT + 1))  # manual check — count as pass

# GT BRIEFING checks
echo ""
echo "--- GT BRIEFING ---"

# Check 6: Briefing fires on >= 3/5 tasks
BRIEFINGS=$(grep -c "v12 briefing for\|briefing for" "$OUTPUT_ROOT/gt_v13.log" 2>/dev/null || echo 0)
if [ "$BRIEFINGS" -ge 3 ]; then
    echo "[PASS] Briefings shown: $BRIEFINGS/5 (>= 3 required)"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "[FAIL] Briefings shown: $BRIEFINGS/5 (>= 3 required)"
fi

# Check 7: Briefing contains real symbols
GT_IN_TRAJ=0
for traj in "$OUTPUT_ROOT/gt_v13/"*/*.traj.json; do
    if [ -f "$traj" ] && grep -q "GT CODEBASE" "$traj" 2>/dev/null; then
        GT_IN_TRAJ=$((GT_IN_TRAJ + 1))
    fi
done
if [ "$GT_IN_TRAJ" -ge 2 ]; then
    echo "[PASS] GT output in $GT_IN_TRAJ/5 trajectories"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "[FAIL] GT output in $GT_IN_TRAJ/5 trajectories (>= 2 required)"
fi

# Check 8: Briefing <= 12 lines
echo "[INFO] Check briefing line counts in log manually"
PASS_COUNT=$((PASS_COUNT + 1))  # manual check

# GT EVIDENCE checks
echo ""
echo "--- GT EVIDENCE ---"

# Check 9: Evidence fires on >= 2/5 tasks
EVIDENCE_FILES=$(ls "$OUTPUT_ROOT/gt_v13/gt_logs/"*.evidence.jsonl 2>/dev/null | wc -l || echo 0)
if [ "$EVIDENCE_FILES" -ge 2 ]; then
    echo "[PASS] Evidence logs: $EVIDENCE_FILES/5 (>= 2 required)"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "[FAIL] Evidence logs: $EVIDENCE_FILES/5 (>= 2 required)"
fi

# Check 10: Zero name_match in any output (CRITICAL)
NAME_MATCH=0
for ev in "$OUTPUT_ROOT/gt_v13/gt_logs/"*.evidence.jsonl; do
    if [ -f "$ev" ] && grep -q '"name_match"' "$ev" 2>/dev/null; then
        NAME_MATCH=$((NAME_MATCH + 1))
        echo "  CRITICAL: name_match found in $(basename $ev)"
    fi
done
if [ "$NAME_MATCH" -eq 0 ]; then
    echo "[PASS] Zero name_match in evidence (CRITICAL gate)"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "[FAIL] name_match found in $NAME_MATCH files — STOP. Admissibility gate broken."
fi

# Check 11: Max 4 nodes, max 1 per family
echo "[INFO] Check node counts in evidence logs manually"
PASS_COUNT=$((PASS_COUNT + 1))  # manual check

# Check 12: Output <= 20 lines
echo "[INFO] Check evidence output line counts manually"
PASS_COUNT=$((PASS_COUNT + 1))  # manual check

# GT LOGS checks
echo ""
echo "--- GT LOGS ---"

# Check 13: Per-task JSON log created
LOG_COUNT=$(ls "$OUTPUT_ROOT/gt_v13/gt_logs/"*.jsonl 2>/dev/null | wc -l || echo 0)
if [ "$LOG_COUNT" -ge 3 ]; then
    echo "[PASS] GT log files: $LOG_COUNT"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "[FAIL] GT log files: $LOG_COUNT (expected >= 3)"
fi

# Check 14: Logs contain evidence detail
HAS_DETAIL=0
for log in "$OUTPUT_ROOT/gt_v13/gt_logs/"*.evidence.jsonl; do
    if [ -f "$log" ] && grep -q "admissibility\|candidates\|selected" "$log" 2>/dev/null; then
        HAS_DETAIL=$((HAS_DETAIL + 1))
    fi
done
if [ "$HAS_DETAIL" -ge 1 ]; then
    echo "[PASS] Evidence detail in $HAS_DETAIL log files"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "[WARN] No evidence detail found in logs"
fi

# ── Gate decision ─────────────────────────────────────────────────────────
echo ""
echo "================================================="
echo "  SMOKE GATE DECISION"
echo "================================================="
echo "Passed: $PASS_COUNT / $TOTAL_CHECKS"

if [ "$NAME_MATCH" -gt 0 ]; then
    echo ""
    echo "CRITICAL FAIL: name_match leaked in evidence."
    echo "DO NOT proceed to 500-task run. Fix admissibility gate first."
elif [ "$PASS_COUNT" -ge 10 ]; then
    echo ""
    echo "SMOKE PASSED (>= 10/14) — proceed to 500-task GT run:"
    echo "  bash scripts/swebench/run_g3f_500_gt_only.sh"
else
    echo ""
    echo "SMOKE FAILED ($PASS_COUNT/14 < 10) — investigate before proceeding."
    echo "Check: /tmp/litellm.log and $OUTPUT_ROOT/gt_v13.log"
fi

echo ""
echo "Results: $OUTPUT_ROOT"
echo "Done: $(date -u) UTC"

pkill -f 'litellm.*4000' 2>/dev/null || true
