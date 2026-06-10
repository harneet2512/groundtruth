#!/bin/bash
set -euo pipefail
source ~/sweagent-env/bin/activate

# Find the run directories
RUN_A="/tmp/qwen_fc_ablation/run_1777313070/runs/A"
RUN_DE_BASE=$(ls -d /tmp/qwen_fc_ablation/run_*/runs/D 2>/dev/null | head -1 | sed 's|/runs/D||')

echo "=== Collecting predictions for all arms ==="
for arm_path in "$RUN_A" \
    "/tmp/qwen_fc_ablation/run_1777313070/runs/B" \
    "/tmp/qwen_fc_ablation/run_1777313070/runs/C" \
    "$RUN_DE_BASE/runs/D" \
    "$RUN_DE_BASE/runs/E"; do

    arm=$(basename "$arm_path")
    if [ ! -d "$arm_path" ]; then
        echo "SKIP: $arm_path not found"
        continue
    fi

    echo "--- $arm ---"
    python3 << PYEOF
import json, glob, os
arm_dir = "$arm_path"
arm = "$arm"
preds = []
for traj_file in sorted(glob.glob(f"{arm_dir}/astropy*/*.traj")):
    with open(traj_file) as f:
        traj = json.load(f)
    iid = os.path.basename(os.path.dirname(traj_file))
    info = traj.get("info", {})
    patch = info.get("submission", "") or ""
    steps = len(traj.get("trajectory", []))
    preds.append({"instance_id": iid, "model_name_or_path": arm, "model_patch": patch})
    print(f"  {arm}/{iid}: steps={steps} patch={'YES' if patch.strip() else 'no'}")
with open(f"/tmp/preds_{arm}.json", "w") as f:
    json.dump(preds, f, indent=2)
print(f"  {arm}: {len(preds)} predictions -> /tmp/preds_{arm}.json")
PYEOF
done

echo ""
echo "=== Evaluating all arms ==="
for arm in A B C D E; do
    pred="/tmp/preds_${arm}.json"
    if [ ! -f "$pred" ]; then
        echo "SKIP: $pred not found"
        continue
    fi
    echo "--- Evaluating $arm ---"
    python3 -m swebench.harness.run_evaluation \
        --predictions_path "$pred" \
        --run_id "ablation_${arm}" --max_workers 2 \
        --dataset princeton-nlp/SWE-bench_Verified 2>&1 | tail -12
    echo ""
done

echo "=== RESOLVED IDS ==="
cd /tmp
for arm in A B C D E; do
    f=$(ls preds_${arm}.ablation_${arm}.json 2>/dev/null || ls ablation_${arm}*.json 2>/dev/null | head -1)
    if [ -n "$f" ] && [ -f "$f" ]; then
        python3 -c "import json; d=json.load(open('$f')); print('$arm:', d.get('resolved_ids',[]))"
    else
        # Try finding in the working directory
        for candidate in $(find /tmp -maxdepth 1 -name "*ablation_${arm}*json" 2>/dev/null); do
            python3 -c "import json; d=json.load(open('$candidate')); print('$arm:', d.get('resolved_ids',[]))" 2>/dev/null && break
        done
    fi
done
