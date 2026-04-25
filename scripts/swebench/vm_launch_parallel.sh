#!/bin/bash
# Launch all 4 arms IN PARALLEL — 16 vCPUs, 1 worker per arm (4 containers total)
set -euo pipefail

export PATH="/home/Lenovo/sweagent-env/bin:/usr/local/go/bin:$PATH"
export OPENAI_API_KEY="sk-gt-local"
export OPENAI_API_BASE="http://172.17.0.1:4000/v1"
cd /tmp/SWE-agent

# Clean stale containers
docker kill $(docker ps -q) 2>/dev/null || true
docker rm $(docker ps -aq) 2>/dev/null || true
sleep 2

TIMESTAMP=$(date +%s)
OUTBASE="/tmp/Groundtruth_vnext/benchmarks/swebench/fast_diag/vnext_par_${TIMESTAMP}"
SUITE="astropy__astropy-12907|astropy__astropy-13033|astropy__astropy-13236|astropy__astropy-13398|astropy__astropy-13453|astropy__astropy-13579|astropy__astropy-13977|astropy__astropy-14096|astropy__astropy-14182|astropy__astropy-14309"
mkdir -p "$OUTBASE"
echo "$OUTBASE" > /tmp/vnext_par_outbase.txt

echo "=== PARALLEL 4-ARM RUN ===" | tee "$OUTBASE/run.log"
echo "Time: $(date -u)" | tee -a "$OUTBASE/run.log"
echo "Output: $OUTBASE" | tee -a "$OUTBASE/run.log"

# Launch all 4 arms in parallel, 1 worker each (4 containers total = 4 tasks at a time)
for arm_cfg in "B:canary_nogt_qwen_B.yaml" "C:canary_nogt_qwen_C.yaml" "F1:canary_gt_vnext_qwen.yaml" "F2:canary_gt_ds_lsp_qwen.yaml"; do
    arm="${arm_cfg%%:*}"
    cfg="${arm_cfg##*:}"
    echo "Launching ARM $arm..." | tee -a "$OUTBASE/run.log"
    sweagent run-batch \
        --config "config/$cfg" \
        --instances.type swe_bench --instances.subset verified --instances.split test \
        --instances.filter="$SUITE" \
        --output_dir "$OUTBASE/arm_$arm" --num_workers 1 \
        > "$OUTBASE/arm_${arm}.stdout.log" 2>&1 &
    echo "  PID=$! arm=$arm" | tee -a "$OUTBASE/run.log"
done

echo "All 4 arms launched in parallel. Waiting..." | tee -a "$OUTBASE/run.log"
wait
echo "=== ALL ARMS COMPLETE ===" | tee -a "$OUTBASE/run.log"
echo "Time: $(date -u)" | tee -a "$OUTBASE/run.log"

# Show exit statuses
for arm in B C F1 F2; do
    echo "--- arm_$arm ---" | tee -a "$OUTBASE/run.log"
    cat "$OUTBASE/arm_$arm/run_batch_exit_statuses.yaml" 2>/dev/null | tee -a "$OUTBASE/run.log"
done

# Run eval
echo "=== RUNNING EVAL ===" | tee -a "$OUTBASE/run.log"
for arm in B C F1 F2; do
    preds="$OUTBASE/arm_$arm/preds.json"
    if [ -f "$preds" ]; then
        echo "Evaluating arm_$arm..." | tee -a "$OUTBASE/run.log"
        python3 -m swebench.harness.run_evaluation \
            --dataset_name princeton-nlp/SWE-bench_Verified \
            --split test \
            --predictions_path "$preds" \
            --run_id "arm_$arm" \
            --max_workers 4 \
            --timeout 300 \
            2>&1 | tee -a "$OUTBASE/run.log" | tail -5
    fi
done

echo "=== DONE ===" | tee -a "$OUTBASE/run.log"
echo "DONE: $(date -u)" | tee -a "$OUTBASE/run.log"
