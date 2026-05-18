#!/usr/bin/env python3
"""Quick metrics for any GT run artifacts. Usage:
    python scripts/compute_run_metrics.py /tmp/v2_all5
    python scripts/compute_run_metrics.py /tmp/v2_all5 --compare /tmp/old_gt_5task
"""
import json, os, glob, sys, argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.localization_metrics import compute_task_metrics


def find_outputs(base):
    return sorted(glob.glob(f'{base}/task-*/results/**/output.jsonl', recursive=True))


def task_id(path):
    seg = path.replace('\\', '/').split('task-')[1].split('/')[0]
    return seg


def analyze(base):
    results = []
    for f in find_outputs(base):
        tid = task_id(f)
        m = compute_task_metrics(f, tid)
        # Extra: check GT markers in output
        with open(f, encoding='utf-8', errors='replace') as fh:
            content = fh.readline()
        gt_count = content.count('[GT]') + content.count('[GT-router-v2')
        m['gt_markers_in_output'] = gt_count
        # Check tool usage
        m['gt_query_called'] = 'gt_query' in content
        m['gt_validate_called'] = 'gt_validate' in content
        results.append(m)
    return results


def print_table(results, label=""):
    if label:
        print(f"\n{'='*70}\n  {label}\n{'='*70}")
    print(f"{'Task':<18} {'Res':<4} {'1stG':<5} {'Acts':<5} {'Prec':<5} {'GTinj':<5} {'Brdg':<5} {'Stale':<5} {'Late':<4} {'Tools'}")
    print("-" * 75)
    for m in results:
        short = m['task_id'].split('__')[1][:15] if '__' in m['task_id'] else m['task_id'][:15]
        fgv = str(m['first_gold_view_step'] or '-')
        res = 'YES' if m['resolved'] else 'NO'
        tools = 'Q+V' if m.get('gt_query_called') and m.get('gt_validate_called') else ('Q' if m.get('gt_query_called') else '-')
        print(f"{short:<18} {res:<4} {fgv:<5} {m['action_count']:<5} {m['edit_file_precision']:<5.2f} {m.get('gt_all_events',0):<5} {m['l3b_bridge_events']:<5} {m['stale_guidance_count']:<5} {m['late_guidance_count']:<4} {tools}")
    # Summary
    n = len(results)
    resolved = sum(1 for m in results if m['resolved'])
    avg_acts = sum(m['action_count'] for m in results) / max(n, 1)
    print("-" * 75)
    print(f"{'TOTAL':<18} {resolved}/{n:<3} {'':5} {avg_acts:<5.0f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('path', help='Artifact directory')
    parser.add_argument('--compare', help='Compare against another artifact dir')
    parser.add_argument('--label', default='')
    args = parser.parse_args()

    results = analyze(args.path)
    print_table(results, args.label or os.path.basename(args.path))

    if args.compare:
        comp = analyze(args.compare)
        print_table(comp, f"COMPARE: {os.path.basename(args.compare)}")


if __name__ == '__main__':
    main()
