#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Evaluating baseline ==="
cd /root/oh-benchmarks
.venv/bin/python -m benchmarks.swebench.eval_infer \
    --run-id qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_baseline \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --no-modal \
    --workers 2 \
    2>&1 | tail -20

echo ""
echo "=== Evaluating GT v7 ==="
.venv/bin/python -m benchmarks.swebench.eval_infer \
    --run-id qwen3-coder_sdk_62c2e7c_maxiter_50_N_v7_gt \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --no-modal \
    --workers 2 \
    2>&1 | tail -20

echo ""
echo "=== SMOKE TEST EVAL DONE ==="

# Pull images for 50-task run
echo ""
echo "=== Pulling images for 50-task run ==="
python3 "$SCRIPT_DIR/pull_50_images.py"

echo ""
echo "=== Starting 50-task A/B run ==="
cd /home/Lenovo/groundtruth
nohup bash scripts/swebench/oh_run_v7_smoke.sh --select /tmp/runnable_50_instances.txt --workers 4 > /home/Lenovo/results/v7_50task_run.log 2>&1 &
echo "50-task PID=$!"
echo "Log: /home/Lenovo/results/v7_50task_run.log"
