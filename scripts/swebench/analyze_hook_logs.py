#!/usr/bin/env python3
"""Post-run analysis of GT hook logs from startupmode eval.

Reads gt_hook_log.jsonl files from task containers and produces
per-task and aggregate metrics.

Usage:
    python analyze_hook_logs.py --gt-output ~/results/startupmode/gt \
                                --baseline-output ~/results/startupmode/baseline

    # Or just analyze hook logs from a single directory:
    python analyze_hook_logs.py --hook-logs /path/to/collected_logs/
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def load_hook_logs(log_dir):
    """Load all gt_hook_log.jsonl files from a directory tree.

    Returns list of log entries, each augmented with 'source_file'.
    """
    entries = []
    for root, _dirs, files in os.walk(log_dir):
        for fname in files:
            if fname == 'gt_hook_log.jsonl' or fname.endswith('_hook_log.jsonl'):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r') as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                entry = json.loads(line)
                                entry['source_file'] = fpath
                                entries.append(entry)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"WARNING: Failed to parse {fpath}: {e}", file=sys.stderr)
    return entries


def load_results(output_dir):
    """Load output.jsonl from an eval run. Returns dict: instance_id -> result."""
    results = {}
    output_file = os.path.join(output_dir, 'output.jsonl')
    if not os.path.exists(output_file):
        return results
    with open(output_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                entry = json.loads(line)
                instance_id = entry.get('instance_id', '')
                results[instance_id] = entry
    return results


def analyze_hooks(entries):
    """Compute aggregate metrics from hook log entries."""
    metrics = {
        'total_invocations': len(entries),
        'enrich_count': 0,
        'enrich_with_output': 0,
        'check_quiet_count': 0,
        'check_with_output': 0,
        'total_obligations_reported': 0,
        'total_suppressed': 0,
        'suppressed_reasons': defaultdict(int),
        'latencies_ms': [],
        'enrich_latencies_ms': [],
        'check_latencies_ms': [],
    }

    for entry in entries:
        mode = entry.get('mode', '')
        wall_time = entry.get('wall_time_ms', 0)
        metrics['latencies_ms'].append(wall_time)

        if mode == 'enrich':
            metrics['enrich_count'] += 1
            metrics['enrich_latencies_ms'].append(wall_time)
            if entry.get('output_lines', 0) > 0:
                metrics['enrich_with_output'] += 1

        elif mode == 'check_quiet':
            metrics['check_quiet_count'] += 1
            metrics['check_latencies_ms'].append(wall_time)
            if entry.get('after_abstention', 0) > 0:
                metrics['check_with_output'] += 1
            metrics['total_obligations_reported'] += len(entry.get('obligations_reported', []))
            metrics['total_suppressed'] += entry.get('suppressed_count', 0)
            for reason in entry.get('suppressed_reasons', []):
                metrics['suppressed_reasons'][reason] += 1

    return metrics


def percentile(values, p):
    """Compute the p-th percentile of a list of values."""
    if not values:
        return 0
    sorted_v = sorted(values)
    idx = int(len(sorted_v) * p / 100)
    return sorted_v[min(idx, len(sorted_v) - 1)]


def format_report(metrics, gt_results=None, baseline_results=None):
    """Format a human-readable report."""
    lines = []
    lines.append("=" * 60)
    lines.append("GT STARTUPMODE HOOK ANALYSIS")
    lines.append("=" * 60)

    lines.append(f"\n## Invocation Summary")
    lines.append(f"Total hook invocations:     {metrics['total_invocations']}")
    lines.append(f"Enrich (file read):         {metrics['enrich_count']}")
    lines.append(f"  With output:              {metrics['enrich_with_output']}")
    if metrics['enrich_count'] > 0:
        rate = metrics['enrich_with_output'] / metrics['enrich_count'] * 100
        lines.append(f"  Fire rate:                {rate:.1f}%")
    lines.append(f"Check-quiet (file edit):    {metrics['check_quiet_count']}")
    lines.append(f"  With output:              {metrics['check_with_output']}")
    if metrics['check_quiet_count'] > 0:
        rate = metrics['check_with_output'] / metrics['check_quiet_count'] * 100
        lines.append(f"  Fire rate:                {rate:.1f}%")

    lines.append(f"\n## Obligation Metrics")
    lines.append(f"Obligations reported:       {metrics['total_obligations_reported']}")
    lines.append(f"Findings suppressed:        {metrics['total_suppressed']}")
    if metrics['suppressed_reasons']:
        lines.append(f"Suppression breakdown:")
        for reason, count in sorted(metrics['suppressed_reasons'].items(), key=lambda x: -x[1]):
            lines.append(f"  {reason}: {count}")

    lines.append(f"\n## Latency")
    if metrics['latencies_ms']:
        lines.append(f"All hooks P50:              {percentile(metrics['latencies_ms'], 50)}ms")
        lines.append(f"All hooks P95:              {percentile(metrics['latencies_ms'], 95)}ms")
        lines.append(f"All hooks P99:              {percentile(metrics['latencies_ms'], 99)}ms")
    if metrics['enrich_latencies_ms']:
        lines.append(f"Enrich P50:                 {percentile(metrics['enrich_latencies_ms'], 50)}ms")
        lines.append(f"Enrich P95:                 {percentile(metrics['enrich_latencies_ms'], 95)}ms")
    if metrics['check_latencies_ms']:
        lines.append(f"Check P50:                  {percentile(metrics['check_latencies_ms'], 50)}ms")
        lines.append(f"Check P95:                  {percentile(metrics['check_latencies_ms'], 95)}ms")

    # A/B comparison if both result sets provided
    if gt_results and baseline_results:
        lines.append(f"\n## A/B Comparison")

        # Common tasks
        common = set(gt_results.keys()) & set(baseline_results.keys())
        lines.append(f"Common tasks:               {len(common)}")

        gt_resolved = sum(1 for tid in common if gt_results[tid].get('resolved', False))
        bl_resolved = sum(1 for tid in common if baseline_results[tid].get('resolved', False))

        lines.append(f"GT resolved:                {gt_resolved}/{len(common)} ({gt_resolved/len(common)*100:.1f}%)" if common else "")
        lines.append(f"Baseline resolved:          {bl_resolved}/{len(common)} ({bl_resolved/len(common)*100:.1f}%)" if common else "")
        delta = gt_resolved - bl_resolved
        lines.append(f"Delta:                      {'+' if delta >= 0 else ''}{delta} ({delta/len(common)*100:+.1f}%)" if common else "")

        # Task-level flips
        gt_wins = []
        gt_losses = []
        for tid in sorted(common):
            gt_ok = gt_results[tid].get('resolved', False)
            bl_ok = baseline_results[tid].get('resolved', False)
            if gt_ok and not bl_ok:
                gt_wins.append(tid)
            elif bl_ok and not gt_ok:
                gt_losses.append(tid)

        lines.append(f"\nGT wins (GT solved, baseline didn't):  {len(gt_wins)}")
        for tid in gt_wins[:10]:
            lines.append(f"  + {tid}")
        if len(gt_wins) > 10:
            lines.append(f"  ... +{len(gt_wins) - 10} more")

        lines.append(f"GT losses (baseline solved, GT didn't): {len(gt_losses)}")
        for tid in gt_losses[:10]:
            lines.append(f"  - {tid}")
        if len(gt_losses) > 10:
            lines.append(f"  ... +{len(gt_losses) - 10} more")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Analyze GT hook logs from startupmode eval")
    parser.add_argument("--hook-logs", help="Directory containing gt_hook_log.jsonl files")
    parser.add_argument("--gt-output", help="GT condition output directory")
    parser.add_argument("--baseline-output", help="Baseline condition output directory")
    parser.add_argument("--output", "-o", help="Write report to file (default: stdout)")
    args = parser.parse_args()

    # Load hook logs
    entries = []
    if args.hook_logs:
        entries = load_hook_logs(args.hook_logs)
    elif args.gt_output:
        entries = load_hook_logs(args.gt_output)

    if not entries:
        print("No hook log entries found. Specify --hook-logs or --gt-output.", file=sys.stderr)

    # Analyze
    metrics = analyze_hooks(entries)

    # Load results for A/B comparison
    gt_results = load_results(args.gt_output) if args.gt_output else None
    baseline_results = load_results(args.baseline_output) if args.baseline_output else None

    # Format and output report
    report = format_report(metrics, gt_results, baseline_results)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(report)
        print(f"Report written to {args.output}")
    else:
        print(report)

    # Also dump raw metrics as JSON for programmatic use
    json_metrics = {k: v for k, v in metrics.items() if k != 'suppressed_reasons'}
    json_metrics['suppressed_reasons'] = dict(metrics['suppressed_reasons'])
    # Remove large latency arrays from JSON output
    for key in ('latencies_ms', 'enrich_latencies_ms', 'check_latencies_ms'):
        vals = json_metrics.pop(key, [])
        if vals:
            json_metrics[f'{key}_p50'] = percentile(vals, 50)
            json_metrics[f'{key}_p95'] = percentile(vals, 95)
            json_metrics[f'{key}_count'] = len(vals)

    json_path = (args.output or 'hook_analysis') + '.json'
    if args.output:
        json_path = args.output.rsplit('.', 1)[0] + '.json'
    with open(json_path, 'w') as f:
        json.dump(json_metrics, f, indent=2)
    print(f"Metrics JSON written to {json_path}", file=sys.stderr)


if __name__ == '__main__':
    main()
