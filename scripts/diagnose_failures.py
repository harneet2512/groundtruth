#!/usr/bin/env python3
import json, glob, os, sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

failed_tasks = [
    'arviz-devs__arviz-2413',
    'aws-cloudformation__cfn-lint-3875',
    'pypsa__pypsa-1172',
    'flexget__flexget-4306',
    'delgan__loguru-1297',
    'delgan__loguru-1306',
    'cyclotruc__gitingest-115',
    'deepset-ai__haystack-8525',
]

for task in failed_tasks:
    files = glob.glob(f'runs/13task_smoke/canary-v2_live-{task}/**/output.jsonl', recursive=True)
    if not files:
        print(f'{task}: NO OUTPUT')
        continue

    with open(files[0], encoding='utf-8', errors='replace') as f:
        for line in f:
            obj = json.loads(line)
            hist = obj.get('history', [])

            gp = obj.get('git_patch', '')
            tr = obj.get('test_result', {})
            if isinstance(tr, dict):
                gp2 = tr.get('git_patch', '')
                if gp2 and not gp:
                    gp = gp2

            edit_count = 0
            action_count = 0
            for e in hist:
                text = ''
                for k in ('content', 'observation', 'text', 'message'):
                    v = e.get(k, '')
                    if isinstance(v, str):
                        text += v
                if 'str_replace_editor' in text or 'edit_file' in text:
                    edit_count += 1
                action_count += 1

            et = 'none'
            for e in hist:
                text = ''
                for k in ('content', 'observation', 'text', 'message'):
                    v = e.get(k, '')
                    if isinstance(v, str):
                        text += v
                extras = e.get('extras', {})
                if isinstance(extras, dict):
                    for k in ('content', 'observation', 'thought'):
                        v = extras.get(k, '')
                        if isinstance(v, str):
                            text += v
                if '<gt-edit-target>' in text:
                    m = re.search(r'Key function:\s*(\S+?)[\(\s]', text)
                    m2 = re.search(r'in\s+(\S+\.py)', text[text.find('Key function:'):] if 'Key function:' in text else '')
                    if m:
                        et = m.group(1) + '() in ' + (m2.group(1) if m2 else '?')
                    break

            ev_path = f'runs/13task_smoke/canary-v2_live-{task}/eval_result.json'
            eval_detail = ''
            try:
                ev = json.load(open(ev_path))
                if 'resolved_ids' in ev:
                    eval_detail = 'resolved_ids=[]'
                else:
                    k = list(ev.keys())[0]
                    r = ev[k]
                    patch_applied = r.get('patch_successfully_applied', False)
                    f2p = r.get('tests_status', {}).get('FAIL_TO_PASS', {})
                    p2p = r.get('tests_status', {}).get('PASS_TO_PASS', {})
                    f2p_pass = len(f2p.get('success', []))
                    f2p_fail = len(f2p.get('failure', []))
                    p2p_fail = len(p2p.get('failure', []))
                    eval_detail = f'patch={patch_applied} F2P:{f2p_pass}pass/{f2p_fail}fail P2P:{p2p_fail}regress'
            except Exception as ex:
                eval_detail = str(ex)

            patch_files = re.findall(r'^\+\+\+ b/(.+)$', gp, re.MULTILINE)
            patch_files = [f for f in patch_files if not f.startswith('.openhands/')]
            patch_empty = len(patch_files) == 0

            category = 'UNKNOWN'
            if patch_empty:
                category = 'NO_PATCH'
            elif 'F2P:0pass' in eval_detail and 'fail' in eval_detail:
                category = 'WRONG_FIX'
            elif 'regress' in eval_detail and not eval_detail.endswith('0regress'):
                category = 'REGRESSION'
            elif f2p_fail > 0 if 'f2p_fail' in dir() else False:
                category = 'PARTIAL_FIX'
            else:
                category = 'CLOSE_BUT_WRONG'

            print(f'{task}')
            print(f'  category: {category}')
            print(f'  edit-target: {et}')
            print(f'  actions: {action_count}, edits: {edit_count}')
            print(f'  patch: {patch_files[:3] if patch_files else "EMPTY"}')
            print(f'  eval: {eval_detail}')
            print()
            break
