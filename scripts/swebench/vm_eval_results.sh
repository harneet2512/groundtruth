#!/bin/bash
set -euo pipefail

OUTDIR="${1:-/tmp/consolidated_1777255826}"
source ~/sweagent-env/bin/activate

echo "=== Step counts ==="
for arm in arm_BL arm_GT; do
    for f in "$OUTDIR/$arm"/astropy*/*.traj; do
        task=$(basename "$(dirname "$f")")
        steps=$(python3 -c "import json; print(len(json.load(open('$f'))['trajectory']))" 2>/dev/null || echo "?")
        patch=$(python3 -c "import json; t=json.load(open('$f')); p=t.get('info',{}).get('submission','') or ''; print('YES' if p.strip() else 'no')" 2>/dev/null || echo "?")
        echo "  $arm/$task: steps=$steps patch=$patch"
    done
done

echo ""
echo "=== Collecting predictions ==="
cd "$OUTDIR"
python3 << 'PYEOF'
import json, glob, os
for arm in ['arm_BL', 'arm_GT']:
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
    print(f'  {arm}: {len(preds)} predictions -> {out}')
    print()
PYEOF

echo "=== Evaluating BL ==="
python3 -m swebench.harness.run_evaluation \
    --predictions_path "$OUTDIR/arm_BL_preds.json" \
    --run_id fc_BL \
    --max_workers 2 \
    --dataset princeton-nlp/SWE-bench_Verified 2>&1 | tail -15

echo ""
echo "=== Evaluating GT ==="
python3 -m swebench.harness.run_evaluation \
    --predictions_path "$OUTDIR/arm_GT_preds.json" \
    --run_id fc_GT \
    --max_workers 2 \
    --dataset princeton-nlp/SWE-bench_Verified 2>&1 | tail -15

echo ""
echo "=== Resolved IDs ==="
python3 << 'PYEOF2'
import json, os
for arm in ['arm_BL', 'arm_GT']:
    report = f'{arm}.fc_{"BL" if "BL" in arm else "GT"}.json'
    if os.path.exists(report):
        d = json.load(open(report))
        print(f'{arm}: resolved={d.get("resolved_instances", d.get("resolved_ids", []))}')
    else:
        print(f'{arm}: no report found')
PYEOF2
