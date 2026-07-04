#!/usr/bin/env python3
"""Deep paired evaluation for the in-flight smoke run.

Reads:
  - <run-dir>/per_task_summary.json (from aggregate_per_task_summary.py)
  - Per-task output.jsonl history -> tool call counts
  - Per-task infer_logs -> hook fires
Computes:
  - Paired patch flip table
  - Per-arm averages
  - Per-task table with actual MCP tool call counts (from history)
  - Cost projection
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path


GT_TOOL_RE = re.compile(r"^(groundtruth_|gt_)")


def find_oh_run_dirs(arm_dir: Path) -> list[Path]:
    out = []
    for v1r in arm_dir.glob("v1r_*"):
        for combo in v1r.glob("SWE-bench-Live/SWE-bench-Live/CodeActAgent/*"):
            if combo.is_dir():
                out.append(combo)
    return out


def parse_trajectory_tool_calls(history: list) -> tuple[int, Counter]:
    """Count GT tool invocations + return per-tool counter."""
    counter = Counter()
    n_total = 0
    for h in history or []:
        if not isinstance(h, dict):
            continue
        # Modern OH: action with name field
        a = h.get("action")
        if isinstance(a, dict):
            tname = a.get("tool_call_metadata") or a.get("name") or a.get("action")
            if isinstance(tname, dict):
                tname = tname.get("function_name") or tname.get("name")
            if isinstance(tname, str) and GT_TOOL_RE.match(tname):
                counter[tname] += 1
                n_total += 1
        # Legacy: extras
        extras = h.get("extras") or h.get("tool_call_metadata") or {}
        if isinstance(extras, dict):
            tname = extras.get("function_name") or extras.get("tool_name")
            if isinstance(tname, str) and GT_TOOL_RE.match(tname):
                counter[tname] += 1
                n_total += 1
    return n_total, counter


def per_task_trajectory(combo: Path, instance_id: str) -> dict:
    f = combo / "output.jsonl"
    if not f.exists():
        return {}
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("instance_id") != instance_id:
            continue
        history = r.get("history") or []
        n_tool, counter = parse_trajectory_tool_calls(history)
        edit_actions = sum(
            1 for h in history
            if isinstance(h, dict)
            and isinstance(h.get("action"), dict)
            and h["action"].get("action") in ("edit", "str_replace_editor", "file_editor")
        )
        return {
            "history_len": len(history),
            "gt_tool_calls": n_tool,
            "tool_distribution": dict(counter),
            "edit_actions": edit_actions,
        }
    return {}


def parse_infer_log_for_hooks(combo: Path, instance_id: str) -> dict:
    """Look for hook firing markers in OH per-task log."""
    log = combo / "infer_logs" / f"instance_{instance_id}.log"
    if not log.exists():
        return {"hook_marks": 0}
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"hook_marks": 0}
    marks = {
        "gt_hook_marker": text.count("<gt-hook>") + text.count("[GT_POST_EDIT]") + text.count("[GT_POST_VIEW]"),
        "evidence_marker": text.count("<gt-evidence>"),
        "post_edit": text.count("post_edit") + text.count("POST_EDIT"),
        "post_view": text.count("post_view") + text.count("POST_VIEW"),
        "brief_marker": text.count("<gt-task-brief>") + text.count("gt-task-brief"),
        "kernel_marker": text.count("<gt-kernel>"),
    }
    return marks


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True, type=Path)
    args = p.parse_args(argv)

    run_dir: Path = args.run_dir
    summary_p = run_dir / "per_task_summary.json"
    if not summary_p.exists():
        print(f"need {summary_p} (run aggregate_per_task_summary.py first)", file=sys.stderr)
        return 2
    base = json.loads(summary_p.read_text(encoding="utf-8"))
    paired = [r for r in base["paired"] if r.get("A") and r.get("B")]
    n = len(paired)

    # Locate combos per arm
    combos_by_arm: dict[str, list[Path]] = {"A": [], "B": []}
    for arm in ("A", "B"):
        arm_dir = run_dir / f"arm_{arm}"
        if arm_dir.is_dir():
            combos_by_arm[arm] = find_oh_run_dirs(arm_dir)

    print(f"=== PAIRED N = {n} ===\n")

    # Patch flip table
    both = sum(1 for r in paired if r["A"]["patch_present"] and r["B"]["patch_present"])
    a_only = sum(1 for r in paired if r["A"]["patch_present"] and not r["B"]["patch_present"])
    b_only = sum(1 for r in paired if not r["A"]["patch_present"] and r["B"]["patch_present"])
    neither = sum(1 for r in paired if not r["A"]["patch_present"] and not r["B"]["patch_present"])
    print("Patch flip table (paired):")
    print(f"  both produced patch: {both}")
    print(f"  A only:              {a_only}")
    print(f"  B only:              {b_only}")
    print(f"  neither:             {neither}\n")

    # Per-task deep dive
    print(f"{'instance':<40} | A: ch act $ tcalls hooks | B: ch act $ tcalls")
    print("-" * 110)

    arm_totals = {"A": {"gt_calls": 0, "hook_marks": 0, "edit_acts": 0, "tools_seen": Counter()},
                  "B": {"gt_calls": 0, "hook_marks": 0, "edit_acts": 0, "tools_seen": Counter()}}

    rows = []
    for r in paired:
        iid = r["instance_id"]
        row = {"instance": iid}
        for arm in ("A", "B"):
            for combo in combos_by_arm[arm]:
                t = per_task_trajectory(combo, iid)
                if t.get("history_len", 0) > 0:
                    row[arm] = t
                    h = parse_infer_log_for_hooks(combo, iid)
                    row[arm]["hooks"] = h
                    arm_totals[arm]["gt_calls"] += t["gt_tool_calls"]
                    arm_totals[arm]["hook_marks"] += sum(h.values())
                    arm_totals[arm]["edit_acts"] += t["edit_actions"]
                    arm_totals[arm]["tools_seen"].update(t["tool_distribution"])
                    break
            if arm not in row:
                row[arm] = {}
        rows.append(row)
        A = r["A"]
        B = r["B"]
        a_t = row["A"].get("gt_tool_calls", 0)
        a_h = sum((row["A"].get("hooks") or {}).values())
        b_t = row["B"].get("gt_tool_calls", 0)
        line = (
            f"{iid[:38]:<40} | "
            f"{A['patch_chars']:5}c {A['n_actions']:3}a ${A['cost_token_math_usd']:.3f} "
            f"{a_t:3}t {a_h:3}h | "
            f"{B['patch_chars']:5}c {B['n_actions']:3}a ${B['cost_token_math_usd']:.3f} "
            f"{b_t:3}t"
        )
        print(line)

    print()
    print("--- Arm totals (paired) ---")
    for arm in ("A", "B"):
        rows_arm = [r[arm] for r in paired]
        n_p = sum(1 for x in rows_arm if x["patch_present"])
        avg_act = statistics.mean(x["n_actions"] for x in rows_arm)
        avg_cost = statistics.mean(x["cost_token_math_usd"] for x in rows_arm)
        avg_in = statistics.mean(x["input_tokens"] for x in rows_arm)
        avg_out = statistics.mean(x["output_tokens"] for x in rows_arm)
        avg_cache = statistics.mean(x["cache_hit_ratio"] for x in rows_arm) * 100
        gt_calls = arm_totals[arm]["gt_calls"]
        hook_marks = arm_totals[arm]["hook_marks"]
        edit_acts = arm_totals[arm]["edit_acts"]
        print(
            f"  {arm}: patches={n_p}/{len(rows_arm)}  acts={avg_act:.1f}  cost=${avg_cost:.3f}  "
            f"in={avg_in/1000:.0f}k  out={avg_out/1000:.0f}k  cache={avg_cache:.0f}%  "
            f"gt_tool_calls(total)={gt_calls}  hook_marks(total)={hook_marks}  edits(total)={edit_acts}"
        )
        if arm == "A":
            print(f"    A's GT tool distribution: {dict(arm_totals[arm]['tools_seen'].most_common(15))}")

    print()
    print("--- Cost projection (Arm A 300-task GT-only) ---")
    sum_a = sum(r["A"]["cost_token_math_usd"] for r in paired)
    sum_b = sum(r["B"]["cost_token_math_usd"] for r in paired)
    print(f"  Paired sum: A=${sum_a:.2f} B=${sum_b:.2f} total=${sum_a + sum_b:.2f}")
    print(f"  Per-task avg A: ${sum_a/n:.4f}")
    print(f"  300-task projection: ${(sum_a/n)*300:.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
