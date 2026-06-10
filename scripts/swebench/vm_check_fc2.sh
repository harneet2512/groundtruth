#!/bin/bash
OUTDIR="/tmp/tier12_fc_1777276628"
echo "=== Step counts ==="
for arm in arm_B arm_C; do
    for f in "$OUTDIR/$arm"/astropy*/*.traj; do
        task=$(basename "$(dirname "$f")")
        steps=$(python3 -c "import json; print(len(json.load(open('$f'))['trajectory']))" 2>/dev/null || echo "?")
        patch=$(python3 -c "import json; t=json.load(open('$f')); p=t.get('info',{}).get('submission','') or ''; print('YES' if p.strip() else 'no')" 2>/dev/null || echo "?")
        echo "  $arm/$task: steps=$steps patch=$patch"
    done
done
