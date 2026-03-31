#!/bin/bash
set -euo pipefail
source ~/gt-venv/bin/activate
source ~/gt-env.sh 2>/dev/null || true

REPO_DIR=$HOME/groundtruth
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT=$HOME/results/v13_verified_smoke_${TIMESTAMP}
MODEL="${MODEL:-openai/gemini-flash}"
WORKERS=2

mkdir -p "$OUTPUT_ROOT"

echo "================================================="
echo "  v13 VERIFIED SMOKE TEST (5 tasks, baseline + GT)"
echo "  Model: $MODEL"
echo "  $(date -u) UTC"
echo "  Output: $OUTPUT_ROOT"
echo "================================================="

cd "$REPO_DIR"

# ── Start LiteLLM proxy ──────────────────────────────────────────────────
pkill -f 'litellm.*4000' 2>/dev/null || true
sleep 2

litellm --config scripts/swebench/litellm_verified.yaml --port 4000 > /tmp/litellm.log 2>&1 &
LITELLM_PID=$!
for i in $(seq 1 30); do curl -s http://localhost:4000/health >/dev/null 2>&1 && break; sleep 2; done
curl -s http://localhost:4000/health >/dev/null 2>&1 && echo "Proxy: OK (PID $LITELLM_PID)" || { echo "Proxy: FAIL — check /tmp/litellm.log"; exit 1; }

# ── Phase A: Baseline (5 tasks) ──────────────────────────────────────────
echo ""
echo "--- Phase A: BASELINE (5 tasks) ---"
python3 benchmarks/swebench/run_v7_baseline.py \
    -c benchmarks/swebench/mini_swebench_verified_baseline.yaml \
    --model "$MODEL" \
    --subset princeton-nlp/SWE-bench_Verified --split test \
    --slice 0:5 \
    -w $WORKERS \
    -o "$OUTPUT_ROOT/baseline" \
    2>&1 | tee "$OUTPUT_ROOT/baseline.log"

BL_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_ROOT/baseline/preds.json'))))" 2>/dev/null || echo 0)
echo "Baseline complete: $BL_COUNT/5 predictions"

# ── Phase B: GT v13 (5 tasks) ────────────────────────────────────────────
echo ""
echo "--- Phase B: GT v13 (5 tasks) ---"
python3 benchmarks/swebench/run_mini_gt_hooked.py \
    -c benchmarks/swebench/mini_swebench_verified_gt_v13.yaml \
    --model "$MODEL" \
    --subset princeton-nlp/SWE-bench_Verified --split test \
    --slice 0:5 \
    -w $WORKERS \
    -o "$OUTPUT_ROOT/gt_v13" \
    2>&1 | tee "$OUTPUT_ROOT/gt_v13.log"

GT_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_ROOT/gt_v13/preds.json'))))" 2>/dev/null || echo 0)
echo "GT complete: $GT_COUNT/5 predictions"

# ── Phase C: Utilization check ────────────────────────────────────────────
echo ""
echo "================================================="
echo "  SMOKE UTILIZATION CHECK"
echo "================================================="

# Count GT evidence events
EVIDENCE_FILES=$(ls "$OUTPUT_ROOT/gt_v13/gt_logs/"*.evidence.jsonl 2>/dev/null | wc -l || echo 0)
echo "Evidence log files: $EVIDENCE_FILES / 5"

# Check briefings in main log
BRIEFINGS=$(grep -c "v12 briefing for" "$OUTPUT_ROOT/gt_v13.log" 2>/dev/null || echo 0)
echo "Briefings shown: $BRIEFINGS / 5"

# Check indexer success
INDEXER_OK=$(grep -c "v11 Go indexer:" "$OUTPUT_ROOT/gt_v13.log" 2>/dev/null || echo 0)
echo "Indexer success: $INDEXER_OK / 5"

# Check for GT output in trajectories
GT_IN_TRAJ=0
for traj in "$OUTPUT_ROOT/gt_v13/"*/*.traj.json; do
    if [ -f "$traj" ] && grep -q "GT CODEBASE" "$traj" 2>/dev/null; then
        GT_IN_TRAJ=$((GT_IN_TRAJ + 1))
    fi
done
echo "Tasks with GT in trajectory: $GT_IN_TRAJ / 5"

# Check for name_match in evidence (MUST be zero)
NAME_MATCH=0
for ev in "$OUTPUT_ROOT/gt_v13/gt_logs/"*.evidence.jsonl; do
    if [ -f "$ev" ] && grep -q '"name_match"' "$ev" 2>/dev/null; then
        NAME_MATCH=$((NAME_MATCH + 1))
        echo "  WARNING: name_match found in $(basename $ev)"
    fi
done
echo "Tasks with name_match in evidence: $NAME_MATCH (MUST be 0)"

echo ""
echo "================================================="
echo "  SMOKE GATE DECISION"
echo "================================================="
PASS=true

if [ "$INDEXER_OK" -lt 3 ]; then
    echo "FAIL: Indexer success < 3/5"
    PASS=false
fi

if [ "$NAME_MATCH" -gt 0 ]; then
    echo "FAIL: name_match found in evidence output"
    PASS=false
fi

if [ "$BL_COUNT" -lt 4 ]; then
    echo "FAIL: Baseline predictions < 4/5"
    PASS=false
fi

if [ "$GT_COUNT" -lt 4 ]; then
    echo "FAIL: GT predictions < 4/5"
    PASS=false
fi

if $PASS; then
    echo "SMOKE PASSED — proceed to 500-task run"
else
    echo "SMOKE FAILED — investigate before proceeding"
fi

echo ""
echo "Results: $OUTPUT_ROOT"
echo "Done: $(date -u) UTC"

pkill -f 'litellm.*4000' 2>/dev/null || true
