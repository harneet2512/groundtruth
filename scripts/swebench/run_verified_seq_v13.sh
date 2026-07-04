#!/bin/bash
set -euo pipefail
source ~/gt-env.sh
source ~/gt-venv/bin/activate
cd ~/groundtruth

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUT=~/results/v13_verified_seq_${TIMESTAMP}
mkdir -p "$OUT"

echo "=== SEQUENTIAL 500-TASK RUN (8 workers each) ==="
echo "Output: $OUT"
echo "Started: $(date -u) UTC"

echo ""
echo "--- Phase 1: BASELINE (500 tasks, 8 workers) ---"
python3 benchmarks/swebench/run_v7_baseline.py \
    -c benchmarks/swebench/mini_swebench_verified_baseline.yaml \
    --model openai/gemini-flash \
    --subset princeton-nlp/SWE-bench_Verified --split test \
    -w 8 \
    -o "$OUT/baseline" \
    2>&1 | tee "$OUT/baseline.log"

echo "Baseline done at $(date -u) UTC"

echo ""
echo "--- Phase 2: GT v13 (500 tasks, 8 workers) ---"
python3 benchmarks/swebench/run_mini_gt_hooked.py \
    -c benchmarks/swebench/mini_swebench_verified_gt_v13.yaml \
    --model openai/gemini-flash \
    --subset princeton-nlp/SWE-bench_Verified --split test \
    -w 8 \
    -o "$OUT/gt_v13" \
    2>&1 | tee "$OUT/gt_v13.log"

echo "GT done at $(date -u) UTC"

echo ""
echo "=== BOTH COMPLETE ==="
echo "Output: $OUT"
echo "Finished: $(date -u) UTC"
