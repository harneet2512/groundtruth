#!/bin/bash
set -euo pipefail
source ~/sweagent-env/bin/activate
OUTDIR="/tmp/ablation_repro_1777304304"
cd "$OUTDIR"

echo "=== Step counts ==="
for arm in arm_B arm_E; do
    for f in "$OUTDIR/$arm"/astropy*/*.traj; do
        task=$(basename "$(dirname "$f")")
        steps=$(python3 -c "import json; print(len(json.load(open('$f'))['trajectory']))" 2>/dev/null || echo "?")
        patch=$(python3 -c "import json; t=json.load(open('$f')); p=t.get('info',{}).get('submission','') or ''; print('YES' if p.strip() else 'no')" 2>/dev/null || echo "?")
        echo "  $arm/$task: steps=$steps patch=$patch"
    done
done

echo ""
echo "=== Collecting predictions ==="
python3 << 'PYEOF'
import json, glob, os
for arm in ['arm_B', 'arm_E']:
    preds = []
    for traj_file in sorted(glob.glob(f'{arm}/astropy*/*.traj')):
        with open(traj_file) as f:
            traj = json.load(f)
        instance_id = os.path.basename(os.path.dirname(traj_file))
        info = traj.get('info', {})
        patch = info.get('submission', '') or ''
        preds.append({'instance_id': instance_id, 'model_name_or_path': arm, 'model_patch': patch})
        has = 'YES' if patch.strip() else 'no'
        print(f'  {arm}/{instance_id}: patch={has}')
    with open(f'{arm}_preds.json', 'w') as f:
        json.dump(preds, f, indent=2)
    print(f'  {arm}: {len(preds)} predictions\n')
PYEOF

for arm in arm_B arm_E; do
    echo "=== Evaluating $arm ==="
    python3 -m swebench.harness.run_evaluation \
        --predictions_path "$OUTDIR/${arm}_preds.json" \
        --run_id "$arm" --max_workers 2 \
        --dataset princeton-nlp/SWE-bench_Verified 2>&1 | tail -12
    echo ""
done

echo "=== Resolved ==="
for arm in arm_B arm_E; do
    f="$arm.$arm.json"
    [ -f "$f" ] && python3 -c "import json; d=json.load(open('$f')); print('$arm:', d.get('resolved_ids',[]))" || echo "$arm: no report"
done
