#!/usr/bin/env python3
"""run_paired_analysis.py — the runner that actually INVOKES compute_paired_metrics.

Task #30 R2-wiring (2026-07-29): compute_paired_metrics.py is a complete paired
analyzer (locked union + named missingness, E1 absolute_pp, E3 net tokens, seeded
bootstrap, McNemar, Holm) that nothing invoked against the real arm layouts.  Its
existing __main__ CLI (--baseline-dir/--oracle-dir/--output-dir) only understands a
single FLAT run directory per arm and writes a fixed-name gt_metrics_report.json;
it cannot walk the frozen GT-off layout or restrict to a locked task set.  This
runner is a thin wrapper that REUSES load_run / compute_paired_report /
_assert_baseline_arm_is_off — it reimplements no analysis.

Arm layouts handled:
  --on-dir   a GT-on run's artifacts dir or downloaded-artifact root: one subdir
             per task (compute_task_metrics rglobs the trajectory inside each).
  --off-dir  default = the frozen GT-off arm
             D:\\gt_runs\\gtoff_baseline_trajs\\{half0,half1}\\ll-full-<task>\\
             Half-split dirs (half0, half1, ...) are each loaded with
             compute_paired_metrics.load_run and merged; the "ll-full-" dir
             prefix is stripped so task ids pair with the on arm.

Task-set restriction (--task-set, repeatable):
  .txt  — one instance id per line, '#' comments and blanks ignored
          (format of .claude/reports/mixture_bag_30_20260712.txt)
  .json — dict form: union of every "selected_*" list (format of
          benchmarks/data/blind_ext_selection_vfinal_20260718.json — pools like
          sig_pool are NOT selections); or a bare JSON list of ids.
  The population is restricted to the locked union; a locked id absent from
  BOTH arms is reported in population.missing_tasks as absent_from_both_arms
  instead of vanishing.

Usage after a paid GT-on run's artifacts are downloaded:
  python scripts/metrics/run_paired_analysis.py \
      --on-dir <downloaded-artifact-root> \
      --task-set .claude/reports/mixture_bag_30_20260712.txt \
      --task-set benchmarks/data/blind_ext_selection_vfinal_20260718.json \
      --output-dir .claude/reports/metrics
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import compute_paired_metrics as cpm  # noqa: E402

DEFAULT_OFF_DIR = Path(r"D:\gt_runs\gtoff_baseline_trajs")
_TASK_DIR_PREFIX = "ll-full-"
_HALF_DIR_RE = re.compile(r"^half\d+$")


def _canonical_task_id(name: str) -> str:
    """Strip the harness 'll-full-' dir prefix so both arms pair on instance ids."""
    if _TASK_DIR_PREFIX in name:
        return name.split(_TASK_DIR_PREFIX, 1)[1]
    return name


def load_arm(root: Path) -> Dict[str, "cpm.TaskMetrics"]:
    """Load one arm via compute_paired_metrics.load_run (reused, not reimplemented).

    A root whose immediate subdirs are half0/half1/... (the frozen GT-off split)
    is loaded half-by-half and merged; anything else is treated as one flat run
    dir.  Task ids are canonicalised by stripping the 'll-full-' prefix.
    """
    halves = [
        d for d in sorted(root.iterdir())
        if d.is_dir() and _HALF_DIR_RE.match(d.name)
    ]
    run_dirs = halves if halves else [root]

    merged: Dict[str, cpm.TaskMetrics] = {}
    for run_dir in run_dirs:
        for raw_id, tm in cpm.load_run(run_dir).items():
            tid = _canonical_task_id(raw_id)
            if tid in merged:
                print(
                    f"[WARN] duplicate task {tid} (from {run_dir.name}/{raw_id}); "
                    f"keeping first occurrence",
                    file=sys.stderr,
                )
                continue
            tm.task_id = tid
            merged[tid] = tm
    return merged


def load_task_set(paths: Iterable[Path]) -> Set[str]:
    """Load the locked task ids from txt (line-per-id, '#' comments) and/or
    blind-selection json (union of selected_* lists, or a bare list)."""
    ids: Set[str] = set()
    for path in paths:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                ids.update(str(x) for x in data)
            elif isinstance(data, dict):
                selected_keys = [k for k in data if k.startswith("selected")]
                if not selected_keys:
                    raise ValueError(
                        f"{path}: no 'selected_*' lists found — refusing to guess "
                        f"which key holds the locked ids"
                    )
                for k in selected_keys:
                    v = data[k]
                    if isinstance(v, list):
                        ids.update(str(x) for x in v)
            else:
                raise ValueError(f"{path}: unsupported JSON shape {type(data).__name__}")
        else:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                ids.add(line)
    return ids


def _print_headline(report: dict) -> None:
    h = report["headline"]
    pop = report["population"]
    print("=== PAIRED ANALYSIS HEADLINE (baseline = GT-off, oracle = GT-on) ===")
    print(
        f"absolute_resolution_pp: {h['absolute_resolution_pp']} "
        f"(paired: on {h['resolved_oracle_paired']}/{pop['n_paired']} "
        f"vs off {h['resolved_baseline_paired']}/{pop['n_paired']})"
    )
    print(
        f"net_tokens off-on: median={h['net_tokens_median']} "
        f"mean={h['net_tokens_mean']} (n_measured={h['net_tokens_n_measured']})"
    )
    print(f"flips: {h['flip_count']}  regressions: {h['regression_count']}")
    print(
        f"population: union={pop['n_union']} paired={pop['n_paired']} "
        f"off_only={pop['n_baseline_only']} on_only={pop['n_oracle_only']}"
    )
    task_set = pop.get("task_set")
    if task_set:
        print(
            f"locked task set: n_locked={task_set['n_locked']} "
            f"missing_from_both_arms={task_set['missing_from_both_arms']}"
        )
    elif pop["missing_tasks"]:
        print(f"missing tasks: {sorted(pop['missing_tasks'])}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the paired GT-on vs GT-off analysis "
            "(wraps compute_paired_metrics.compute_paired_report)."
        ),
    )
    parser.add_argument(
        "--on-dir", required=True,
        help="GT-on run artifacts dir or downloaded-artifact root (one subdir per task)",
    )
    parser.add_argument(
        "--off-dir", default=str(DEFAULT_OFF_DIR),
        help=f"GT-off arm root (default: frozen {DEFAULT_OFF_DIR})",
    )
    parser.add_argument(
        "--task-set", action="append", default=[],
        help="Locked task-id file (.txt line-per-id or blind-selection .json); repeatable",
    )
    parser.add_argument(
        "--output-dir", default=".",
        help="Directory to write paired_report_<timestamp>.json (default: cwd)",
    )
    args = parser.parse_args(argv)

    on_dir = Path(args.on_dir).resolve()
    off_dir = Path(args.off_dir).resolve()
    if not on_dir.is_dir():
        print(f"[ERROR] --on-dir does not exist: {on_dir}", file=sys.stderr)
        return 1
    if not off_dir.is_dir():
        print(f"[ERROR] --off-dir does not exist: {off_dir}", file=sys.stderr)
        return 1
    # Reuse the analyzer's own arm guard: refuse a GT-on dir posing as the off arm.
    cpm._assert_baseline_arm_is_off(off_dir)

    print(f"[INFO] Loading GT-off arm (baseline): {off_dir}", file=sys.stderr)
    off_run = load_arm(off_dir)
    print(f"[INFO] Loading GT-on arm (oracle):   {on_dir}", file=sys.stderr)
    on_run = load_arm(on_dir)
    if not off_run:
        print("[ERROR] no tasks loaded from --off-dir", file=sys.stderr)
        return 1
    if not on_run:
        print("[ERROR] no tasks loaded from --on-dir", file=sys.stderr)
        return 1

    task_set_files = [Path(p) for p in args.task_set]
    locked_ids: Optional[Set[str]] = None
    if task_set_files:
        for p in task_set_files:
            if not p.is_file():
                print(f"[ERROR] --task-set file not found: {p}", file=sys.stderr)
                return 1
        locked_ids = load_task_set(task_set_files)
        print(f"[INFO] Locked task set: {len(locked_ids)} ids", file=sys.stderr)
        off_run = {t: m for t, m in off_run.items() if t in locked_ids}
        on_run = {t: m for t, m in on_run.items() if t in locked_ids}
        if not off_run and not on_run:
            print("[ERROR] no arm task overlaps the locked task set", file=sys.stderr)
            return 1

    report = cpm.compute_paired_report(
        baseline_run=off_run,
        oracle_run=on_run,
        baseline_run_id=off_dir.name,
        oracle_run_id=on_dir.name,
    )

    if locked_ids is not None:
        # A locked id absent from BOTH arms never enters compute_paired_report's
        # union, so it would silently vanish; name it in the population block.
        missing_both = sorted(locked_ids - set(off_run) - set(on_run))
        for tid in missing_both:
            report["population"]["missing_tasks"][tid] = {
                "in_baseline": False,
                "in_oracle": False,
                "missing_reason": "absent_from_both_arms",
            }
        report["population"]["task_set"] = {
            "files": [str(p) for p in task_set_files],
            "n_locked": len(locked_ids),
            "missing_from_both_arms": missing_both,
        }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"paired_report_{stamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"[INFO] Written: {out_path}", file=sys.stderr)

    _print_headline(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
