#!/bin/bash
# Wait for 18-task run to finish, then launch 50-task A/B
set -e
export PATH="/root/.local/bin:$PATH"

LOG_18="/home/Lenovo/results/v8_precompute_18t.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Waiting for 18-task run to complete..."
while true; do
    if grep -q "GT v7 Smoke Test Complete\|Smoke Test Complete" "$LOG_18" 2>/dev/null; then
        echo "18-task run finished!"
        break
    fi
    sleep 60
done

# Run eval on 18-task results
RESULTS_18=$(ls -td /root/results/v7_smoke_* 2>/dev/null | head -1)
echo "18-task results at: $RESULTS_18"

BASELINE_18=$(find "$RESULTS_18" -name "output.jsonl" -path "*/baseline/*" | head -1)
GT_18=$(find "$RESULTS_18" -name "output.jsonl" -path "*/gt_v7/*" | head -1)

if [ -n "$BASELINE_18" ] && [ -n "$GT_18" ]; then
    echo "=== Evaluating 18-task baseline ==="
    cd /root/oh-benchmarks
    .venv/bin/python -m benchmarks.swebench.eval_infer "$BASELINE_18" \
        --run-id v8_precompute_baseline --dataset princeton-nlp/SWE-bench_Lite \
        --split test --no-modal --workers 4 2>&1 | tail -5

    echo "=== Evaluating 18-task GT v8 ==="
    .venv/bin/python -m benchmarks.swebench.eval_infer "$GT_18" \
        --run-id v8_precompute_gt --dataset princeton-nlp/SWE-bench_Lite \
        --split test --no-modal --workers 4 2>&1 | tail -5

    # Show results
    echo "=== 18-task results ==="
    python3 "$SCRIPT_DIR/show_results.py" 2>/dev/null || true
fi

# Now launch 50-task run
echo ""
echo "========================================="
echo "  Starting 50-task A/B run"
echo "  $(date -u) UTC"
echo "========================================="

cd /home/Lenovo/groundtruth
# Copy prompt template
cp scripts/swebench/oh_prompt_gt_v7.j2 /root/oh-benchmarks/benchmarks/swebench/prompts/gt_hook_v7.j2

bash scripts/swebench/oh_run_v7_smoke.sh \
    --select /tmp/runnable_50_real.txt \
    --workers 4 \
    2>&1 | tee /home/Lenovo/results/v8_50task_final.log

echo ""
echo "========================================="
echo "  50-task run complete"
echo "  $(date -u) UTC"
echo "========================================="

# Run eval on 50-task results
RESULTS_50=$(ls -td /root/results/v7_smoke_* 2>/dev/null | head -1)
BASELINE_50=$(find "$RESULTS_50" -name "output.jsonl" -path "*/baseline/*" | head -1)
GT_50=$(find "$RESULTS_50" -name "output.jsonl" -path "*/gt_v7/*" | head -1)

if [ -n "$BASELINE_50" ] && [ -n "$GT_50" ]; then
    echo "=== Evaluating 50-task baseline ==="
    cd /root/oh-benchmarks
    .venv/bin/python -m benchmarks.swebench.eval_infer "$BASELINE_50" \
        --run-id v8_50task_baseline --dataset princeton-nlp/SWE-bench_Lite \
        --split test --no-modal --workers 4 2>&1 | tail -5

    echo "=== Evaluating 50-task GT v8 ==="
    .venv/bin/python -m benchmarks.swebench.eval_infer "$GT_50" \
        --run-id v8_50task_gt --dataset princeton-nlp/SWE-bench_Lite \
        --split test --no-modal --workers 4 2>&1 | tail -5
fi

echo "=== ALL DONE ==="
