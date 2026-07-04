#!/usr/bin/env bash
set -euo pipefail

OUT_ROOT="/home/ubuntu/results/oh_gt_full_parallel_$(date +%s)"
mkdir -p "$OUT_ROOT"

IDS="$(python3 -c 'import json; d=json.load(open("/home/ubuntu/Groundtruth/benchmarks/live_lite_300_ids.json", encoding="utf-8")); print(",".join(d["instance_ids"][:10]))')"

cd /home/ubuntu/OpenHands-0.54.0

nohup env \
  GT_INDEX_BINARY=/home/ubuntu/Groundtruth/tools/sweagent/gt_edit/bin/gt-index \
  PYTHONPATH=/home/ubuntu/Groundtruth/src:/home/ubuntu/Groundtruth \
  /home/ubuntu/OpenHands-0.54.0/.venv/bin/python \
  /home/ubuntu/Groundtruth/scripts/swebench/oh_gt_full_wrapper.py \
  --instance-ids "$IDS" \
  --llm-config qwen3 \
  --dataset SWE-bench-Live/SWE-bench-Live \
  --split lite \
  --max-iterations 100 \
  --eval-num-workers 10 \
  --eval-output-dir "$OUT_ROOT" \
  >"$OUT_ROOT/run.log" 2>&1 &

echo "OUT_ROOT=$OUT_ROOT"
echo "PID=$!"
