"""Offline conversion classifier for GT trajectory analysis.

Reads output.jsonl from completed runs, computes behavioral metrics,
and classifies silent windows into:
  CONVERTED — agent edited/tested after GT context
  PRODUCTIVE_SILENT — agent exploring new ground, GT correctly quiet
  DELAYED_CONVERSION — slow but eventual progress
  FAILED_CONVERSION — GT context not converted, agent stuck

Usage:
    python scripts/analysis/conversion_classifier.py <artifacts_dir>

Example:
    python scripts/analysis/conversion_classifier.py .tmp_analysis/run11
"""
from __future__ import annotations

import json
import glob
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BehavioralMetrics:
    """Behavioral metrics computed from a sliding window of agent actions."""
    new_file_rate: float = 0.0
    edit_velocity: int = 0
    test_after_edit: bool = False
    candidate_coverage: float = 0.0
    repeated_read_ratio: float = 0.0
    search_without_edit: int = 0
    actions_since_source_edit: int = 999
    actions_since_last_gt: int = 999
    explicit_stuck_signal: bool = False
    understanding_tests: int = 0


@dataclass
class ActionRecord:
    num: int
    kind: str  # read, edit, run, run_test, think, finish, gt_injection
    detail: str = ""
    is_source_edit: bool = False
    is_new_file: bool = False
    is_repeated_read: bool = False
    is_search: bool = False
    is_stuck_think: bool = False


@dataclass
class SilentWindow:
    start: int
    end: int
    duration: int
    classification: str = "UNKNOWN"
    edits_in: int = 0
    tests_in: int = 0
    new_reads: int = 0
    repeated_reads: int = 0
    stuck_thinks: int = 0
    metrics_at_start: BehavioralMetrics = field(default_factory=BehavioralMetrics)


STUCK_PHRASES = [
    "going in circles", "reconsider", "different approach", "step back",
    "fresh look", "take a completely", "totally different", "re-read",
    "re-think", "from scratch", "different angle", "different perspective",
]


def parse_trajectory(output_jsonl_path: str, task_id: str) -> list[ActionRecord]:
    """Parse output.jsonl into a list of ActionRecords."""
    with open(output_jsonl_path, encoding="utf-8", errors="replace") as f:
        data = json.loads(f.readline())

    history = data.get("history", [])
    records: list[ActionRecord] = []
    read_counts: dict[str, int] = {}
    seen_files: set[str] = set()
    action_num = 0

    for entry in history:
        act = entry.get("action", "")
        obs = entry.get("observation", "")
        args = entry.get("args", {}) or {}
        content = str(entry.get("content", "") or "")

        if act not in ("read", "edit", "run", "think", "finish"):
            # Check for GT injection in observations
            if obs and ("[GT]" in content or "Confirmed" in content
                        or "[CONTRACT" in content or "[PATTERN" in content):
                action_num += 1
                records.append(ActionRecord(action_num, "gt_injection", content[:200]))
            continue

        action_num += 1

        if act == "read":
            p = args.get("path", "").split(task_id + "/")[-1]
            read_counts[p] = read_counts.get(p, 0) + 1
            is_new = p not in seen_files
            seen_files.add(p)
            records.append(ActionRecord(
                action_num, "read", p,
                is_new_file=is_new,
                is_repeated_read=read_counts[p] > 1,
            ))

        elif act == "edit":
            p = args.get("path", "").split(task_id + "/")[-1]
            is_source = (
                "test" not in p.lower()
                and "TASKS" not in p
                and "/tmp/" not in args.get("path", "")
            )
            records.append(ActionRecord(action_num, "edit", p, is_source_edit=is_source))

        elif act == "run":
            cmd = args.get("command", "")
            is_test = "pytest" in cmd or "python -m pytest" in cmd
            is_search = "grep" in cmd or "find" in cmd
            records.append(ActionRecord(
                action_num,
                "run_test" if is_test else "run",
                cmd[:80],
                is_search=is_search,
            ))

        elif act == "think":
            thought = args.get("thought", "")
            is_stuck = any(p in thought.lower() for p in STUCK_PHRASES)
            records.append(ActionRecord(
                action_num, "think", thought[:100],
                is_stuck_think=is_stuck,
            ))

        elif act == "finish":
            records.append(ActionRecord(action_num, "finish"))

    return records


def compute_metrics_at(records: list[ActionRecord], position: int, window: int = 15) -> BehavioralMetrics:
    """Compute behavioral metrics at a given position using a sliding window."""
    window_records = [r for r in records if position - window < r.num <= position]
    all_before = [r for r in records if r.num <= position]

    reads_in_window = [r for r in window_records if r.kind == "read"]
    new_reads = sum(1 for r in reads_in_window if r.is_new_file)
    repeated_reads = sum(1 for r in reads_in_window if r.is_repeated_read)
    total_reads = len(reads_in_window)

    source_edits_in_window = [r for r in window_records if r.kind == "edit" and r.is_source_edit]
    tests_in_window = [r for r in window_records if r.kind == "run_test"]

    last_source_edit = max(
        (r.num for r in all_before if r.kind == "edit" and r.is_source_edit),
        default=0,
    )
    last_gt = max(
        (r.num for r in all_before if r.kind == "gt_injection"),
        default=0,
    )

    # test_after_edit: test within 10 actions of last source edit
    test_after = any(
        r.kind == "run_test" and last_source_edit > 0 and r.num - last_source_edit <= 10
        for r in window_records
    )

    # understanding_tests: tests with no preceding source edit
    understanding_tests = sum(
        1 for r in window_records
        if r.kind == "run_test" and last_source_edit == 0
    )

    searches_since_edit = sum(
        1 for r in all_before
        if r.is_search and r.num > last_source_edit
    )

    stuck_thinks = sum(1 for r in all_before if r.is_stuck_think)

    return BehavioralMetrics(
        new_file_rate=new_reads / max(len(window_records), 1),
        edit_velocity=len(source_edits_in_window),
        test_after_edit=test_after,
        candidate_coverage=0.0,  # needs brief_candidates, computed externally
        repeated_read_ratio=repeated_reads / max(total_reads, 1),
        search_without_edit=searches_since_edit,
        actions_since_source_edit=position - last_source_edit if last_source_edit > 0 else 999,
        actions_since_last_gt=position - last_gt if last_gt > 0 else 999,
        explicit_stuck_signal=stuck_thinks > 0,
        understanding_tests=understanding_tests,
    )


def classify_window(window: SilentWindow) -> str:
    """Classify a silent window using behavioral signals."""
    m = window.metrics_at_start

    # A: Converted — agent is editing or testing after edits
    if window.edits_in > 0 or (window.tests_in > 0 and m.edit_velocity > 0):
        return "PRODUCTIVE_SILENT"

    # A2: Post-edit validation — agent recently edited and is now testing/verifying
    if m.actions_since_source_edit < 30 and m.actions_since_source_edit != 999 and window.tests_in > 0:
        return "POST_EDIT_VALIDATION"

    # B: Productive exploration — reading new files, not spinning
    if m.new_file_rate > 0.3 and m.repeated_read_ratio < 0.2 and not m.explicit_stuck_signal:
        return "PRODUCTIVE_SILENT"

    # C: Failed conversion — multi-signal stuck detection
    stuck_signals = 0
    if m.edit_velocity == 0:
        stuck_signals += 1
    if m.repeated_read_ratio > 0.3:
        stuck_signals += 1
    if m.search_without_edit > 10:
        stuck_signals += 1
    if m.actions_since_last_gt > 20:
        stuck_signals += 1
    if m.new_file_rate < 0.1:
        stuck_signals += 1
    if m.explicit_stuck_signal:
        stuck_signals += 1
    if m.understanding_tests > 3 and m.edit_velocity == 0:
        stuck_signals += 1

    if stuck_signals >= 3:
        return "FAILED_CONVERSION"

    # D: Delayed conversion — slow but some progress
    if m.edit_velocity == 0 and m.new_file_rate > 0.1 and window.duration > 15:
        return "DELAYED_CONVERSION"

    # E: Post-edit validation
    if m.actions_since_source_edit < 20 and window.tests_in > 0:
        return "POST_EDIT_VALIDATION"

    return "PRODUCTIVE_SILENT"


def find_silent_windows(records: list[ActionRecord], min_gap: int = 8) -> list[SilentWindow]:
    """Find windows where GT is silent for more than min_gap actions."""
    gt_actions = [r.num for r in records if r.kind == "gt_injection"]
    if not records:
        return []

    max_action = max(r.num for r in records)
    boundaries = [0] + gt_actions + [max_action]
    windows = []

    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i + 1]
        if end - start >= min_gap:
            w = SilentWindow(start=start, end=end, duration=end - start)

            # Count events in window
            for r in records:
                if r.num <= start or r.num > end:
                    continue
                if r.kind == "edit" and r.is_source_edit:
                    w.edits_in += 1
                if r.kind == "run_test":
                    w.tests_in += 1
                if r.kind == "read" and r.is_new_file:
                    w.new_reads += 1
                if r.kind == "read" and r.is_repeated_read:
                    w.repeated_reads += 1
                if r.is_stuck_think:
                    w.stuck_thinks += 1

            w.metrics_at_start = compute_metrics_at(records, end, window=end - start)
            w.classification = classify_window(w)
            windows.append(w)

    return windows


def analyze_task(artifacts_dir: str, task_id: str) -> dict:
    """Full analysis for one task."""
    output_files = glob.glob(
        f"{artifacts_dir}/task-{task_id}/results/*/CodeActAgent/*/output.jsonl"
    )
    if not output_files:
        return {"task": task_id, "error": "no output.jsonl"}

    records = parse_trajectory(output_files[0], task_id)
    windows = find_silent_windows(records)

    resolved = "?"
    eval_file = f"{artifacts_dir}/task-{task_id}/eval_result.json"
    if os.path.exists(eval_file):
        try:
            d = json.load(open(eval_file))
            k = list(d.keys())[0]
            resolved = d[k].get("resolved", d.get("resolved", "?"))
        except Exception:
            pass

    total_actions = max((r.num for r in records), default=0)
    source_edits = sum(1 for r in records if r.kind == "edit" and r.is_source_edit)
    test_runs = sum(1 for r in records if r.kind == "run_test")
    gt_injections = [r.num for r in records if r.kind == "gt_injection"]
    first_source_edit = next(
        (r.num for r in records if r.kind == "edit" and r.is_source_edit), None
    )
    first_test = next((r.num for r in records if r.kind == "run_test"), None)
    stuck_thinks = sum(1 for r in records if r.is_stuck_think)

    return {
        "task": task_id,
        "resolved": resolved,
        "action_count": total_actions,
        "first_source_edit": first_source_edit,
        "first_test": first_test,
        "source_edits": source_edits,
        "test_runs": test_runs,
        "gt_injections": len(gt_injections),
        "gt_steps": gt_injections,
        "last_gt": gt_injections[-1] if gt_injections else 0,
        "actions_since_last_gt": total_actions - (gt_injections[-1] if gt_injections else 0),
        "stuck_thinks": stuck_thinks,
        "windows": windows,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python conversion_classifier.py <artifacts_dir>")
        sys.exit(1)

    artifacts_dir = sys.argv[1]
    tasks = [
        d.replace("task-", "")
        for d in os.listdir(artifacts_dir)
        if d.startswith("task-") and os.path.isdir(os.path.join(artifacts_dir, d))
    ]

    if not tasks:
        print(f"No task-* directories found in {artifacts_dir}")
        sys.exit(1)

    all_results = []
    for task_id in sorted(tasks):
        result = analyze_task(artifacts_dir, task_id)
        all_results.append(result)

        if "error" in result:
            print(f"=== {task_id}: {result['error']} ===\n")
            continue

        print(f"=== {result['task']} (resolved={result['resolved']}, {result['action_count']} actions) ===")
        print(f"  first_source_edit:     {result['first_source_edit']}")
        print(f"  first_test:            {result['first_test']}")
        print(f"  source_edits:          {result['source_edits']}")
        print(f"  test_runs:             {result['test_runs']}")
        print(f"  gt_injections:         {result['gt_injections']} at {result['gt_steps']}")
        print(f"  actions_since_last_gt: {result['actions_since_last_gt']}")
        print(f"  stuck_thinks:          {result['stuck_thinks']}")
        print(f"  SILENT WINDOWS:")
        for w in result["windows"]:
            print(f"    A{w.start}-A{w.end} ({w.duration} actions): {w.classification}")
            m = w.metrics_at_start
            print(f"      edits={w.edits_in} tests={w.tests_in} new_reads={w.new_reads} "
                  f"repeated={w.repeated_reads} stuck_thinks={w.stuck_thinks}")
            print(f"      metrics: new_file_rate={m.new_file_rate:.2f} edit_vel={m.edit_velocity} "
                  f"repeated_ratio={m.repeated_read_ratio:.2f} search_no_edit={m.search_without_edit} "
                  f"since_gt={m.actions_since_last_gt} stuck={m.explicit_stuck_signal}")
        print()

    # Summary
    total_productive = sum(
        sum(1 for w in r["windows"] if w.classification == "PRODUCTIVE_SILENT")
        for r in all_results if "windows" in r
    )
    total_delayed = sum(
        sum(1 for w in r["windows"] if w.classification == "DELAYED_CONVERSION")
        for r in all_results if "windows" in r
    )
    total_failed = sum(
        sum(1 for w in r["windows"] if w.classification == "FAILED_CONVERSION")
        for r in all_results if "windows" in r
    )

    print("=== SUMMARY ===")
    print(f"  PRODUCTIVE_SILENT windows: {total_productive}")
    print(f"  DELAYED_CONVERSION windows: {total_delayed}")
    print(f"  FAILED_CONVERSION windows: {total_failed}")
    print(f"  Rescue would fire in: {total_failed} windows across "
          f"{sum(1 for r in all_results if 'windows' in r and any(w.classification == 'FAILED_CONVERSION' for w in r['windows']))} tasks")
    print(f"  Productive tasks correctly left quiet: "
          f"{sum(1 for r in all_results if 'windows' in r and all(w.classification in ('PRODUCTIVE_SILENT', 'POST_EDIT_VALIDATION') for w in r['windows']))}")


if __name__ == "__main__":
    main()
