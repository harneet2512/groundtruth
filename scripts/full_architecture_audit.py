#!/usr/bin/env python3
"""Full architecture audit for GT benchmark runs.

Reads output.jsonl for each task, extracts every GT injection,
classifies localization, hook delivery, agent reactions, and outcomes.

Usage: python scripts/full_architecture_audit.py runs/cursor_rerun/
"""
import json, sys, io, glob, re, os
from collections import defaultdict, Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

GT_TAGS = {
    'L1_brief': '<gt-task-brief>',
    'L1_edit_target': '<gt-edit-target>',
    'L1_key_contracts': '[GT KEY CONTRACTS]',
    'L3_signature': '[SIGNATURE]',
    'L3b_called_by': 'Called by:',
    'L3b_calls_into': 'Calls into:',
    'L3_test': '[TEST]',
    'L3_completeness': '[COMPLETENESS]',
    'L3_pattern': '[PATTERN]',
    'L4b_catches': '[CATCHES]',
    'L4b_raises': '[RAISES]',
    'L4a_auto': '[GT_AUTO]',
    'L5_scaffold': '<gt-advisory layer="L5"',
    'L5b_reminder': '[GT L5: Unexamined structural signal]',
    'L6_review': '[REVIEW]',
    'consensus_scope': '<gt-scope',
    'grep_intercept': '[GT] Callers of',
}

HIDDEN_PREFIXES = ['[GT_META]', '[GT_STATUS]', '[GT_TRACE]', '[GT_DELIVERY]']
VENDOR_MARKERS = ['jquery', '/static/', 'node_modules/', '.min.js', 'vendor/']
GT_TOOLS = ['gt_query', 'gt_validate', 'gt_search', 'gt_navigate',
            'groundtruth_trace', 'groundtruth_brief', 'groundtruth_validate',
            'groundtruth_hotspots', 'groundtruth_symbols', 'groundtruth_explain',
            'groundtruth_impact', 'groundtruth_orient', 'groundtruth_do']


def extract_text(entry):
    text = ''
    for k in ('content', 'observation', 'text', 'message'):
        v = entry.get(k, '')
        if isinstance(v, str):
            text += v
    extras = entry.get('extras', {})
    if isinstance(extras, dict):
        for k in ('content', 'observation', 'thought'):
            v = extras.get(k, '')
            if isinstance(v, str):
                text += v
    return text


def detect_edits(text, entry_idx):
    edits = []
    if 'str_replace_editor' in text or 'edit_file_by_replace' in text:
        m = re.search(r'path["\s:=]+(/\S+)', text)
        edits.append(('str_replace_editor', m.group(1) if m else '?', entry_idx))
    if re.search(r'sed\s+-i', text):
        m = re.search(r'sed\s+-i\s+.*?(\S+\.py\b)', text)
        edits.append(('sed', m.group(1) if m else '?', entry_idx))
    if re.search(r"cat\s*<<.*?>>\s*(/\S+)", text):
        m = re.search(r"cat\s*<<.*?>>\s*(/\S+)", text)
        edits.append(('cat_heredoc', m.group(1) if m else '?', entry_idx))
    if re.search(r'>\s*/workspace/\S+', text) and 'echo' in text.lower():
        m = re.search(r'>\s*(/workspace/\S+)', text)
        edits.append(('echo_redirect', m.group(1) if m else '?', entry_idx))
    if "open(" in text and ("'w'" in text or '"w"' in text) and '/workspace/' in text:
        m = re.search(r"open\(['\"](/workspace/\S+?)['\"]", text)
        if m:
            edits.append(('python_write', m.group(1), entry_idx))
    return edits


def audit_task(task_dir):
    task_name = os.path.basename(task_dir)
    short = task_name.replace('canary-v2_live-', '').replace('canary-baseline-', '')

    output_files = glob.glob(os.path.join(task_dir, '**', 'output.jsonl'), recursive=True)
    if not output_files:
        return {'task': short, 'error': 'NO OUTPUT.JSONL'}

    # Eval result
    eval_path = os.path.join(task_dir, 'eval_result.json')
    resolved = None
    try:
        ev = json.load(open(eval_path))
        if 'resolved_ids' in ev:
            resolved = len(ev['resolved_ids']) > 0
        else:
            k = list(ev.keys())[0]
            resolved = ev[k].get('resolved', None)
    except:
        resolved = None

    result = {
        'task': short,
        'resolved': resolved,
        'total_entries': 0,
        'gt_injections': [],
        'hook_counts': Counter(),
        'all_edits': [],
        'gt_tool_calls': [],
        'hidden_leaks': [],
        'vendor_in_callers': False,
        'brief_files': [],
        'edit_target': '',
        'git_patch': '',
        'agent_actions': 0,
    }

    with open(output_files[0], encoding='utf-8', errors='replace') as f:
        for line in f:
            obj = json.loads(line)
            hist = obj.get('history', [])
            result['total_entries'] = len(hist)

            # Git patch
            gp = obj.get('git_patch', '')
            tr = obj.get('test_result', {})
            if isinstance(tr, dict):
                gp2 = tr.get('git_patch', '')
                if gp2 and not gp:
                    gp = gp2
            result['git_patch'] = gp

            for i, entry in enumerate(hist):
                text = extract_text(entry)
                if not text:
                    continue

                action = entry.get('action', '')
                action_type = str(entry.get('action_type', ''))
                if action or action_type:
                    result['agent_actions'] += 1

                # Detect GT injections
                for layer_name, tag in GT_TAGS.items():
                    if tag in text:
                        idx = text.find(tag)
                        snippet = text[idx:idx+300].replace('\n', ' ').strip()
                        result['gt_injections'].append({
                            'entry': i,
                            'layer': layer_name,
                            'snippet': snippet[:250],
                        })
                        result['hook_counts'][layer_name] += 1

                # Brief files
                if '<gt-task-brief>' in text and not result['brief_files']:
                    result['brief_files'] = re.findall(r'\d+\.\s+(\S+\.(?:py|js|ts|go|rs|java))', text)[:5]

                # Edit target
                if '<gt-edit-target>' in text and not result['edit_target']:
                    m = re.search(r'Key function:\s*(\S+?)[\(\s]', text)
                    m2 = re.search(r'in\s+(\S+\.(?:py|js|ts|go|rs|java))', text[text.find('Key function:'):] if 'Key function:' in text else '')
                    result['edit_target'] = '%s() in %s' % (m.group(1) if m else '?', m2.group(1) if m2 else '?')

                # Detect edits
                edits = detect_edits(text, i)
                result['all_edits'].extend(edits)

                # Detect GT tool calls
                for tool in GT_TOOLS:
                    if tool in text.lower():
                        result['gt_tool_calls'].append((i, tool))

                # Hidden prefix leaks
                for hp in HIDDEN_PREFIXES:
                    if hp in text:
                        result['hidden_leaks'].append((i, hp))

                # Vendor in callers
                if 'Called by:' in text:
                    cb = text[text.find('Called by:'):text.find('Called by:') + 400].lower()
                    if any(v in cb for v in VENDOR_MARKERS):
                        result['vendor_in_callers'] = True

            break

    # Patch analysis
    patch = result['git_patch']
    patch_files = re.findall(r'^\+\+\+ b/(.+)$', patch, re.MULTILINE)
    result['patch_files'] = [f for f in patch_files if not f.startswith('.openhands/')]
    result['patch_empty'] = len(result['patch_files']) == 0

    return result


def print_task_report(r):
    print('=' * 100)
    print(f"TASK: {r['task']}")
    print(f"Resolved: {r['resolved']}  |  Entries: {r['total_entries']}  |  Actions: {r['agent_actions']}")
    print(f"GT injections: {sum(r['hook_counts'].values())}  |  GT tool calls: {len(r['gt_tool_calls'])}")
    print()

    # 1. Localization
    print('--- LOCALIZATION ---')
    print(f"  L1 Brief files: {', '.join(r['brief_files']) if r['brief_files'] else '(none)'}")
    print(f"  L1+ Edit target: {r['edit_target'] or '(none)'}")
    if r['all_edits']:
        seen = set()
        for method, path, idx in r['all_edits']:
            key = (method, path)
            if key not in seen:
                print(f"  Agent edit: e{idx} via {method} -> {path}")
                seen.add(key)
    else:
        print("  Agent edits: NONE")
    if r['patch_files']:
        print(f"  Patch files: {', '.join(r['patch_files'])}")
    else:
        print(f"  Patch files: EMPTY PATCH")
    print()

    # 2. Per-layer delivery
    print('--- HOOK DELIVERY (count per layer) ---')
    for layer in GT_TAGS:
        count = r['hook_counts'].get(layer, 0)
        if count > 0:
            print(f"  {layer:25s} {count}x")
    for layer in GT_TAGS:
        if r['hook_counts'].get(layer, 0) == 0:
            print(f"  {layer:25s} (not fired)")
    print()

    # 3. GT injection details (first occurrence per layer)
    print('--- GT INJECTION DETAILS (first per layer) ---')
    seen_layers = set()
    for inj in r['gt_injections']:
        if inj['layer'] not in seen_layers:
            seen_layers.add(inj['layer'])
            print(f"  e{inj['entry']} [{inj['layer']}]: {inj['snippet'][:150]}")
    print()

    # 4. Tool use
    print('--- GT TOOL USE ---')
    if r['gt_tool_calls']:
        for idx, tool in r['gt_tool_calls']:
            print(f"  e{idx}: {tool}")
    else:
        print("  Agent never called any GT MCP tool")
    print()

    # 5. Safety checks
    print('--- SAFETY ---')
    print(f"  Hidden prefix leaks: {len(r['hidden_leaks'])} {'(' + ', '.join(f'e{i}:{h}' for i,h in r['hidden_leaks'][:3]) + ')' if r['hidden_leaks'] else ''}")
    print(f"  Vendor JS in callers: {'YES' if r['vendor_in_callers'] else 'clean'}")
    completeness_count = r['hook_counts'].get('L3_completeness', 0)
    print(f"  PRIOR-004 (class-wide completeness): {'FIRED %dx' % completeness_count if completeness_count > 0 else 'correct_silence'}")
    print()

    # 6. Verdict
    print('--- VERDICT ---')
    total_gt = sum(r['hook_counts'].values())
    issues = []
    if r['hidden_leaks']:
        issues.append('hidden_prefix_leak')
    if r['vendor_in_callers']:
        issues.append('vendor_in_callers')
    if completeness_count > 0:
        issues.append('PRIOR-004_recurrence')
    if r['patch_empty']:
        issues.append('empty_patch')
    if not r['brief_files']:
        issues.append('no_brief')
    if issues:
        print(f"  Issues: {', '.join(issues)}")
    else:
        print(f"  Issues: NONE")
    print(f"  GT behavior: {'DELIVERED' if total_gt > 0 else 'SILENT'} ({total_gt} injections)")
    print(f"  Resolved: {r['resolved']}")
    print()


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/full_architecture_audit.py <run_dir>")
        print("Example: python scripts/full_architecture_audit.py runs/cursor_rerun/")
        sys.exit(1)

    run_dir = sys.argv[1]
    task_dirs = sorted(glob.glob(os.path.join(run_dir, 'canary-*')))
    if not task_dirs:
        task_dirs = sorted(d for d in glob.glob(os.path.join(run_dir, '*')) if os.path.isdir(d))

    if not task_dirs:
        print(f"No task directories found in {run_dir}")
        sys.exit(1)

    results = []
    for td in task_dirs:
        r = audit_task(td)
        results.append(r)
        print_task_report(r)

    # Summary table
    print('=' * 100)
    print('SUMMARY TABLE')
    print('=' * 100)
    print(f"{'Task':40s} {'Resolved':10s} {'GT inj':8s} {'Tools':6s} {'Edits':6s} {'Patch':8s} {'PRIOR004':10s} {'Leaks':6s} {'Vendor':7s}")
    print('-' * 100)
    for r in results:
        total_gt = sum(r['hook_counts'].values())
        print(f"{r['task']:40s} {str(r['resolved']):10s} {total_gt:8d} {len(r['gt_tool_calls']):6d} {len(r['all_edits']):6d} {'EMPTY' if r['patch_empty'] else 'yes':8s} {'FIRED' if r['hook_counts'].get('L3_completeness',0)>0 else 'clean':10s} {len(r['hidden_leaks']):6d} {'YES' if r['vendor_in_callers'] else 'no':7s}")

    print()
    print('LAYER FIRING MATRIX')
    print(f"{'Task':40s}", end='')
    layer_order = ['L1_brief', 'L1_edit_target', 'L3_signature', 'L3b_called_by', 'L3b_calls_into',
                   'L3_test', 'L3_completeness', 'L3_pattern', 'L4b_catches', 'L4b_raises',
                   'L4a_auto', 'L5_scaffold', 'L5b_reminder', 'L6_review', 'consensus_scope', 'grep_intercept']
    for l in layer_order:
        short = l.replace('L3b_', '').replace('L3_', '').replace('L4b_', '').replace('L4a_', '').replace('L5b_', '').replace('L5_', '').replace('L6_', '').replace('L1_', '')[:6]
        print(f" {short:>6s}", end='')
    print()
    print('-' * (40 + 7 * len(layer_order)))
    for r in results:
        print(f"{r['task']:40s}", end='')
        for l in layer_order:
            c = r['hook_counts'].get(l, 0)
            print(f" {c if c > 0 else '-':>6}", end='')
        print()

    # Resolve rate
    resolved_count = sum(1 for r in results if r['resolved'] is True)
    total = len(results)
    print()
    print(f"RESOLVE RATE: {resolved_count}/{total}")


if __name__ == '__main__':
    main()
