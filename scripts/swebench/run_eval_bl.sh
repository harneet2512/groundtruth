#!/bin/bash
set -euo pipefail
source ~/gt-venv/bin/activate

echo "=== BASELINE EVAL ==="
echo "Started: $(date -u) UTC"

python3 -m swebench.harness.run_evaluation \
    --predictions_path /home/Lenovo/results/v13_verified_500_20260329_233605/baseline/preds.json \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --max_workers 16 \
    --run_id v13_flash_baseline

echo "=== BASELINE EVAL DONE ==="
echo "Finished: $(date -u) UTC"
