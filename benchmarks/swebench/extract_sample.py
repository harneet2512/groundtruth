#!/usr/bin/env python3
"""Extract GT evidence samples from v14 smoke test trajectories."""
import json, glob, sys

for t in sorted(glob.glob('/home/Lenovo/results/v14_smoke/*/*.traj.json'))[:3]:
    data = json.load(open(t))
    iid = t.split('/')[-2]
    print(f'=== {iid} ===')
    print(f'Top keys: {list(data.keys())}')

    raw = json.dumps(data)

    for marker in ['CONSTRAINTS FOR THIS FUNCTION', 'CODEBASE CONTEXT', 'REMINDER:']:
        idx = raw.find(marker)
        if idx >= 0:
            snippet = raw[max(0,idx-30):idx+300]
            decoded = snippet.replace('\\n', '\n').replace('\\t', '\t')
            print(f'\n--- {marker} ---')
            print(decoded[:300])
    print()
