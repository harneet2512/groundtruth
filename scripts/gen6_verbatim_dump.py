#!/usr/bin/env python3
"""Dump VERBATIM GT content from every trajectory entry for all 6 gen eval tasks."""
import json, sys, io, glob, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

tasks = [
    ('weasyprint-2300', 'canary-v2_live-kozea__weasyprint-2300'),
    ('flexget-4306', 'canary-v2_live-flexget__flexget-4306'),
    ('pypsa-1172', 'canary-v2_live-pypsa__pypsa-1172'),
    ('cfn-lint-3875', 'canary-v2_live-aws-cloudformation__cfn-lint-3875'),
    ('sh-744', 'canary-v2_live-amoffat__sh-744'),
    ('arviz-2413', 'canary-v2_live-arviz-devs__arviz-2413'),
]

GT_MARKERS = [
    '<gt-task-brief>', '<gt-edit-target>', '[GT KEY CONTRACTS]',
    '[SIGNATURE]', '[BEHAVIORAL CONTRACT]', 'PRESERVE:',
    'Called by:', 'Calls into:', '[TEST]', '[COMPLETENESS]',
    '[PATTERN]', '[PEER]', '[SIMILAR]', '[OVERRIDE]', '[MISMATCH]',
    '[GT_AUTO]', '<gt-advisory', '<gt-scope', '[GT L5:', '[REVIEW]',
    '[CATCHES]', '[RAISES]', 'SEMANTIC WARNING:', '[GT] Callers of',
]

def extract_block(text, marker, max_len=200):
    idx = text.find(marker)
    if idx < 0:
        return ''
    block = text[idx:idx+max_len]
    lines = [l.strip() for l in block.split('\n') if l.strip()]
    return '\n'.join(lines[:4])

for short, task in tasks:
    files = glob.glob(f'runs/gen6_eval/{task}/**/output.jsonl', recursive=True)
    if not files:
        continue
    try:
        ev = json.load(open(f'runs/gen6_eval/{task}/eval_result.json'))
        k = list(ev.keys())[0]
        resolved = ev[k]['resolved']
    except:
        resolved = 'N/A'

    print('=' * 70)
    print(f'TASK: {short} | RESOLVED: {resolved}')
    print('=' * 70)

    with open(files[0], encoding='utf-8', errors='replace') as f:
        for line in f:
            obj = json.loads(line)
            history = obj.get('history', [])
            for i, entry in enumerate(history):
                text = ''
                for k2 in ('content', 'observation', 'text', 'message'):
                    v = entry.get(k2, '')
                    if isinstance(v, str):
                        text += v
                extras = entry.get('extras', {})
                if isinstance(extras, dict):
                    for k2 in ('content', 'observation', 'thought'):
                        v = extras.get(k2, '')
                        if isinstance(v, str):
                            text += v
                if not text:
                    continue

                found = [m for m in GT_MARKERS if m in text]
                if not found:
                    continue

                print(f'\n--- Entry [{i}] markers: {found} ---')

                for marker in found:
                    block = extract_block(text, marker, 250)
                    if not block:
                        continue
                    label = marker.strip('<>[]').replace('gt-', 'L1 ').replace('GT_AUTO', 'L4a AUTO')
                    # Indent each line
                    for bline in block.split('\n')[:4]:
                        print(f'  {bline[:130]}')

            break
    print()
