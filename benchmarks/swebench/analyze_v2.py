#!/usr/bin/env python3
"""V2 Pull Architecture — Post-run analysis script.

Answers:
1. How often did the agent call each tool? (usage rate)
2. How often did hooks fire vs skip? (hook rate)
3. On GAINED tasks: what tools/hooks fired? (what helped)
4. On LOST tasks: what tools/hooks fired? (what hurt)
5. On BOTH_PASS tasks: did GT stay silent? (non-interference check)
6. What was the average tokens injected per task? (noise level)
7. Localization accuracy: did agent patches touch the right files?

Usage:
    python3 -m benchmarks.swebench.analyze_v2 \\
        --predictions results/groundtruth_v2_pull/predictions.jsonl \\
        --log-dir results/groundtruth_v2_pull/gt_logs/ \\
        --baseline results/baseline/predictions.jsonl \\
        --gold-patches /path/to/gold_patches/  # optional
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load_predictions(path: str) -> dict[str, dict]:
    """Load predictions JSONL into dict keyed by instance_id."""
    preds = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            pred = json.loads(line)
            preds[pred["instance_id"]] = pred
    return preds


def load_log(log_dir: str, task_id: str) -> list[dict]:
    """Load all JSONL log entries for a task."""
    entries = []
    log_dir_path = Path(log_dir)
    for suffix in [".v2.jsonl", ".hooks.jsonl"]:
        log_path = log_dir_path / f"{task_id}{suffix}"
        if log_path.exists():
            with open(log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
    return entries


def extract_files_from_patch(patch: str) -> set[str]:
    """Extract changed file paths from a unified diff."""
    files = set()
    for match in re.finditer(r"^diff --git a/(.*?) b/", patch, re.MULTILINE):
        files.add(match.group(1))
    if not files:
        for match in re.finditer(r"^\+\+\+ b/(.*?)$", patch, re.MULTILINE):
            files.add(match.group(1))
    return files


def count_tool_calls(log: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = Counter()
    for entry in log:
        if entry.get("event_type") == "tool_call":
            counts[entry.get("tool", "unknown")] += 1
    return dict(counts)


def count_hooks_fired(log: list[dict]) -> int:
    return sum(1 for e in log if e.get("event_type") == "hook_fire")


def count_hooks_skipped(log: list[dict]) -> int:
    return sum(1 for e in log if e.get("event_type") == "hook_skip")


def sum_tokens_injected(log: list[dict]) -> int:
    total = 0
    for entry in log:
        if entry.get("event_type") == "tool_call" and entry.get("confidence") == "RESPONDED":
            total += entry.get("response_tokens", 0)
    return total


def analyze(
    predictions_path: str,
    log_dir: str,
    baseline_path: str | None = None,
    gold_dir: str | None = None,
) -> None:
    preds = load_predictions(predictions_path)
    baseline = load_predictions(baseline_path) if baseline_path else {}

    # Classify tasks
    gained = []  # resolved in v2 but not baseline
    lost = []  # resolved in baseline but not v2
    both_pass = []  # resolved in both
    both_fail = []  # failed in both

    for task_id, pred in preds.items():
        v2_resolved = bool(pred.get("model_patch", "").strip())
        base_resolved = bool(baseline.get(task_id, {}).get("model_patch", "").strip())

        if v2_resolved and not base_resolved:
            gained.append(task_id)
        elif base_resolved and not v2_resolved:
            lost.append(task_id)
        elif v2_resolved and base_resolved:
            both_pass.append(task_id)
        else:
            both_fail.append(task_id)

    print("=" * 70)
    print("V2 PULL ARCHITECTURE — POST-RUN ANALYSIS")
    print("=" * 70)
    print(f"Total tasks: {len(preds)}")
    print(f"Patched:     {sum(1 for p in preds.values() if p.get('model_patch', '').strip())}")
    if baseline:
        print(f"GAINED:      {len(gained)}")
        print(f"LOST:        {len(lost)}")
        print(f"BOTH PASS:   {len(both_pass)}")
        print(f"BOTH FAIL:   {len(both_fail)}")
        print(f"NET:         {len(gained) - len(lost):+d}")
    print()

    # Aggregate metrics
    total_tool_calls: dict[str, int] = Counter()
    total_hooks_fired = 0
    total_hooks_skipped = 0
    total_tokens = 0
    tasks_with_tool_calls = 0
    tasks_with_hooks = 0
    tasks_silent = 0

    per_task_data: list[dict] = []

    for task_id, pred in preds.items():
        log = load_log(log_dir, task_id)

        tc = count_tool_calls(log)
        hf = count_hooks_fired(log)
        hs = count_hooks_skipped(log)
        tokens = sum_tokens_injected(log)

        for tool, count in tc.items():
            total_tool_calls[tool] += count
        total_hooks_fired += hf
        total_hooks_skipped += hs
        total_tokens += tokens

        if tc:
            tasks_with_tool_calls += 1
        if hf > 0:
            tasks_with_hooks += 1
        if not tc and hf == 0:
            tasks_silent += 1

        category = "unknown"
        if task_id in gained:
            category = "GAINED"
        elif task_id in lost:
            category = "LOST"
        elif task_id in both_pass:
            category = "BOTH_PASS"
        elif task_id in both_fail:
            category = "BOTH_FAIL"

        per_task_data.append({
            "task_id": task_id,
            "category": category,
            "tool_calls": tc,
            "hooks_fired": hf,
            "hooks_skipped": hs,
            "tokens_injected": tokens,
            "patched": bool(pred.get("model_patch", "").strip()),
        })

    n = max(len(preds), 1)
    print("--- TOOL USAGE ---")
    for tool, count in sorted(total_tool_calls.items()):
        print(f"  {tool}: {count} calls across {n} tasks ({count / n:.1f}/task)")
    print(f"  Tasks with tool calls: {tasks_with_tool_calls}/{n}")
    print()

    print("--- HOOK USAGE ---")
    print(f"  Hooks fired:   {total_hooks_fired} ({total_hooks_fired / n:.1f}/task)")
    print(f"  Hooks skipped: {total_hooks_skipped}")
    print(f"  Tasks with hooks: {tasks_with_hooks}/{n}")
    print(f"  Tasks silent:     {tasks_silent}/{n}")
    print()

    print("--- TOKEN INJECTION ---")
    print(f"  Total tokens injected: {total_tokens}")
    print(f"  Avg per task: {total_tokens / n:.0f}")
    print()

    # Per-category breakdown
    if baseline:
        for category_name, task_list in [
            ("GAINED", gained),
            ("LOST", lost),
            ("BOTH_PASS", both_pass),
            ("BOTH_FAIL", both_fail),
        ]:
            if not task_list:
                continue
            cat_data = [d for d in per_task_data if d["category"] == category_name]
            cat_tools = sum(sum(d["tool_calls"].values()) for d in cat_data)
            cat_hooks = sum(d["hooks_fired"] for d in cat_data)
            cat_tokens = sum(d["tokens_injected"] for d in cat_data)
            cat_n = max(len(cat_data), 1)

            print(f"--- {category_name} ({len(task_list)} tasks) ---")
            print(f"  Avg tool calls/task: {cat_tools / cat_n:.1f}")
            print(f"  Avg hooks fired/task: {cat_hooks / cat_n:.1f}")
            print(f"  Avg tokens/task: {cat_tokens / cat_n:.0f}")

            # Top tools used in this category
            cat_tool_counts: dict[str, int] = Counter()
            for d in cat_data:
                for tool, count in d["tool_calls"].items():
                    cat_tool_counts[tool] += count
            if cat_tool_counts:
                print(f"  Tools: {dict(cat_tool_counts)}")
            print()

    # Localization accuracy (if gold patches available)
    if gold_dir:
        print("--- LOCALIZATION ACCURACY ---")
        loc_accuracies = []
        ev_precisions = []
        ev_recalls = []

        gold_path = Path(gold_dir)
        for task_id, pred in preds.items():
            agent_patch = pred.get("model_patch", "")
            if not agent_patch:
                continue

            agent_files = extract_files_from_patch(agent_patch)

            # Try to load gold patch
            gold_file = gold_path / f"{task_id}.patch"
            if not gold_file.exists():
                continue
            gold_patch = gold_file.read_text(encoding="utf-8")
            gold_files = extract_files_from_patch(gold_patch)

            if gold_files:
                loc_acc = len(agent_files & gold_files) / len(gold_files)
                loc_accuracies.append(loc_acc)

            # Check gt_locate precision/recall
            log = load_log(log_dir, task_id)
            gt_files: set[str] = set()
            for entry in log:
                if entry.get("tool") == "gt_locate" and entry.get("confidence") == "RESPONDED":
                    # Parse file paths from response (approximation)
                    response = entry.get("input", {}).get("issue_description", "")
                    # We'd need the actual response to get files, but we log input not output
                    pass

            if gt_files and gold_files:
                ev_precisions.append(len(gt_files & gold_files) / max(len(gt_files), 1))
                ev_recalls.append(len(gt_files & gold_files) / max(len(gold_files), 1))

        if loc_accuracies:
            avg_loc = sum(loc_accuracies) / len(loc_accuracies)
            print(f"  Avg localization accuracy: {avg_loc:.2f} ({len(loc_accuracies)} tasks)")
        if ev_precisions:
            avg_prec = sum(ev_precisions) / len(ev_precisions)
            avg_rec = sum(ev_recalls) / len(ev_recalls)
            print(f"  Avg evidence precision: {avg_prec:.2f}")
            print(f"  Avg evidence recall: {avg_rec:.2f}")
        print()

    # Detail dump for investigation
    print("--- PER-TASK DETAIL (top 20 by tokens) ---")
    sorted_tasks = sorted(per_task_data, key=lambda d: d["tokens_injected"], reverse=True)
    for d in sorted_tasks[:20]:
        tc_str = ", ".join(f"{k}={v}" for k, v in d["tool_calls"].items()) or "none"
        print(
            f"  {d['task_id']}: {d['category']} "
            f"tools=[{tc_str}] hooks={d['hooks_fired']} "
            f"tokens={d['tokens_injected']} patched={d['patched']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze v2 pull architecture results")
    parser.add_argument("--predictions", required=True, help="Path to predictions.jsonl")
    parser.add_argument("--log-dir", required=True, help="Path to gt_logs/ directory")
    parser.add_argument("--baseline", help="Path to baseline predictions.jsonl for flip analysis")
    parser.add_argument("--gold-patches", help="Directory with gold patch files for localization accuracy")
    args = parser.parse_args()

    analyze(
        predictions_path=args.predictions,
        log_dir=args.log_dir,
        baseline_path=args.baseline,
        gold_dir=args.gold_patches,
    )


if __name__ == "__main__":
    main()
