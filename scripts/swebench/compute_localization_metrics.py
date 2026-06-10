"""Compute localization metrics + gap audit for a GT/baseline run.

Implements the rubric in benchmarks/swebench/localization_metrics.md:
1. Median time-to-first-gold-edit (lower is better)
2. Avg edit precision (gold edits / total edits)
3. Avg gold-file coverage (unique gold files touched / gold files needed)

Plus gap audit:
- Tasks with missing brief.txt (GT arms only)
- Tasks with missing _pretask.jsonl (GT arms only)
- Tasks with infra deaths (UnixHTTPConnectionPool / timeout errors)
- Tasks with empty patches (different from infra death)
- Tasks where the bundle ran but produced no signal

Usage:
    python compute_localization_metrics.py --run-dir <out_dir> --gt-logs <gt_logs_dir> \\
        --dataset SWE-bench-Live/SWE-bench-Live --split lite --arm-name v7_full_n50

Writes:
    <run_dir>/localization_report.md
    <run_dir>/localization_report.json
    <run_dir>/per_task.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

EDIT_ACTIONS = {"edit", "write", "create", "str_replace", "insert", "edit_file", "str_replace_editor"}
INFRA_ERROR_PATTERNS = (
    "UnixHTTPConnectionPool",
    "Read timed out",
    "BrokenPipeError",
    "ConnectionResetError",
    "docker.errors.APIError",
)


def load_output_jsonl(path: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = rec.get("instance_id") or (rec.get("instance") or {}).get("instance_id")
            if tid:
                out[tid] = rec
    return out


def gold_files(gold_patch: str) -> set[str]:
    files: set[str] = set()
    for ln in (gold_patch or "").splitlines():
        if ln.startswith("diff --git "):
            parts = ln.split()
            if len(parts) >= 3:
                files.add(parts[2].lstrip("a/"))
    return files


def patch_files(rec: dict[str, Any]) -> set[str]:
    p = (rec.get("test_result") or {}).get("git_patch") or rec.get("git_patch") or ""
    return gold_files(p)


def has_patch(rec: dict[str, Any]) -> bool:
    p = (rec.get("test_result") or {}).get("git_patch") or rec.get("git_patch") or ""
    return bool((p or "").strip())


def classify_failure(rec: dict[str, Any]) -> str:
    err = rec.get("error") or rec.get("error_message") or ""
    if isinstance(err, dict):
        err = json.dumps(err)
    err = str(err)
    if any(p in err for p in INFRA_ERROR_PATTERNS):
        return "infra_died"
    if has_patch(rec):
        return "has_patch"
    if err:
        return "errored"
    return "no_patch"


def history_edit_steps(rec: dict[str, Any]) -> list[tuple[int, str, str]]:
    """Walk agent history, return (step_idx, action_type, normalized_path) for edit-class actions."""
    steps: list[tuple[int, str, str]] = []
    history = rec.get("history") or rec.get("test_result", {}).get("history") or []
    if not isinstance(history, list):
        return steps
    for idx, ev in enumerate(history):
        if not isinstance(ev, dict):
            continue
        atype = (ev.get("action") or "").lower()
        if atype not in EDIT_ACTIONS:
            args = ev.get("args") or {}
            atype = (args.get("action") or "").lower() if isinstance(args, dict) else ""
        if atype not in EDIT_ACTIONS:
            continue
        args = ev.get("args") or {}
        path = ""
        if isinstance(args, dict):
            path = args.get("path") or args.get("file") or args.get("file_path") or ""
        if not path:
            path = ev.get("path") or ev.get("file") or ""
        if path:
            steps.append((idx, atype, str(path).replace("\\", "/").lstrip("./")))
    return steps


def time_to_first_gold(steps: list[tuple[int, str, str]], gold: set[str]) -> int | None:
    if not gold:
        return None
    for idx, _atype, path in steps:
        if any(path == g or path.endswith("/" + g) or g.endswith("/" + path) for g in gold):
            return idx
    return None


def edit_precision(steps: list[tuple[int, str, str]], gold: set[str]) -> float | None:
    if not steps:
        return None
    if not gold:
        return None
    hits = sum(
        1 for _i, _a, p in steps
        if any(p == g or p.endswith("/" + g) or g.endswith("/" + p) for g in gold)
    )
    return hits / len(steps)


def gold_coverage(steps: list[tuple[int, str, str]], gold: set[str]) -> float | None:
    if not gold:
        return None
    edited = {p for _i, _a, p in steps}
    hit = sum(
        1 for g in gold
        if any(p == g or p.endswith("/" + g) or g.endswith("/" + p) for p in edited)
    )
    return hit / len(gold)


def load_gold_patches(dataset: str, split: str, instance_ids: set[str]) -> dict[str, str]:
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        print("WARN: datasets package not available, skipping gold-patch lookup", file=sys.stderr)
        return {}
    try:
        ds = load_dataset(dataset, split=split)
    except Exception as e:
        print(f"WARN: failed to load dataset {dataset} split={split}: {e}", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for row in ds:  # type: ignore[union-attr]
        if not isinstance(row, dict):
            continue
        iid = row.get("instance_id")
        if iid in instance_ids:
            out[iid] = row.get("patch") or ""
    return out


def gap_audit(
    records: dict[str, dict[str, Any]],
    gt_logs_dir: str | None,
    expected_ids: list[str],
) -> dict[str, Any]:
    """Per-task artifact presence audit."""
    has_brief: dict[str, bool] = {}
    has_pretask: dict[str, bool] = {}
    if gt_logs_dir and os.path.isdir(gt_logs_dir):
        for tid in expected_ids:
            has_brief[tid] = os.path.exists(os.path.join(gt_logs_dir, f"{tid}_brief.txt"))
            has_pretask[tid] = os.path.exists(os.path.join(gt_logs_dir, f"{tid}_pretask.jsonl"))
    else:
        for tid in expected_ids:
            has_brief[tid] = False
            has_pretask[tid] = False

    counters = {
        "expected": len(expected_ids),
        "in_output_jsonl": sum(1 for tid in expected_ids if tid in records),
        "missing_from_output_jsonl": sum(1 for tid in expected_ids if tid not in records),
        "infra_died": 0,
        "has_patch": 0,
        "no_patch": 0,
        "errored": 0,
        "missing_brief": 0,
        "missing_pretask": 0,
        "double_gap_brief_and_died": 0,
    }
    for tid in expected_ids:
        rec = records.get(tid)
        if rec:
            cls = classify_failure(rec)
            counters[cls] = counters.get(cls, 0) + 1
            if not has_brief.get(tid, False):
                counters["missing_brief"] += 1
                if cls == "infra_died":
                    counters["double_gap_brief_and_died"] += 1
        else:
            counters["missing_brief"] += 1
            counters["missing_pretask"] += 1
        if not has_pretask.get(tid, False):
            counters["missing_pretask"] += 1
    return {"counters": counters, "has_brief": has_brief, "has_pretask": has_pretask}


def aggregate(values: list[float | None]) -> dict[str, float | None]:
    nums = [v for v in values if v is not None]
    if not nums:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "n": len(nums),
        "mean": round(statistics.mean(nums), 4),
        "median": round(statistics.median(nums), 4),
        "min": round(min(nums), 4),
        "max": round(max(nums), 4),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True, help="Directory containing the OH run output")
    p.add_argument("--gt-logs", default=None, help="GT logs dir (gt_logs/) for brief.txt + _pretask.jsonl")
    p.add_argument("--dataset", default="SWE-bench-Live/SWE-bench-Live", help="HF dataset name")
    p.add_argument("--split", default="lite", help="Dataset split")
    p.add_argument("--arm-name", default="arm", help="Label for this arm in the report")
    p.add_argument("--instance-ids-file", default=None, help="Optional file listing expected instance_ids")
    p.add_argument("--out-dir", default=None, help="Output directory (default: <run-dir>)")
    args = p.parse_args()

    out_dir = Path(args.out_dir or args.run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    output_jsonls = sorted(glob.glob(os.path.join(args.run_dir, "**", "output.jsonl"), recursive=True))
    # Only filter out "killed" subdirs (half-started runs); "corrupt" is allowed in path.
    output_jsonls = [p for p in output_jsonls if ".killed" not in p]
    if not output_jsonls:
        print(f"ERROR: no output.jsonl found under {args.run_dir}", file=sys.stderr)
        return 2
    output_path = output_jsonls[0]
    print(f"Loading output from: {output_path}")
    records = load_output_jsonl(output_path)
    print(f"  records: {len(records)}")

    if args.instance_ids_file and os.path.exists(args.instance_ids_file):
        expected = [ln.strip() for ln in open(args.instance_ids_file) if ln.strip()]
    else:
        expected = sorted(records.keys())
    print(f"  expected tasks: {len(expected)}")

    audit = gap_audit(records, args.gt_logs, expected)

    gold = load_gold_patches(args.dataset, args.split, set(expected))
    print(f"  gold patches loaded: {len(gold)}")

    per_task = []
    metrics: dict[str, list[float | None]] = {"first_gold": [], "precision": [], "coverage": []}
    for tid in expected:
        rec = records.get(tid) or {}
        cls = classify_failure(rec) if rec else "missing"
        gp = gold.get(tid) or ""
        gold_set = gold_files(gp)
        steps = history_edit_steps(rec) if rec else []
        ftg = time_to_first_gold(steps, gold_set)
        prec = edit_precision(steps, gold_set)
        cov = gold_coverage(steps, gold_set)
        metrics["first_gold"].append(ftg)
        metrics["precision"].append(prec)
        metrics["coverage"].append(cov)
        per_task.append({
            "task_id": tid,
            "classification": cls,
            "has_brief": audit["has_brief"].get(tid, False),
            "has_pretask": audit["has_pretask"].get(tid, False),
            "gold_files": ";".join(sorted(gold_set)),
            "gold_count": len(gold_set),
            "edit_steps": len(steps),
            "first_gold_step": ftg if ftg is not None else "",
            "edit_precision": round(prec, 4) if prec is not None else "",
            "gold_coverage": round(cov, 4) if cov is not None else "",
        })

    csv_path = out_dir / "per_task.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(per_task[0].keys()))
        w.writeheader()
        w.writerows(per_task)

    fg_agg = aggregate(metrics["first_gold"])
    pr_agg = aggregate(metrics["precision"])
    cov_agg = aggregate(metrics["coverage"])

    summary = {
        "arm": args.arm_name,
        "run_dir": args.run_dir,
        "tasks_expected": len(expected),
        "tasks_in_output": len(records),
        "audit": audit["counters"],
        "metrics": {
            "first_gold_step": fg_agg,
            "edit_precision": pr_agg,
            "gold_coverage": cov_agg,
        },
        "per_task_csv": str(csv_path),
    }
    json_path = out_dir / "localization_report.json"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    md = [f"# Localization Report — {args.arm_name}", ""]
    md.append(f"Run dir: `{args.run_dir}`")
    md.append("")
    md.append("## Gap audit (no gaps = good)")
    md.append("")
    c = audit["counters"]
    md.append(f"| Metric | Value |")
    md.append(f"|---|---|")
    md.append(f"| Tasks expected | {c['expected']} |")
    md.append(f"| In output.jsonl | {c['in_output_jsonl']} |")
    md.append(f"| Missing from output.jsonl | {c['missing_from_output_jsonl']} |")
    md.append(f"| has_patch | {c.get('has_patch',0)} |")
    md.append(f"| no_patch | {c.get('no_patch',0)} |")
    md.append(f"| errored | {c.get('errored',0)} |")
    md.append(f"| **infra_died** | **{c['infra_died']}** |")
    md.append(f"| Missing brief.txt | {c['missing_brief']} |")
    md.append(f"| Missing _pretask.jsonl | {c['missing_pretask']} |")
    md.append(f"| Double gap (no brief AND infra_died) | {c['double_gap_brief_and_died']} |")
    md.append("")
    md.append("## Localization metrics")
    md.append("")
    md.append("| Metric | n | mean | median | min | max |")
    md.append("|---|---|---|---|---|---|")
    for label, key in (("Time-to-first-gold-edit (steps, lower=better)", "first_gold_step"),
                        ("Edit precision (higher=better)", "edit_precision"),
                        ("Gold-file coverage (higher=better)", "gold_coverage")):
        a = summary["metrics"][key]
        md.append(f"| {label} | {a['n']} | {a['mean']} | {a['median']} | {a['min']} | {a['max']} |")
    md.append("")
    md.append(f"Per-task table: `{csv_path.name}`")
    md_path = out_dir / "localization_report.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    print()
    print("\n".join(md))
    print()
    print(f"Wrote: {json_path}")
    print(f"Wrote: {md_path}")
    print(f"Wrote: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
