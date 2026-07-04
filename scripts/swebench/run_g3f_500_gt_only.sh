#!/bin/bash
set -euo pipefail
source ~/gt-venv/bin/activate
source ~/gt-env.sh 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# GT v1.3 — Gemini 3 Flash: Full 500-Task GT-Only Leaderboard Run
#
# Official baseline: Gemini 3 Flash + mini-swe-agent = 379/500 (75.8%)
# This run: Gemini 3 Flash + mini-swe-agent + GroundTruth v1.3
#
# 8 workers. Proven clean from v1.3 redo: zero 429 errors, zero failures.
# Do NOT increase workers. Do NOT experiment.
# ─────────────────────────────────────────────────────────────────────────────

REPO_DIR=$HOME/groundtruth
OUTPUT_ROOT=$HOME/results/v1.3_g3f_submission
MODEL="openai/gemini-3-flash"
WORKERS=8

mkdir -p "$OUTPUT_ROOT"

echo "================================================="
echo "  GT v1.3 — Gemini 3 Flash: 500-TASK GT-ONLY RUN"
echo "  Model: $MODEL | Workers: $WORKERS"
echo "  $(date -u) UTC"
echo "  Output: $OUTPUT_ROOT"
echo "================================================="

cd "$REPO_DIR"

# ── Pre-flight checks ────────────────────────────────────────────────────
echo ""
echo "--- Pre-flight ---"

# Check GCP project
CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null || echo "unknown")
if [ "$CURRENT_PROJECT" = "massive-house-346402" ]; then
    echo "GCP project: OK ($CURRENT_PROJECT)"
else
    echo "WARNING: GCP project is '$CURRENT_PROJECT' — expected 'massive-house-346402'"
    echo "Run: gcloud config set project massive-house-346402"
    exit 1
fi

# Check gt-index-static exists
if [ -f "$REPO_DIR/gt-index/gt-index-static" ]; then
    echo "Go indexer binary: OK"
else
    echo "WARNING: gt-index-static not found at $REPO_DIR/gt-index/gt-index-static"
    exit 1
fi

# ── Verify LiteLLM proxy (managed by systemd) ────────────────────────────
curl -s http://localhost:4000/health >/dev/null 2>&1 && echo "Proxy: OK" || { echo "Proxy: FAIL — run: systemctl --user restart litellm"; exit 1; }

# ── Quick model verify ───────────────────────────────────────────────────
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"gemini-3-flash","messages":[{"role":"user","content":"hello"}],"max_tokens":5}')
if [ "$HTTP_CODE" = "200" ]; then
    echo "Model verify: OK"
else
    echo "Model verify: FAIL (HTTP $HTTP_CODE)"
    exit 1
fi

# ── Start monitoring in background ───────────────────────────────────────
echo ""
echo "--- Starting monitor ---"
(
    while true; do
        DONE=$(find "$OUTPUT_ROOT/gt_v13/" -name "*.traj.json" 2>/dev/null | wc -l || echo 0)
        ERRORS=$(grep -c "429\|rate.limit" "$OUTPUT_ROOT/gt_v13.log" 2>/dev/null || echo 0)
        echo "$(date '+%H:%M:%S') — $DONE/500 complete, $ERRORS errors"
        sleep 60
    done
) > "$OUTPUT_ROOT/monitor.log" 2>&1 &
MONITOR_PID=$!
echo "Monitor PID: $MONITOR_PID (tail -f $OUTPUT_ROOT/monitor.log)"

# ── Full 500-task GT run ─────────────────────────────────────────────────
echo ""
echo "--- Starting GT v13 (500 tasks, $WORKERS workers) ---"
echo "Started: $(date -u) UTC"

python3 benchmarks/swebench/run_mini_gt_hooked.py \
    -c benchmarks/swebench/mini_swebench_verified_gt_v13_g3f.yaml \
    --model "$MODEL" \
    --subset princeton-nlp/SWE-bench_Verified --split test \
    -w "$WORKERS" \
    -o "$OUTPUT_ROOT/gt_v13" \
    2>&1 | tee "$OUTPUT_ROOT/gt_v13.log"

echo ""
echo "Finished: $(date -u) UTC"

# ── Kill monitor ──────────────────────────────────────────────────────────
kill $MONITOR_PID 2>/dev/null || true

# ── Post-run summary ─────────────────────────────────────────────────────
echo ""
echo "================================================="
echo "  COMPLETION SUMMARY"
echo "================================================="

# Count predictions
if [ -f "$OUTPUT_ROOT/gt_v13/preds.json" ]; then
    GT_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_ROOT/gt_v13/preds.json'))))" 2>/dev/null || echo 0)
    echo "GT predictions: $GT_COUNT / 500"
else
    echo "WARNING: preds.json not found"
fi

# Count non-empty patches
NON_EMPTY=$(python3 -c "
import json
preds = json.load(open('$OUTPUT_ROOT/gt_v13/preds.json'))
non_empty = sum(1 for p in preds.values() if p.get('model_patch', '').strip())
print(non_empty)
" 2>/dev/null || echo "?")
echo "Non-empty patches: $NON_EMPTY / 500"

# Check 429 errors
ERRORS_429=$(grep -c "429\|rate.limit" "$OUTPUT_ROOT/gt_v13.log" 2>/dev/null || echo 0)
echo "Rate-limit errors: $ERRORS_429"

# Check name_match (CRITICAL)
NAME_MATCH=0
for ev in "$OUTPUT_ROOT/gt_v13/gt_logs/"*.evidence.jsonl; do
    if [ -f "$ev" ] && grep -q '"name_match"' "$ev" 2>/dev/null; then
        NAME_MATCH=$((NAME_MATCH + 1))
    fi
done
echo "name_match in evidence: $NAME_MATCH (MUST be 0)"
if [ "$NAME_MATCH" -gt 0 ]; then
    echo "CRITICAL: name_match leaked. Review evidence logs before submitting."
fi

# GT delivery stats
INDEXER_OK=$(grep -c "v11 Go indexer:" "$OUTPUT_ROOT/gt_v13.log" 2>/dev/null || echo 0)
BRIEFINGS=$(grep -c "v12 briefing for\|briefing for" "$OUTPUT_ROOT/gt_v13.log" 2>/dev/null || echo 0)
EVIDENCE_FILES=$(ls "$OUTPUT_ROOT/gt_v13/gt_logs/"*.evidence.jsonl 2>/dev/null | wc -l || echo 0)
echo ""
echo "GT Delivery:"
echo "  Indexer success: $INDEXER_OK / 500"
echo "  Briefings shown: $BRIEFINGS / 500"
echo "  Evidence logs: $EVIDENCE_FILES / 500"

echo ""
echo "================================================="
echo "  NEXT STEPS"
echo "================================================="
echo "1. Run evaluation:"
echo "   python3 -m swebench.harness.run_evaluation \\"
echo "       --predictions_path $OUTPUT_ROOT/gt_v13/preds.json \\"
echo "       --swe_bench_tasks princeton-nlp/SWE-bench_Verified \\"
echo "       --max_workers 16 \\"
echo "       --run_id v1.3_g3f_gt"
echo ""
echo "2. Run analysis:"
echo "   python3 benchmarks/swebench/analyze_v13.py \\"
echo "       --gt-results <eval_results.json> \\"
echo "       --gt-logs $OUTPUT_ROOT/gt_v13/gt_logs/ \\"
echo "       --gt-trajs $OUTPUT_ROOT/gt_v13/"
echo ""
echo "3. Compare against official baseline: 379/500 (75.8%)"
echo ""
echo "Output: $OUTPUT_ROOT"
echo "Done: $(date -u) UTC"
