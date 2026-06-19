#!/usr/bin/env bash
# Run SWE-bench Verified eval harness on a wave's preds.
#
# Usage (on VM):
#   bash eval_wave_preds.sh <wave_root> <run_id>
# Example:
#   bash eval_wave_preds.sh /tmp/wave1b_nolsp/repeat_wave1b wave1b_nolsp_eval
#
# Assumes sweagent-env contains swebench >= 4.0 and docker is running.
set -euo pipefail
WAVE_ROOT="${1:?wave root required}"
RUN_ID="${2:?run id required}"

source ~/sweagent-env/bin/activate

PREDS_JSONL=$(mktemp /tmp/wave_preds_XXXXXX.jsonl)

python3 - "$WAVE_ROOT" "$PREDS_JSONL" <<'PY'
import glob, json, sys
from pathlib import Path
root, out_path = sys.argv[1], sys.argv[2]
count = 0
with open(out_path, "w") as out:
    for p in sorted(glob.glob(f"{root}/**/preds.json", recursive=True)):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        for iid, rec in d.items():
            patch = rec.get("model_patch", "") or ""
            if not patch.strip():
                continue
            out.write(json.dumps({
                "instance_id": iid,
                "model_patch": patch,
                "model_name_or_path": "gt_wave",
            }) + "\n")
            count += 1
print(f"WROTE {count} predictions to {out_path}")
PY

python3 -m swebench.harness.run_evaluation \
  --predictions_path "$PREDS_JSONL" \
  --run_id "$RUN_ID" \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --split test \
  --max_workers 4 \
  2>&1 | tee "/tmp/${RUN_ID}_eval.log"

echo "EVAL_DONE run_id=$RUN_ID"
ls -la "/tmp/${RUN_ID}"* 2>&1 | head -5 || true
