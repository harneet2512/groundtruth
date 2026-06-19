#!/usr/bin/env python3
"""OpenHands Evidence Delivery Smoke Gate — Report Generator.

Reads delivery_telemetry.jsonl and optional eval/metadata files,
produces per_task_summary.csv, config_diff.md, and FINAL_REPORT.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ARM_ORDER = ["baseline", "gt_v11", "gt_v13"]
ARM_LABELS = {"baseline": "baseline", "gt_v11": "GT v1.1", "gt_v13": "GT v1.3"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _pad_table(rows: list[list[str]]) -> str:
    """Render a list-of-lists as a properly aligned markdown table."""
    if not rows:
        return ""
    ncols = len(rows[0])
    widths = [0] * ncols
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt(row: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(row, widths)) + " |"

    lines = [_fmt(rows[0])]
    lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for row in rows[1:]:
        lines.append(_fmt(row))
    return "\n".join(lines)


def _ratio(num: int, den: int) -> str:
    return f"{num}/{den}"


def _pct(num: int, den: int) -> str:
    if den == 0:
        return "0%"
    return f"{num * 100 // den}%"


# ---------------------------------------------------------------------------
# per_task_summary.csv
# ---------------------------------------------------------------------------

def write_per_task_csv(records: list[dict[str, Any]], out_dir: Path) -> Path:
    path = out_dir / "per_task_summary.csv"
    fieldnames = [
        "task_id", "arm", "gt_version",
        "evidence_generated", "evidence_injected", "evidence_visible",
        "evidence_tokens", "evidence_families",
        "hook_fire_count", "hook_error_count", "errors",
    ]
    sorted_records = sorted(records, key=lambda r: (r.get("task_id", ""), r.get("arm", "")))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for rec in sorted_records:
            row = {k: rec.get(k, "") for k in fieldnames}
            # Rename field if needed
            if "evidence_visible_in_trajectory" in rec and "evidence_visible" not in rec:
                row["evidence_visible"] = rec["evidence_visible_in_trajectory"]
            writer.writerow(row)
    return path


# ---------------------------------------------------------------------------
# config_diff.md
# ---------------------------------------------------------------------------

def write_config_diff(meta_files: dict[str, Path | None], out_dir: Path) -> Path | None:
    metas: dict[str, dict[str, Any]] = {}
    for arm, p in meta_files.items():
        data = _load_json(p)
        if data is not None:
            metas[arm] = data
    if not metas:
        return None

    all_keys: list[str] = []
    seen: set[str] = set()
    for d in metas.values():
        for k in d:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    arms_present = [a for a in ARM_ORDER if a in metas]
    header = ["Field"] + arms_present
    rows: list[list[str]] = [header]
    for key in all_keys:
        row = [key]
        for arm in arms_present:
            row.append(str(metas[arm].get(key, "-")))
        rows.append(row)

    path = out_dir / "config_diff.md"
    content = "## Config Comparison\n\n" + _pad_table(rows) + "\n"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Evidence summary table
# ---------------------------------------------------------------------------

def _evidence_summary_table(records: list[dict[str, Any]]) -> str:
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_arm.setdefault(r.get("arm", "unknown"), []).append(r)

    header = ["Arm", "Generated", "Injected", "Visible", "Avg Tokens", "Families"]
    rows: list[list[str]] = [header]
    for arm in ARM_ORDER:
        tasks = by_arm.get(arm, [])
        if not tasks:
            continue
        n = len(tasks)
        gen = sum(1 for t in tasks if t.get("evidence_generated"))
        inj = sum(1 for t in tasks if t.get("evidence_injected"))
        vis = sum(
            1 for t in tasks
            if t.get("evidence_visible_in_trajectory") or t.get("evidence_visible")
        )
        avg_tok = sum(t.get("evidence_tokens", 0) for t in tasks) // max(n, 1)
        families: set[str] = set()
        for t in tasks:
            fam = t.get("evidence_families")
            if isinstance(fam, list):
                families.update(fam)
            elif isinstance(fam, str) and fam:
                families.update(f.strip() for f in fam.split(","))
        rows.append([
            arm,
            _ratio(gen, n),
            _ratio(inj, n),
            _ratio(vis, n),
            str(avg_tok),
            ", ".join(sorted(families)) if families else "-",
        ])
    return _pad_table(rows)


# ---------------------------------------------------------------------------
# Evaluation section
# ---------------------------------------------------------------------------

def _eval_section(eval_files: dict[str, dict[str, Any] | None]) -> str:
    present = {a: d for a, d in eval_files.items() if d is not None}
    if not present:
        return ""

    lines: list[str] = ["## Evaluation Results\n"]

    # Summary table
    header = ["Arm", "Resolved", "Total", "Rate"]
    rows: list[list[str]] = [header]
    resolved_ids: dict[str, set[str]] = {}
    for arm in ARM_ORDER:
        data = present.get(arm)
        if data is None:
            continue
        res = len(data.get("resolved_ids", []))
        total = data.get("total_instances", 0)
        resolved_ids[arm] = set(data.get("resolved_ids", []))
        rows.append([arm, _ratio(res, total), str(total), _pct(res, total)])
    lines.append(_pad_table(rows))

    # Task-level flips
    all_tasks: set[str] = set()
    for ids in resolved_ids.values():
        all_tasks.update(ids)
    # Also include tasks from any arm's completed list
    for data in present.values():
        if data and "completed_instances" in data:
            completed = data["completed_instances"]
            if isinstance(completed, list):
                all_tasks.update(completed)

    if all_tasks:
        lines.append("\n### Task-Level Flips")
        flip_header = ["Task", "Baseline", "GT v1.1", "GT v1.3"]
        flip_rows: list[list[str]] = [flip_header]
        for task in sorted(all_tasks):
            row = [task]
            for arm in ARM_ORDER:
                ids = resolved_ids.get(arm, set())
                row.append("X" if task in ids else "-")
            flip_rows.append(row)
        lines.append(_pad_table(flip_rows))

    # v1.3 vs v1.1 delta
    v11 = resolved_ids.get("gt_v11", set())
    v13 = resolved_ids.get("gt_v13", set())
    if v11 or v13:
        gained = sorted(v13 - v11)
        lost = sorted(v11 - v13)
        net = len(gained) - len(lost)
        lines.append("\n### v1.3 vs v1.1 Delta")
        lines.append(f"- Gained: {', '.join(gained) if gained else 'none'}")
        lines.append(f"- Lost: {', '.join(lost) if lost else 'none'}")
        lines.append(f"- Net: {'+' if net > 0 else ''}{net}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FINAL_REPORT.md
# ---------------------------------------------------------------------------

def write_final_report(
    records: list[dict[str, Any]],
    gate_report: Path | None,
    eval_files: dict[str, dict[str, Any] | None],
    config_diff_path: Path | None,
    out_dir: Path,
) -> Path:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    arms_seen = sorted({r.get("arm", "unknown") for r in records})
    task_ids = sorted({r.get("task_id", "") for r in records})
    n_tasks = len(task_ids)

    arm_desc = []
    for a in ARM_ORDER:
        if a in arms_seen:
            arm_desc.append(f"{a} ({ARM_LABELS.get(a, a)})")

    parts: list[str] = []
    parts.append("# OpenHands Evidence Delivery Smoke Gate Report\n")
    parts.append(f"**Date:** {now}")
    parts.append(f"**Tasks:** {n_tasks}")
    parts.append(f"**Arms:** {', '.join(arm_desc)}\n")

    # Gate report
    if gate_report and gate_report.exists():
        parts.append("## Delivery Gate Results\n")
        parts.append(gate_report.read_text(encoding="utf-8").strip())
        parts.append("")

    # Evidence summary
    parts.append("## Evidence Summary\n")
    parts.append(_evidence_summary_table(records))
    parts.append("")

    # Config parity
    if config_diff_path and config_diff_path.exists():
        parts.append("## Config Parity\n")
        parts.append(config_diff_path.read_text(encoding="utf-8").strip())
        parts.append("")

    # Evaluation
    eval_text = _eval_section(eval_files)
    if eval_text:
        parts.append(eval_text)
        parts.append("")

    # Verdict
    parts.append("## Verdict\n")
    # Determine gate pass/fail from gate report content
    gate_verdict = "UNKNOWN"
    if gate_report and gate_report.exists():
        content = gate_report.read_text(encoding="utf-8")
        if "FAIL" in content.upper().split("VERDICT")[-1] if "VERDICT" in content.upper() else "":
            gate_verdict = "FAIL"
        elif "PASS" in content.upper():
            # Check if there's an explicit FAIL too
            upper = content.upper()
            if "GATE: FAIL" in upper or "VERDICT: FAIL" in upper or "VERDICT — FAIL" in upper:
                gate_verdict = "FAIL"
            else:
                gate_verdict = "PASS"

    parts.append(f"DELIVERY GATE: {gate_verdict}")

    # Eval summary
    any_eval = any(v is not None for v in eval_files.values())
    if any_eval and gate_verdict == "PASS":
        eval_arms = []
        for arm in ARM_ORDER:
            data = eval_files.get(arm)
            if data:
                res = len(data.get("resolved_ids", []))
                total = data.get("total_instances", 0)
                eval_arms.append(f"{arm}: {res}/{total}")
        parts.append(f"EVALUATION: {'; '.join(eval_arms)}")
    elif any_eval and gate_verdict == "FAIL":
        parts.append("EVALUATION: Blocked — delivery gate failed")
    else:
        parts.append("EVALUATION: No eval data provided")

    path = out_dir / "FINAL_REPORT.md"
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpenHands Evidence Delivery Smoke Gate — Report Generator"
    )
    parser.add_argument("--telemetry", required=True, help="Path to delivery_telemetry.jsonl")
    parser.add_argument("--gate-report", default=None, help="Path to DELIVERY_GATE_REPORT.md")
    parser.add_argument("--baseline-eval", default=None, help="Path to baseline result.json")
    parser.add_argument("--gt-v11-eval", default=None, help="Path to gt_v11 result.json")
    parser.add_argument("--gt-v13-eval", default=None, help="Path to gt_v13 result.json")
    parser.add_argument("--baseline-meta", default=None, help="Path to baseline METADATA.json")
    parser.add_argument("--gt-v11-meta", default=None, help="Path to gt_v11 METADATA.json")
    parser.add_argument("--gt-v13-meta", default=None, help="Path to gt_v13 METADATA.json")
    parser.add_argument("--out-dir", required=True, help="Output directory for reports")
    args = parser.parse_args()

    telemetry_path = Path(args.telemetry)
    if not telemetry_path.exists():
        print(f"ERROR: telemetry file not found: {telemetry_path}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    records = _load_jsonl(telemetry_path)
    if not records:
        print("ERROR: telemetry file is empty", file=sys.stderr)
        sys.exit(1)

    gate_report = Path(args.gate_report) if args.gate_report else None

    meta_files: dict[str, Path | None] = {
        "baseline": Path(args.baseline_meta) if args.baseline_meta else None,
        "gt_v11": Path(args.gt_v11_meta) if args.gt_v11_meta else None,
        "gt_v13": Path(args.gt_v13_meta) if args.gt_v13_meta else None,
    }

    eval_files: dict[str, dict[str, Any] | None] = {
        "baseline": _load_json(Path(args.baseline_eval) if args.baseline_eval else None),
        "gt_v11": _load_json(Path(args.gt_v11_eval) if args.gt_v11_eval else None),
        "gt_v13": _load_json(Path(args.gt_v13_eval) if args.gt_v13_eval else None),
    }

    # 1. per_task_summary.csv
    csv_path = write_per_task_csv(records, out_dir)
    print(f"Written: {csv_path}")

    # 2. config_diff.md
    config_diff_path = write_config_diff(meta_files, out_dir)
    if config_diff_path:
        print(f"Written: {config_diff_path}")
    else:
        print("Skipped: config_diff.md (no metadata files provided)")

    # 3. FINAL_REPORT.md
    report_path = write_final_report(records, gate_report, eval_files, config_diff_path, out_dir)
    print(f"Written: {report_path}")


if __name__ == "__main__":
    main()
