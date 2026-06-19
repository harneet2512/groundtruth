#!/bin/bash
set -euo pipefail
source ~/gt-env.sh
source ~/gt-venv/bin/activate
cd ~/groundtruth

OLD=~/results/v13_verified_500_20260329_233605

echo "=== GT REDO FAILURES ==="
echo "Started: $(date -u) UTC"

# Remove empty predictions from old GT preds.json
python3 - "$OLD/gt_v13/preds.json" << 'PYEOF'
import json, sys
path = sys.argv[1]
preds = json.load(open(path))
orig = len(preds)
clean = {}
for k, v in preds.items():
    if isinstance(v, str) and v.strip():
        clean[k] = v
    elif isinstance(v, dict) and v.get("model_patch", "").strip():
        clean[k] = v
removed = orig - len(clean)
json.dump(clean, open(path, "w"), indent=2)
print(f"Kept {len(clean)}/{orig} predictions, removed {removed} empties")
PYEOF

echo ""
echo "--- Running GT v13 redo (8 workers) ---"
python3 benchmarks/swebench/run_mini_gt_hooked.py \
    -c benchmarks/swebench/mini_swebench_verified_gt_v13.yaml \
    --model openai/gemini-flash \
    --subset princeton-nlp/SWE-bench_Verified --split test \
    -w 8 --redo-existing \
    -o "$OLD/gt_v13" \
    2>&1 | tee ~/redo_gt.log

echo ""
echo "=== GT REDO COMPLETE ==="
echo "Finished: $(date -u) UTC"
