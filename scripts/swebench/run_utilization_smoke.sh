#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <task_id_or_count> <output_dir>"
  exit 2
fi

TASK_SPEC="$1"
OUT_DIR="$2"
mkdir -p "$OUT_DIR"

echo "[cost] projected spend: variable by model and retries; keep per-task limit at configured cap"

if [[ "$TASK_SPEC" =~ ^[0-9]+$ ]]; then
  TASK_IDS="aws-cloudformation__cfn-lint-3890"
else
  TASK_IDS="$TASK_SPEC"
fi

python scripts/swebench/swe_agent_smoke_runner.py \
  --config config/gt_track4.yaml \
  --task-ids "$TASK_IDS" \
  --output-dir "$OUT_DIR" \
  --workers 1 \
  --per-instance-wallclock-cap-seconds 1800
