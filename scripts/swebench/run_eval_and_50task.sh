#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="/root/results/v7_smoke_20260327_075143"
OH_DIR="/root/oh-benchmarks"

BASELINE_OUTPUT="$RESULTS_DIR/baseline/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_baseline/output.jsonl"
GT_OUTPUT="$RESULTS_DIR/gt_v7/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_gt/output.jsonl"

echo "=== Evaluating baseline ==="
cd "$OH_DIR"
.venv/bin/python -m benchmarks.swebench.eval_infer \
    "$BASELINE_OUTPUT" \
    --run-id qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_baseline \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --no-modal \
    --workers 2 \
    2>&1 | tail -30

echo ""
echo "=== Evaluating GT v7 ==="
.venv/bin/python -m benchmarks.swebench.eval_infer \
    "$GT_OUTPUT" \
    --run-id qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_gt \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --no-modal \
    --workers 2 \
    2>&1 | tail -30

echo ""
echo "=== SMOKE TEST EVAL DONE ==="

# Pull images for 50-task run using OH venv (has datasets module)
echo ""
echo "=== Pulling images for 50-task run ==="
cd "$OH_DIR"
.venv/bin/python "$SCRIPT_DIR/pull_50_images.py"

echo ""
echo "=== Starting 50-task A/B run ==="
cd /home/Lenovo/groundtruth
nohup bash scripts/swebench/oh_run_v7_smoke.sh --select /tmp/runnable_50_instances.txt --workers 4 > /home/Lenovo/results/v7_50task_run.log 2>&1 &
echo "50-task PID=$!"
echo "Log: /home/Lenovo/results/v7_50task_run.log"
