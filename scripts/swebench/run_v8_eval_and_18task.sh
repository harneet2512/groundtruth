#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OH_DIR="/root/oh-benchmarks"

# ── Eval v8 7-task smoke test ──────────────────────────────────────
RESULTS_7T="/root/results/v7_smoke_20260327_151316"
GT_7T_OUTPUT="$RESULTS_7T/gt_v7/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_50_N_v8_gt/output.jsonl"

echo "=== Evaluating v8 7-task GT smoke test ==="
cd "$OH_DIR"
.venv/bin/python -m benchmarks.swebench.eval_infer \
    "$GT_7T_OUTPUT" \
    --run-id qwen3-coder_sdk_62c2e7c_maxiter_50_N_v8_gt \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --no-modal \
    --workers 2 \
    2>&1 | tail -10

echo ""
echo "=== V8 7-task report ==="
cat "$RESULTS_7T/gt_v7/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_50_N_v8_gt/output.report.json" 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps({"resolved": len(d.get("resolved_ids",[])), "resolved_ids": d.get("resolved_ids",[])}, indent=2))'

# ── Count understand calls in v8 logs ──────────────────────────────
echo ""
echo "=== V8 understand call analysis ==="
python3 -c "
import os, glob
log_dir = '$RESULTS_7T/gt_v7/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_50_N_v8_gt/logs'
total_understand = 0
total_verify = 0
for f in sorted(glob.glob(os.path.join(log_dir, 'instance_*.output.log'))):
    inst = os.path.basename(f).replace('instance_', '').replace('.output.log', '')
    with open(f) as fh:
        content = fh.read()
    u = content.count('gt_hook.py understand')
    v = content.count('gt_hook.py verify')
    total_understand += u
    total_verify += v
    if u > 0 or v > 0:
        print(f'  {inst}: {u} understand, {v} verify')
print(f'Total: {total_understand} understand, {total_verify} verify calls')
"

# ── Start 18-task v8 A/B run ──────────────────────────────────────
echo ""
echo "=== Starting 18-task v8 A/B run ==="
cd /home/Lenovo/groundtruth
nohup bash scripts/swebench/oh_run_v7_smoke.sh --select /tmp/runnable_50_instances.txt --workers 4 > /home/Lenovo/results/v8_18task_run.log 2>&1 &
echo "18-task PID=$!"
echo "Log: /home/Lenovo/results/v8_18task_run.log"
