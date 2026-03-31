#!/bin/bash
set -euo pipefail
# ─────────────────────────────────────────────────────────────────────────────
# GT V2 Pull Architecture — 15-Task Smoke Test
#
# GCP Project: fit-parity-491905-t9 (crym)
# Model: Gemini 3 Flash (Vertex AI, $0)
# ─────────────────────────────────────────────────────────────────────────────

export GOOGLE_CLOUD_PROJECT="fit-parity-491905-t9"
export HOME="${HOME:-/home/Lenovo}"

REPO_DIR="${REPO_DIR:-$HOME/groundtruth}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT="$HOME/results/v2_pull_smoke_${TIMESTAMP}"
MODEL="openai/gemini-3-flash"
WORKERS=5

# 15 smoke tasks: 5 v1.5-lost + 5 v1.5-gained + 5 both-fail
# Set A (v1.5 LOST — expect GT stays silent, no regression):
TASK_A="django__django-16560,astropy__astropy-13977,django__django-11138,django__django-12273,django__django-13837"
# Set B (v1.5 GAINED — expect tools/hooks fire, retain gains):
TASK_B="django__django-13230,sympy__sympy-18057,django__django-11099,matplotlib__matplotlib-23476,astropy__astropy-6938"
# Set C (both-fail — opportunity, expect gt_locate helps):
TASK_C="django__django-14155,sympy__sympy-13146,scikit-learn__scikit-learn-13241,django__django-14999,sympy__sympy-20442"

ALL_TASKS="${TASK_A},${TASK_B},${TASK_C}"

mkdir -p "$OUTPUT_ROOT"

echo "================================================="
echo "  GT V2 PULL — SMOKE TEST (15 tasks)"
echo "  Project: fit-parity-491905-t9"
echo "  Model: $MODEL | Workers: $WORKERS"
echo "  $(date -u) UTC"
echo "  Output: $OUTPUT_ROOT"
echo "================================================="

cd "$REPO_DIR"

# ── Start LiteLLM proxy ──────────────────────────────────────────────────
pkill -f 'litellm.*4000' 2>/dev/null || true
sleep 2

litellm --config scripts/swebench/litellm_v2_pull.yaml --port 4000 > /tmp/litellm.log 2>&1 &
LITELLM_PID=$!
for i in $(seq 1 30); do curl -s http://localhost:4000/health >/dev/null 2>&1 && break; sleep 2; done
curl -s http://localhost:4000/health >/dev/null 2>&1 && echo "Proxy: OK (PID $LITELLM_PID)" || { echo "Proxy: FAIL"; cat /tmp/litellm.log; exit 1; }

# ── Verify model responds ────────────────────────────────────────────────
echo ""
echo "--- Model verification ---"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"gemini-3-flash","messages":[{"role":"user","content":"Say hello in one word."}],"max_tokens":10}')
HTTP_CODE=$(echo "$RESPONSE" | tail -1)

if [ "$HTTP_CODE" = "200" ]; then
    echo "Model test: OK (HTTP 200)"
else
    echo "Model test: FAIL (HTTP $HTTP_CODE)"
    pkill -f 'litellm.*4000' 2>/dev/null || true
    exit 1
fi

# ── Run v2 pull smoke test ───────────────────────────────────────────────
echo ""
echo "--- Running v2 pull (15 tasks) ---"

export OPENAI_API_KEY="dummy"
export OPENAI_BASE_URL="http://localhost:4000"
export MODEL_NAME_EXACT="$MODEL"

python3 -m benchmarks.swebench.runner \
    --mode groundtruth_v2_pull \
    --model "$MODEL" \
    --dataset princeton-nlp/SWE-bench_Verified \
    --split test \
    --instance-ids $ALL_TASKS \
    --workers $WORKERS \
    --max-turns 30 \
    --timeout 600 \
    --output-dir "$OUTPUT_ROOT" \
    --save-traces \
    2>&1 | tee "$OUTPUT_ROOT/smoke.log"

# ── Analyze ──────────────────────────────────────────────────────────────
echo ""
echo "--- Analysis ---"
python3 -m benchmarks.swebench.analyze_v2 \
    --predictions "$OUTPUT_ROOT/groundtruth_v2_pull/predictions.jsonl" \
    --log-dir "$OUTPUT_ROOT/groundtruth_v2_pull/gt_logs/" \
    2>&1 | tee "$OUTPUT_ROOT/analysis.txt"

# ── Smoke checklist ──────────────────────────────────────────────────────
echo ""
echo "================================================="
echo "  SMOKE CHECKLIST"
echo "================================================="

# Check 1: GT logs exist
LOG_COUNT=$(find "$OUTPUT_ROOT" -name "*.v2.jsonl" -o -name "*.hooks.jsonl" 2>/dev/null | wc -l)
echo "GT log files: $LOG_COUNT"

# Check 2: Token injection level
if [ -f "$OUTPUT_ROOT/analysis.txt" ]; then
    grep -i "avg per task" "$OUTPUT_ROOT/analysis.txt" || echo "(no token stats)"
    grep -i "tasks silent" "$OUTPUT_ROOT/analysis.txt" || echo "(no silence stats)"
fi

# Check 3: Patched count
PATCHED=$(grep -c '"model_patch"' "$OUTPUT_ROOT/groundtruth_v2_pull/predictions.jsonl" 2>/dev/null || echo 0)
echo "Tasks with patches: $PATCHED/15"

echo ""
echo "Results: $OUTPUT_ROOT"
echo "Done: $(date -u) UTC"

pkill -f 'litellm.*4000' 2>/dev/null || true
