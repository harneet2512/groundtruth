#!/bin/bash
set -euo pipefail
source ~/gt-venv/bin/activate

echo "=== GT EVAL ==="
echo "Started: $(date -u) UTC"

python3 -m swebench.harness.run_evaluation \
    --predictions_path /home/Lenovo/results/v13_verified_500_20260329_233605/gt_v13/preds.json \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --max_workers 16 \
    --run_id v13_flash_gt

echo "=== GT EVAL DONE ==="
echo "Finished: $(date -u) UTC"
