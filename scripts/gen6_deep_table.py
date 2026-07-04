#!/usr/bin/env python3
"""Generate DOC_OF_HONOR vs Reality table for each task in gen6 eval."""
import json, sys, io, glob, re, os

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
    base = f'runs/gen6_eval/{task}'
    files = glob.glob(f'{base}/**/output.jsonl', recursive=True)
    if not files:
        print(f'{short}: NO OUTPUT'); continue

    try:
        ev = json.load(open(f'{base}/eval_result.json'))
        k = list(ev.keys())[0]; resolved = str(ev[k]['resolved'])
    except: resolved = 'N/A'

    # Accumulators
    brief_files = []
    brief_entry = -1
    et_func = et_file = et_callers = ''
    et_entry = -1
    kc_text = ''
    kc_entry = -1
    pe_entry = -1
    pe_markers = []
    pe_sig = ''
    test_all = []
    comp_all = []
    pat_all = []
    pv_count = 0
    pv_vendor = False
    pv_sample = ''
    auto_entry = -1
    auto_text = ''
    l5_entry = -1
    l5_var = ''
    l5b_entry = -1
    l5b_text = ''
    l6_entry = -1
    l6_text = ''
    scope_entry = -1
    grep_entry = -1
    hidden_leaked = []

    with open(files[0], encoding='utf-8', errors='replace') as f:
        for line in f:
            obj = json.loads(line)
            history = obj.get('history', [])
            for i, entry in enumerate(history):
                text = ''
                for k2 in ('content', 'observation', 'text', 'message'):
                    v = entry.get(k2, '')
                    if isinstance(v, str): text += v
                extras = entry.get('extras', {})
                if isinstance(extras, dict):
                    for k2 in ('content', 'observation', 'thought'):
                        v = extras.get(k2, '')
                        if isinstance(v, str): text += v
                if not text: continue

                if '<gt-task-brief>' in text and brief_entry < 0:
                    brief_entry = i
                    brief_files = re.findall(r'\d+\.\s+(\S+\.py)', text)[:3]

                if '<gt-edit-target>' in text and et_entry < 0:
                    et_entry = i
                    m = re.search(r'Key function:\s*(\S+?)[\(\s]', text)
                    if m: et_func = m.group(1)
                    m2 = re.search(r'in\s+(\S+\.py)', text[text.find('Key function:'):] if 'Key function:' in text else '')
                    if m2: et_file = m2.group(1)
                    m3 = re.search(r'(\d+)\s+callers', text)
                    if m3: et_callers = m3.group(1)

                if '[GT KEY CONTRACTS]' in text and kc_entry < 0:
                    kc_entry = i
                    idx = text.find('[GT KEY CONTRACTS]')
                    kc_text = text[idx+19:idx+80].replace('\n',' ').strip()

                if ('[SIGNATURE]' in text or '[BEHAVIORAL CONTRACT]' in text) and pe_entry < 0:
                    pe_entry = i
                    for pm in ['[SIGNATURE]','[BEHAVIORAL CONTRACT]','PRESERVE:','Called by:','Calls into:','[TEST]','[COMPLETENESS]','[PATTERN]','[SIMILAR]','[REVIEW]','[CATCHES]','[RAISES]']:
                        if pm in text: pe_markers.append(pm)
                    sm = re.search(r'\[SIGNATURE\]\s*(.+?)[\|\n]', text.replace('\n','|'))
                    if sm: pe_sig = sm.group(1).strip()[:60]

                if '[TEST]' in text:
                    tidx = text.find('[TEST]')
                    tb = text[tidx:tidx+200]
                    is_h = any(h in tb for h in ['_common.py','conftest.py','helper.py'])
                    tf = re.search(r'(\S+test\S*\.py)', tb)
                    test_all.append({'e':i, 'helper':is_h, 'file': tf.group(1) if tf else tb[6:50].strip()})

                if '[COMPLETENESS]' in text:
                    cidx = text.find('[COMPLETENESS]')
                    comp_all.append({'e':i, 't': text[cidx:cidx+100].replace('\n',' ')})

                if '[PATTERN]' in text:
                    pidx = text.find('[PATTERN]')
                    pb = text[pidx:pidx+150]
                    dd = any(d in pb for d in ['__init__','__repr__','__str__'])
                    sm = re.search(r'sibling\s+(\w+)\(\)', pb)
                    pat_all.append({'e':i, 'dunder':dd, 'sib': sm.group(1) if sm else '?'})

                if 'Called by:' in text or 'Calls into:' in text:
                    pv_count += 1
                    if not pv_sample:
                        cm = re.search(r'Called by:\s*(\S+:\d+)', text)
                        if cm: pv_sample = cm.group(1)[:30]
                    for ck in ['Called by:', 'Calls into:']:
                        if ck in text:
                            ci = text.find(ck)
                            cb = text[ci:ci+400].lower()
                            if any(v in cb for v in ['jquery','/static/','node_modules/','.min.js']):
                                pv_vendor = True

                if '[GT_AUTO]' in text and auto_entry < 0:
                    auto_entry = i
                    ai = text.find('[GT_AUTO]')
                    auto_text = text[ai:ai+60].replace('\n',' ')

                if ('[GT L5: No Source Edits]' in text or 'Scaffolding' in text or '<gt-advisory' in text) and l5_entry < 0:
                    l5_entry = i
                    if 'No Source Edits' in text: l5_var = 'No Source Edits'
                    elif 'Scaffolding' in text: l5_var = 'Scaffolding Trap'
                    else: l5_var = 'advisory'

                if '[GT L5: Ignored Structural Witness]' in text or '[GT L5: Unexamined structural signal]' in text or '[GT L5: Scope Check]' in text:
                    l5b_entry = i
                    li = text.find('[GT L5:')
                    l5b_text = text[li:li+50].replace('\n',' ')

                if '[REVIEW]' in text and l6_entry < 0:
                    l6_entry = i
                    ri = text.find('[REVIEW]')
                    l6_text = text[ri:ri+60].replace('\n',' ')

                if '<gt-scope' in text and scope_entry < 0:
                    scope_entry = i

                if '[GT] Callers of' in text and grep_entry < 0:
                    grep_entry = i

                for hp in ['[GT_META]','[GT_STATUS]','[GT_TRACE]','[GT_DELIVERY]']:
                    if hp in text and hp not in hidden_leaked:
                        hidden_leaked.append(hp)
            break

    # PRINT TABLE
    W = 130
    print()
    print(f'  {short} -- DOC_OF_HONOR vs Reality (Resolved: {resolved})')
    print()
    hdr = f'  {"DOC Layer":<18} {"DOC Intent":<32} {"What Agent Actually Saw":<55} {"Match?":<13} {"Issue"}'
    print(hdr)
    print(f'  {"-"*18} {"-"*32} {"-"*55} {"-"*13} {"-"*40}')

    def row(layer, intent, saw, match, issue):
        print(f'  {layer:<18} {intent:<32} {saw:<55} {match:<13} {issue}')

    row('L0 Index', 'graph.db with 7 tables', 'Substrate (queries work)', 'YES', '-')

    if brief_entry >= 0:
        row('L1 Brief', 'Ranked files+callers/tests', f'Entry {brief_entry}: files={brief_files}', 'YES', '-')
    else:
        row('L1 Brief', 'Ranked files+callers/tests', 'NOT FIRED', 'NO', 'Brief missing')

    if et_entry >= 0:
        row('L1+ Edit Target', 'Most relevant function', f'Entry {et_entry}: {et_func}() in {et_file}, {et_callers} cal', 'CONDITIONAL', 'Depends on brief ranking')
    else:
        row('L1+ Edit Target', 'Most relevant function', 'NOT FIRED', 'NO', '-')

    if kc_entry >= 0:
        row('L1+ Key Contracts', 'Guard/return properties', f'Entry {kc_entry}: {kc_text[:40]}', 'YES', '-')
    else:
        row('L1+ Key Contracts', 'Guard/return properties', 'Not fired (no qualifying props)', 'CONDITIONAL', '-')

    if pe_entry >= 0:
        row('L3 Post-Edit', 'Sig/contract/test/callers', f'Entry {pe_entry}: {pe_sig[:30]} +{len(pe_markers)} markers', 'YES', '-')
    else:
        row('L3 Post-Edit', 'Sig/contract/test/callers', 'NOT FIRED', 'NO', 'No source edits?')

    # TEST
    if test_all:
        helpers = [t for t in test_all if t['helper']]
        if helpers:
            row('L3 [TEST]', 'Relevant test assertions', f'{len(test_all)}x, HELPER={helpers[0].get("file","")}', 'NO', f'PRIOR-003: helper outranks')
        else:
            row('L3 [TEST]', 'Relevant test assertions', f'{len(test_all)}x, file={test_all[0].get("file","inline")[:35]}', 'YES', '-')
    else:
        row('L3 [TEST]', 'Relevant test assertions', 'No [TEST] evidence', 'N/A', '-')

    # COMPLETENESS
    if comp_all:
        row('L3 [COMPLETENESS]', 'Scoped to edited func', f'{len(comp_all)}x: {comp_all[0]["t"][:40]}', 'PARTIAL', 'PRIOR-004 possible')
    else:
        row('L3 [COMPLETENESS]', 'Scoped to edited func', 'No [COMPLETENESS]', 'N/A', '-')

    # PATTERN
    if pat_all:
        if any(p['dunder'] for p in pat_all):
            row('L3 [PATTERN]', 'Sibling, no dunders', f'{len(pat_all)}x DUNDER LEAK', 'NO', 'PRIOR-008 recurrence')
        else:
            row('L3 [PATTERN]', 'Sibling, no dunders', f'{len(pat_all)}x, sib={pat_all[0]["sib"]}()', 'YES', '-')
    else:
        row('L3 [PATTERN]', 'Sibling, no dunders', 'No [PATTERN]', 'N/A', '-')

    # L3b
    if pv_count > 0:
        vflag = 'VENDOR JS!' if pv_vendor else '-'
        row('L3b Post-View', 'Callers/callees on read', f'{pv_count}x, sample={pv_sample[:25]}', 'NO' if pv_vendor else 'YES', vflag)
    else:
        row('L3b Post-View', 'Callers/callees on read', 'NOT FIRED', 'N/A', '-')

    # L4a
    if auto_entry >= 0:
        row('L4a Auto-Query', 'Key symbols on first read', f'Entry {auto_entry}: {auto_text[:40]}', 'YES', '-')
    else:
        row('L4a Auto-Query', 'Key symbols on first read', 'NOT FIRED', 'N/A', '-')

    # L5
    if l5_entry >= 0:
        row('L5 Scaffold', 'Warning on scratch files', f'Entry {l5_entry}: {l5_var}', 'YES', '-')
    else:
        row('L5 Scaffold', 'Warning on scratch files', 'Not fired', 'N/A', '-')

    # L5b
    if l5b_entry >= 0:
        row('L5b Reminder', 'Ignored witness warning', f'Entry {l5b_entry}: {l5b_text[:40]}', 'YES', '-')
    else:
        row('L5b Reminder', 'Ignored witness warning', 'Not fired', 'N/A', '-')

    # L6
    if l6_entry >= 0:
        row('L6 Pre-Submit', 'Review before finish', f'Entry {l6_entry}: {l6_text[:40]}', 'YES', '-')
    else:
        row('L6 Pre-Submit', 'Review before finish', 'Not fired (no callers/no edit)', 'N/A', '-')

    # Consensus
    if scope_entry >= 0:
        row('Consensus', 'Scope on candidate view', f'Entry {scope_entry}: <gt-scope>', 'YES', '-')
    else:
        row('Consensus', 'Scope on candidate view', 'Not fired', 'N/A', '-')

    # Grep
    if grep_entry >= 0:
        row('Grep Intercept', 'Callers on grep', f'Entry {grep_entry}: [GT] Callers of', 'YES', '-')
    else:
        row('Grep Intercept', 'Callers on grep', 'Not fired', 'N/A', '-')

    # Hidden
    if hidden_leaked:
        row('Hidden Prefixes', 'No GT_META in agent text', f'LEAKED: {hidden_leaked}', 'NO', 'Internal prefix visible')
    else:
        row('Hidden Prefixes', 'No GT_META in agent text', 'No leaks', 'YES', '-')

    print()
