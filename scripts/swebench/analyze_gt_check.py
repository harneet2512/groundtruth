#!/usr/bin/env python3
"""Analyze gt_check calls from SWE-bench GT run.

Reads GT tool call JSONL + Inspect eval logs to:
1. Map gt_check calls to task instance_ids
2. Classify responses (CLEAN, VIOLATIONS_FOUND, ERROR, EMPTY, UNINFORMATIVE)
3. Cross-reference with task outcomes (resolved/failed)
4. Build the diagnostic matrix

Run on the VM where the data lives:
    python3.11 scripts/swebench/analyze_gt_check.py \
        --gt-log /tmp/gt_tool_calls_full.jsonl \
        --eval-dir ~/results/gt/ \
        --baseline-dir ~/results/baseline/
"""
import argparse
import json
import glob
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta


def load_gt_calls(path):
    """Load GT tool call log."""
    calls = []
    with open(path) as f:
        for line in f:
            calls.append(json.loads(line))
    return calls


def load_eval_results(eval_dir):
    """Load Inspect eval log and extract per-task results + tool call timestamps."""
    eval_files = sorted(glob.glob(os.path.join(eval_dir, "*.eval")),
                       key=os.path.getmtime)
    if not eval_files:
        print(f"No eval files in {eval_dir}")
        return {}, {}

    from inspect_ai.log import read_eval_log

    results = {}  # instance_id -> resolved (bool)
    task_tool_calls = {}  # instance_id -> list of tool call info

    for ef in eval_files:
        log = read_eval_log(ef)
        if not log.samples:
            continue
        for sample in log.samples:
            iid = str(sample.id)
            resolved = False
            if sample.scores:
                resolved = any(
                    hasattr(v, 'value') and v.value == 1.0
                    for v in sample.scores.values()
                )
            results[iid] = resolved

            # Extract tool calls from messages
            tc_list = []
            for msg in (sample.messages or []):
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    for tc in msg.tool_calls:
                        if tc.function and 'gt_' in tc.function:
                            tc_list.append({
                                'function': tc.function,
                                'arguments': tc.arguments if hasattr(tc, 'arguments') else {},
                            })
            task_tool_calls[iid] = tc_list

    return results, task_tool_calls


def classify_gt_check_response(result_preview, result_length):
    """Classify a gt_check response."""
    if result_length < 20:
        return "EMPTY_INPUT"

    preview = result_preview.lower()

    if "error" in preview or "traceback" in preview or "exception" in preview:
        return "ERROR"

    # Check for violation markers
    has_violations = False
    has_clean = False

    if "\u2717" in result_preview or "NOT modified" in result_preview or "missing" in preview:
        has_violations = True
    if "\u2713" in result_preview or "modified" in result_preview:
        has_clean = True

    if "no changes detected" in preview or "no diff" in preview or "no patch" in preview:
        return "EMPTY_INPUT"

    if has_violations:
        return "VIOLATIONS_FOUND"

    if result_length < 50 and not has_clean and not has_violations:
        return "UNINFORMATIVE"

    if has_clean and not has_violations:
        return "CLEAN"

    # If we see checkmarks but also crosses
    if has_clean and has_violations:
        return "VIOLATIONS_FOUND"

    return "UNINFORMATIVE"


def count_violations(result_preview):
    """Count number of specific violations in a gt_check response."""
    return result_preview.count("\u2717")  # Count X marks


def map_calls_to_tasks(gt_calls, task_tool_calls, results):
    """Map GT calls to task instance_ids using tool call matching.

    Strategy: match gt_check calls by looking at which tasks had gt_check
    in their Inspect conversation trace, then correlate by order.
    """
    # Get tasks that used gt_check, in order
    tasks_with_gt_check = []
    for iid, tc_list in task_tool_calls.items():
        check_count = sum(1 for tc in tc_list if tc['function'] == 'gt_check')
        if check_count > 0:
            tasks_with_gt_check.append((iid, check_count))

    # Get gt_check calls from JSONL in order
    check_calls = [c for c in gt_calls if c['tool'] == 'gt_check']

    # Simple mapping: assign calls to tasks in order, respecting count per task
    call_to_task = {}
    call_idx = 0
    for iid, count in tasks_with_gt_check:
        for _ in range(count):
            if call_idx < len(check_calls):
                call_to_task[call_idx] = iid
                call_idx += 1

    # For unmapped calls, mark as unknown
    for i in range(len(check_calls)):
        if i not in call_to_task:
            call_to_task[i] = "UNKNOWN"

    return check_calls, call_to_task


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gt-log", required=True)
    parser.add_argument("--eval-dir", required=True)
    parser.add_argument("--baseline-dir", default="")
    parser.add_argument("--output", default="/tmp/gt_check_analysis.json")
    args = parser.parse_args()

    print("Loading GT calls...")
    gt_calls = load_gt_calls(args.gt_log)
    check_calls = [c for c in gt_calls if c['tool'] == 'gt_check']
    print(f"  Total GT calls: {len(gt_calls)}")
    print(f"  gt_check calls: {len(check_calls)}")

    print("Loading eval results...")
    results, task_tool_calls = load_eval_results(args.eval_dir)
    print(f"  Tasks with results: {len(results)}")
    total_resolved = sum(1 for v in results.values() if v)
    print(f"  Resolved: {total_resolved}/{len(results)}")

    # Classify gt_check responses
    print("\n=== GT_CHECK RESPONSE CLASSIFICATION ===")
    classifications = []
    for c in check_calls:
        cls = classify_gt_check_response(c['result_preview'], c['result_length'])
        violations = count_violations(c['result_preview']) if cls == "VIOLATIONS_FOUND" else 0
        classifications.append({
            'classification': cls,
            'violations': violations,
            'duration': c['duration_seconds'],
            'result_preview': c['result_preview'],
            'result_length': c['result_length'],
        })

    dist = Counter(c['classification'] for c in classifications)
    total = len(classifications)
    for cls, count in dist.most_common():
        print(f"  {cls}: {count} ({100*count/total:.0f}%)")

    # Map calls to tasks
    print("\n=== MAPPING CALLS TO TASKS ===")
    mapped_checks, call_to_task = map_calls_to_tasks(gt_calls, task_tool_calls, results)

    # Build task-level summary
    task_check_results = defaultdict(list)
    for i, c in enumerate(classifications):
        iid = call_to_task.get(i, "UNKNOWN")
        task_check_results[iid].append(c)

    # Determine per-task gt_check status
    task_gt_status = {}
    for iid, checks in task_check_results.items():
        if iid == "UNKNOWN":
            continue
        has_violations = any(c['classification'] == 'VIOLATIONS_FOUND' for c in checks)
        if has_violations:
            task_gt_status[iid] = "VIOLATIONS"
        else:
            # Use the last check's classification
            task_gt_status[iid] = checks[-1]['classification']

    # Build the matrix
    print("\n=== OUTCOME MATRIX ===")
    matrix = {
        'clean_resolved': 0, 'clean_failed': 0,
        'violations_resolved': 0, 'violations_failed': 0,
        'no_check_resolved': 0, 'no_check_failed': 0,
    }

    # Tasks with gt_check
    for iid, status in task_gt_status.items():
        resolved = results.get(iid, False)
        if status in ("CLEAN", "UNINFORMATIVE", "EMPTY_INPUT"):
            if resolved:
                matrix['clean_resolved'] += 1
            else:
                matrix['clean_failed'] += 1
        elif status == "VIOLATIONS":
            if resolved:
                matrix['violations_resolved'] += 1
            else:
                matrix['violations_failed'] += 1

    # Tasks without gt_check
    tasks_with_check = set(task_gt_status.keys())
    for iid, resolved in results.items():
        if iid not in tasks_with_check:
            if resolved:
                matrix['no_check_resolved'] += 1
            else:
                matrix['no_check_failed'] += 1

    A = matrix['clean_resolved']
    B = matrix['clean_failed']
    C = matrix['violations_resolved']
    D = matrix['violations_failed']
    E = matrix['no_check_resolved']
    F = matrix['no_check_failed']

    print(f"                    | RESOLVED | FAILED |")
    print(f"--------------------|----------|--------|")
    print(f"gt_check: CLEAN     |    {A:3d}   |  {B:3d}   |")
    print(f"gt_check: VIOLATIONS|    {C:3d}   |  {D:3d}   |")
    print(f"No gt_check called  |    {E:3d}   |  {F:3d}   |")

    # Resolve rates by group
    if A + B > 0:
        print(f"\nCLEAN resolve rate: {A}/{A+B} ({100*A/(A+B):.0f}%)")
    if C + D > 0:
        print(f"VIOLATIONS resolve rate: {C}/{C+D} ({100*C/(C+D):.0f}%)")
    if E + F > 0:
        print(f"No gt_check resolve rate: {E}/{E+F} ({100*E/(E+F):.0f}%)")

    # gt_impact and gt_references correlation
    print("\n=== GT_IMPACT / GT_REFERENCES CORRELATION ===")
    tasks_with_impact = set()
    tasks_with_refs = set()
    for iid, tc_list in task_tool_calls.items():
        for tc in tc_list:
            if tc['function'] == 'gt_impact':
                tasks_with_impact.add(iid)
            elif tc['function'] == 'gt_references':
                tasks_with_refs.add(iid)

    for name, task_set in [("gt_impact", tasks_with_impact), ("gt_references", tasks_with_refs)]:
        with_tool = sum(1 for iid in task_set if results.get(iid, False))
        with_tool_total = len(task_set)
        without_tool = sum(1 for iid in results if iid not in task_set and results[iid])
        without_tool_total = len(results) - with_tool_total
        if with_tool_total > 0 and without_tool_total > 0:
            print(f"{name}: {with_tool}/{with_tool_total} ({100*with_tool/with_tool_total:.0f}%) vs "
                  f"without: {without_tool}/{without_tool_total} ({100*without_tool/without_tool_total:.0f}%)")

    # Cell D examples (VIOLATIONS + FAILED)
    print("\n=== CELL D EXAMPLES (VIOLATIONS + FAILED) ===")
    cell_d_tasks = [iid for iid, status in task_gt_status.items()
                    if status == "VIOLATIONS" and not results.get(iid, False)]
    for iid in cell_d_tasks[:5]:
        checks = task_check_results[iid]
        print(f"\n  Task: {iid}")
        for c in checks:
            if c['classification'] == 'VIOLATIONS_FOUND':
                print(f"    Violations ({c['violations']}): {c['result_preview'][:300]}")

    # Cell B examples (CLEAN + FAILED)
    print("\n=== CELL B EXAMPLES (CLEAN + FAILED) ===")
    cell_b_tasks = [iid for iid, status in task_gt_status.items()
                    if status in ("CLEAN", "UNINFORMATIVE", "EMPTY_INPUT")
                    and not results.get(iid, False)]
    for iid in cell_b_tasks[:5]:
        checks = task_check_results[iid]
        print(f"\n  Task: {iid}")
        for c in checks:
            print(f"    {c['classification']}: {c['result_preview'][:200]}")

    # Save full analysis
    output = {
        'distribution': dict(dist),
        'matrix': matrix,
        'cell_d_count': len(cell_d_tasks),
        'cell_b_count': len(cell_b_tasks),
        'cell_d_tasks': cell_d_tasks,
        'cell_b_tasks': cell_b_tasks,
        'total_check_calls': len(check_calls),
        'tasks_with_check': len(tasks_with_check),
    }
    with open(args.output, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\nFull analysis saved to {args.output}")


if __name__ == "__main__":
    main()
