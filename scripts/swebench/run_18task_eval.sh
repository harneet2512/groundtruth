#!/bin/bash
set -e

RESULTS_DIR="/root/results/v7_smoke_20260327_100453"
OH_DIR="/root/oh-benchmarks"

BASELINE_OUTPUT="$RESULTS_DIR/baseline/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_baseline/output.jsonl"
GT_OUTPUT="$RESULTS_DIR/gt_v7/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_gt/output.jsonl"

echo "=== Evaluating 18-task baseline ==="
cd "$OH_DIR"
.venv/bin/python -m benchmarks.swebench.eval_infer \
    "$BASELINE_OUTPUT" \
    --run-id qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_baseline \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --no-modal \
    --workers 4 \
    2>&1 | tail -30

echo ""
echo "=== Evaluating 18-task GT v7 ==="
.venv/bin/python -m benchmarks.swebench.eval_infer \
    "$GT_OUTPUT" \
    --run-id qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_gt \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --no-modal \
    --workers 4 \
    2>&1 | tail -30

echo ""
echo "=== COMPARISON ==="
echo "Baseline report:"
cat "$RESULTS_DIR/baseline/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_baseline/output.report.json" 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps({"resolved": len(d.get("resolved_ids",[])), "unresolved": len(d.get("unresolved_ids",[])), "error": len(d.get("error_ids",[])), "resolved_ids": d.get("resolved_ids",[])}, indent=2))'

echo ""
echo "GT v7 report:"
cat "$RESULTS_DIR/gt_v7/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_gt/output.report.json" 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps({"resolved": len(d.get("resolved_ids",[])), "unresolved": len(d.get("unresolved_ids",[])), "error": len(d.get("error_ids",[])), "resolved_ids": d.get("resolved_ids",[])}, indent=2))'

echo ""
echo "=== DONE ==="
