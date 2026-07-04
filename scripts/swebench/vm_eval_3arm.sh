#!/bin/bash
set -euo pipefail
source ~/sweagent-env/bin/activate
OUTDIR="/tmp/tier12_1777271983"
cd "$OUTDIR"

echo "=== Step counts ==="
for arm in arm_A_baseline arm_B_tier1 arm_C_tier12; do
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
for arm in ['arm_A_baseline', 'arm_B_tier1', 'arm_C_tier12']:
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
    out = f'{arm}_preds.json'
    with open(out, 'w') as f:
        json.dump(preds, f, indent=2)
    print(f'  {arm}: {len(preds)} predictions')
    print()
PYEOF

for arm in arm_A_baseline arm_B_tier1 arm_C_tier12; do
    tag=$(echo "$arm" | sed 's/arm_//')
    echo "=== Evaluating $arm ==="
    python3 -m swebench.harness.run_evaluation \
        --predictions_path "$OUTDIR/${arm}_preds.json" \
        --run_id "$tag" \
        --max_workers 2 \
        --dataset princeton-nlp/SWE-bench_Verified 2>&1 | tail -12
    echo ""
done

echo "=== Resolved IDs ==="
python3 << 'PYEOF2'
import json, os
for arm, tag in [('arm_A_baseline','A_baseline'), ('arm_B_tier1','B_tier1'), ('arm_C_tier12','C_tier12')]:
    report = f'{arm}.{tag}.json'
    if os.path.exists(report):
        d = json.load(open(report))
        print(f'{arm}: resolved={d.get("resolved_ids", [])}')
    else:
        print(f'{arm}: no report')
PYEOF2
