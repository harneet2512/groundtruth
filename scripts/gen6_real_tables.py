#!/usr/bin/env python3
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
for short, task in tasks:
    files = glob.glob(f'runs/gen6_eval/{task}/**/output.jsonl', recursive=True)
    if not files: continue
    try:
        ev = json.load(open(f'runs/gen6_eval/{task}/eval_result.json'))
        k = list(ev.keys())[0]; resolved = str(ev[k]['resolved'])
    except: resolved = 'N/A'
    r = {x: '' for x in ['bf','et','kc','sig','callers','callees','test','comp','pat','cr','auto','l5','l5b','l6','scope','grep','vendor','leak']}
    with open(files[0], encoding='utf-8', errors='replace') as f:
        for line in f:
            obj = json.loads(line)
            for i, entry in enumerate(obj.get('history', [])):
                text = ''
                for k2 in ('content','observation','text','message'):
                    v = entry.get(k2,'');
                    if isinstance(v,str): text += v
                extras = entry.get('extras',{})
                if isinstance(extras,dict):
                    for k2 in ('content','observation','thought'):
                        v = extras.get(k2,'')
                        if isinstance(v,str): text += v
                if not text: continue
                if '<gt-task-brief>' in text and not r['bf']:
                    bf = re.findall(r'\d+\.\s+(\S+\.py)', text)[:3]
                    r['bf'] = ', '.join(bf)
                if '<gt-edit-target>' in text and not r['et']:
                    m = re.search(r'Key function:\s*(\S+?)[\(\s]', text)
                    m2 = re.search(r'in\s+(\S+\.py)', text[text.find('Key function:'):] if 'Key function:' in text else '')
                    m3 = re.search(r'(\d+)\s+callers', text)
                    r['et'] = '%s() in %s (%s callers)' % (m.group(1) if m else '?', m2.group(1) if m2 else '?', m3.group(1) if m3 else '?')
                if '[GT KEY CONTRACTS]' in text and not r['kc']:
                    idx = text.find('[GT KEY CONTRACTS]')
                    r['kc'] = text[idx+19:idx+70].replace('\n',' ').strip()
                if '[SIGNATURE]' in text and not r['sig']:
                    idx = text.find('[SIGNATURE]')
                    end = text.find('\n', idx)
                    if end < 0: end = idx+80
                    r['sig'] = 'e%d: %s' % (i, text[idx+12:end].strip()[:65])
                if 'Called by:' in text and not r['callers']:
                    idx = text.find('Called by:')
                    end = text.find('\n', idx)
                    if end < 0: end = idx+120
                    r['callers'] = 'e%d: %s' % (i, text[idx+10:end].strip()[:80])
                    cb = text[idx:idx+400].lower()
                    if any(v in cb for v in ['jquery','/static/','node_modules/','.min.js']):
                        r['vendor'] = 'VENDOR IN CALLERS'
                if 'Calls into:' in text and not r['callees']:
                    idx = text.find('Calls into:')
                    end = text.find('\n', idx)
                    if end < 0: end = idx+120
                    r['callees'] = 'e%d: %s' % (i, text[idx+12:end].strip()[:80])
                if '[TEST]' in text and not r['test']:
                    idx = text.find('[TEST]')
                    r['test'] = 'e%d: %s' % (i, text[idx+6:idx+80].split('\n')[0].strip()[:70])
                if '[COMPLETENESS]' in text and not r['comp']:
                    idx = text.find('[COMPLETENESS]')
                    r['comp'] = 'e%d: %s' % (i, text[idx+15:idx+100].replace('\n',' ').strip()[:70])
                if '[PATTERN]' in text and not r['pat']:
                    idx = text.find('[PATTERN]')
                    pb = text[idx:idx+80]
                    sm = re.search(r'sibling\s+(\w+)\(\)', pb)
                    dd = '__init__' in pb or '__repr__' in pb
                    r['pat'] = 'e%d: sibling %s()%s' % (i, sm.group(1) if sm else '?', ' **DUNDER**' if dd else '')
                if ('[CATCHES]' in text or '[RAISES]' in text) and not r['cr']:
                    for tag in ['[CATCHES]','[RAISES]']:
                        if tag in text:
                            idx = text.find(tag)
                            r['cr'] = 'e%d: %s' % (i, text[idx:idx+65].replace('\n',' ')[:65])
                            break
                if '[GT_AUTO]' in text and not r['auto']:
                    idx = text.find('[GT_AUTO]')
                    r['auto'] = 'e%d: %s' % (i, text[idx+10:idx+70].replace('\n',' ').strip()[:60])
                if '[GT L5: No Source Edits]' in text and not r['l5']:
                    m = re.search(r'Iteration:\s*(\d+)/(\d+)', text)
                    r['l5'] = 'e%d: iter %s/%s' % (i, m.group(1), m.group(2)) if m else 'e%d: fired' % i
                if ('[GT L5: Ignored Structural Witness]' in text or '[GT L5: Unexamined structural signal]' in text) and not r['l5b']:
                    m = re.search(r'for\s+(\S+)\s', text[text.find('Ignored'):])
                    r['l5b'] = 'e%d: suggest %s' % (i, m.group(1)[:35]) if m else 'e%d: fired' % i
                if '[REVIEW]' in text and not r['l6']:
                    idx = text.find('[REVIEW]')
                    ps = re.findall(r'PRESERVE:\s*(\w+)', text[idx:idx+200])
                    r['l6'] = 'e%d: PRESERVE %s' % (i, ', '.join(ps[:3])) if ps else 'e%d: fired' % i
                if '<gt-scope' in text and not r['scope']:
                    m = re.search(r'files="(\d+)"', text)
                    ls = re.findall(r'\d+\.\s+(\S+)', text[text.find('<gt-scope'):text.find('<gt-scope')+150])
                    r['scope'] = 'e%d: %s files (%s)' % (i, m.group(1) if m else '?', ', '.join(ls[:2]))
                if '[GT] Callers of' in text and not r['grep']:
                    m = re.search(r"Callers of '(\w+)'", text)
                    r['grep'] = 'e%d: %s' % (i, m.group(1)) if m else 'e%d: fired' % i
                for hp in ['[GT_META]','[GT_STATUS]','[GT_TRACE]','[GT_DELIVERY]']:
                    if hp in text and not r['leak']: r['leak'] = hp
            break

    print()
    print('  %s (resolved: %s)' % (short, resolved))
    print('  %-22s %s' % ('Layer', 'Verbatim value from output.jsonl'))
    print('  %s %s' % ('-'*22, '-'*90))
    for label, key in [
        ('L1 Brief files', 'bf'), ('L1+ Edit target', 'et'), ('L1+ Key contracts', 'kc'),
        ('L3 [SIGNATURE]', 'sig'), ('L3 Called by', 'callers'), ('L3 Calls into', 'callees'),
        ('L3 [TEST]', 'test'), ('L3 [COMPLETENESS]', 'comp'), ('L3 [PATTERN]', 'pat'),
        ('L4b [CATCHES/RAISES]', 'cr'), ('L4a [GT_AUTO]', 'auto'),
        ('L5 Scaffold', 'l5'), ('L5b Reminder', 'l5b'), ('L6 [REVIEW]', 'l6'),
        ('Consensus scope', 'scope'), ('Grep intercept', 'grep'),
        ('Vendor JS in callers', 'vendor'), ('Hidden prefix leak', 'leak'),
    ]:
        val = r[key] or '(not fired)'
        print('  %-22s %s' % (label, val[:90]))
    print()
