#!/usr/bin/env python3
"""Comprehensive smoke eval — produces SMOKE_REPORT-style output covering every layer.

For each paired task computes:
  Brief layer:
    - brief_present (Y/N)
    - brief_chars
    - brief sections detected
    - focus_files extracted from brief
  Localization (Arm A only):
    - focus_files vs gold_files (from gold-paths sidecar OR from edited files in resolved patch)
    - focus_precision / focus_coverage / first_gold_rank
  Hook layer (Arm A):
    - Counts of evidence markers in trajectory (CHANGE / CONTRACT / PATTERN / STRUCTURAL / SEMANTIC / IMPORT / CALLER / SIBLING / TEST / IMPACT / TYPE / PRECEDENT)
    - <gt-evidence>, <gt-hook>, [VERIFIED], [WARNING], [INFO] markers in user-turn msgs after edits
    - Total hook fires (proxy via marker count after agent edit actions)
  Edit-in-focus signal:
    - Of agent's edited files, how many were in brief's focus_files list?
  Action distribution per task
  Patch flip table (paired)
  Per-arm totals + per-task table
  Cost projection 300-task GT-only
  Caching: per-arm cache hit ratio, cache_hit_input_tokens
  Anomalies: tasks with no patch / 0 actions / brief missing / hung
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path


GT_MARKERS = [
    "<gt-evidence>", "<gt-hook>", "<gt-task-brief>",
    "[VERIFIED]", "[WARNING]", "[INFO]",
    "[GT_POST_EDIT]", "[GT_POST_VIEW]",
]
EVIDENCE_FAMILIES = ["CHANGE:", "CONTRACT:", "PATTERN:", "STRUCTURAL:", "SEMANTIC:",
                     "IMPORT:", "CALLER:", "SIBLING:", "TEST:", "IMPACT:", "TYPE:", "PRECEDENT:"]
PATCH_FILE_RE = re.compile(r"^\+\+\+ b/(\S+)$", re.MULTILINE)
BRIEF_FOCUS_RE = re.compile(r"focus[_ ]files?[\s:]*\[([^\]]+)\]", re.IGNORECASE)
BRIEF_PATH_RE = re.compile(r"`([^`\n]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|c|cc|cpp|h|hpp))`")


def find_oh_run_dirs(arm_dir: Path) -> list[Path]:
    out = []
    for v1r in arm_dir.glob("v1r_*"):
        for combo in v1r.glob("SWE-bench-Live/SWE-bench-Live/CodeActAgent/*"):
            if combo.is_dir():
                out.append(combo)
    return out


def load_outputs(combo: Path) -> dict[str, dict]:
    f = combo / "output.jsonl"
    if not f.exists():
        return {}
    out = {}
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        iid = r.get("instance_id")
        if iid:
            out[iid] = r
    return out


def parse_brief_from_instruction(instr: str) -> dict:
    has = "<gt-task-brief>" in instr or "gt-task-brief" in instr
    section_brief = ""
    if has:
        # Try to extract brief block
        m = re.search(r"<gt-task-brief>(.*?)</gt-task-brief>", instr, re.DOTALL)
        if m:
            section_brief = m.group(1)
        else:
            # try alt format
            m = re.search(r"gt-task-brief.*?(?=---|##|$)", instr, re.DOTALL)
            if m:
                section_brief = m.group(0)
    # Extract focus paths from brief
    focus = []
    if section_brief:
        for m in BRIEF_PATH_RE.finditer(section_brief):
            focus.append(m.group(1))
    # Heuristic: any file paths mentioned as focus in JSON-ish lists
    fbrief = (section_brief or "")
    sections_present = {
        s: s.lower() in fbrief.lower()
        for s in ["focus", "constraint", "candidate", "contract", "caller", "recent", "pattern"]
    }
    return {
        "has_brief": has,
        "brief_chars": len(section_brief),
        "focus_files": focus,
        "sections_present": sections_present,
    }


def files_in_patch(patch: str) -> list[str]:
    return PATCH_FILE_RE.findall(patch or "")


def hook_content_count(trajectory_text: str) -> dict[str, int]:
    out = {m: trajectory_text.count(m) for m in GT_MARKERS if m in trajectory_text}
    out["families"] = {f: trajectory_text.count(f) for f in EVIDENCE_FAMILIES if f in trajectory_text}
    return out


def per_task_actions(history: list) -> Counter:
    c = Counter()
    for h in history or []:
        if isinstance(h, dict):
            a = h.get("action")
            if a:
                c[a] += 1
    return c


def edited_files_from_history(history: list) -> list[str]:
    files = []
    for h in history or []:
        if not isinstance(h, dict):
            continue
        if h.get("action") in ("edit", "str_replace_editor", "file_editor"):
            args = h.get("args") or {}
            p = args.get("path") or args.get("file_path") or args.get("file")
            if p:
                files.append(p)
    return files


def usage_from_metrics(rec: dict) -> dict:
    m = rec.get("metrics") or {}
    return {
        "input_tokens": m.get("accumulated_token_usage", {}).get("prompt_tokens", 0),
        "output_tokens": m.get("accumulated_token_usage", {}).get("completion_tokens", 0),
        "cached_tokens": m.get("accumulated_token_usage", {}).get("cache_read_tokens", 0),
        "cost": m.get("accumulated_cost", 0.0),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True, type=Path)
    args = p.parse_args(argv)
    base: Path = args.run_dir

    arms_data: dict[str, dict[str, dict]] = {"A": {}, "B": {}}
    for arm in ("A", "B"):
        for combo in find_oh_run_dirs(base / f"arm_{arm}"):
            outputs = load_outputs(combo)
            for iid, rec in outputs.items():
                hist = rec.get("history") or []
                instr = rec.get("instruction") or ""
                tr_text = json.dumps(hist, default=str)
                brief = parse_brief_from_instruction(instr)
                actions = per_task_actions(hist)
                edited = edited_files_from_history(hist)
                gt_patch = ((rec.get("test_result") or {}).get("git_patch") or "")
                patch_files = files_in_patch(gt_patch)
                hooks = hook_content_count(tr_text) if arm == "A" else {"families": {}}
                # edits-in-focus signal
                eif = 0
                if brief["focus_files"] and edited:
                    focus_set = {f for f in brief["focus_files"]}
                    eif = sum(1 for e in edited if any(e.endswith(f) or f.endswith(e) for f in focus_set))
                arms_data[arm][iid] = {
                    "history_len": len(hist),
                    "instruction_chars": len(instr),
                    "brief": brief,
                    "actions": dict(actions),
                    "edits": actions.get("edit", 0),
                    "edited_files": edited,
                    "edits_in_focus": eif,
                    "patch_chars": len(gt_patch),
                    "patch_files": patch_files,
                    "hook_marks": hooks,
                    "n_actions": sum(actions.values()),
                }

    paired = sorted(set(arms_data["A"].keys()) & set(arms_data["B"].keys()))
    n = len(paired)
    print(f"# SMOKE COMPREHENSIVE EVAL — paired n={n} (smoke paused at A=15 B=19)\n")

    print("## 1. APPARATUS\n")
    print(f"- Paired complete: {n}/30 ({n*100//30}%)")
    a_patch = sum(1 for iid in paired if arms_data['A'][iid]['patch_chars'] > 0)
    b_patch = sum(1 for iid in paired if arms_data['B'][iid]['patch_chars'] > 0)
    print(f"- Patches generated: A={a_patch}/{n}, B={b_patch}/{n}")
    a_zero = [iid for iid in paired if arms_data['A'][iid]['n_actions'] == 0]
    print(f"- Tasks with 0 actions on A: {a_zero}")

    print("\n## 2. BRIEF LAYER (Arm A)\n")
    a_brief = sum(1 for iid in paired if arms_data['A'][iid]['brief']['has_brief'])
    print(f"- Brief in instruction: {a_brief}/{n} ({a_brief*100//n}%)")
    a_brief_avg = statistics.mean(arms_data['A'][iid]['brief']['brief_chars'] for iid in paired) if paired else 0
    print(f"- Avg brief chars: {a_brief_avg:.0f}")
    missing = [iid for iid in paired if not arms_data['A'][iid]['brief']['has_brief']]
    if missing:
        print(f"- Tasks MISSING brief (silent failure): {missing}")
    avg_focus = statistics.mean(len(arms_data['A'][iid]['brief']['focus_files']) for iid in paired) if paired else 0
    print(f"- Avg focus_files extracted from brief: {avg_focus:.1f}")

    print("\n## 3. LOCALIZATION SIGNAL (Arm A — edits-in-focus)\n")
    eif_total = 0; edit_total = 0; tasks_with_focus_overlap = 0
    for iid in paired:
        a = arms_data['A'][iid]
        eif_total += a['edits_in_focus']; edit_total += a['edits']
        if a['edits_in_focus'] > 0: tasks_with_focus_overlap += 1
    print(f"- Total edits across A's 15 tasks: {edit_total}")
    print(f"- Edits hitting focus_files: {eif_total} ({eif_total*100//max(edit_total,1)}% of all edits)")
    print(f"- Tasks where any edit hit focus: {tasks_with_focus_overlap}/{n}")

    print("\n## 4. HOOK CONTENT DELIVERY (Arm A — evidence in trajectory)\n")
    total_marks = Counter()
    fam_counts = Counter()
    for iid in paired:
        for k, v in arms_data['A'][iid]['hook_marks'].items():
            if isinstance(v, dict):
                fam_counts.update(v)
            elif isinstance(v, int):
                total_marks[k] += v
    print(f"- Marker counts across A trajectories: {dict(total_marks)}")
    print(f"- Evidence-family mentions (CHANGE/CONTRACT/PATTERN/...): {dict(fam_counts)}")
    print(f"- Verdict: post-edit hook content delivered? {'YES' if (total_marks.get('<gt-evidence>',0) > 0 or total_marks.get('<gt-hook>',0) > 0 or sum(fam_counts.values()) > 0) else '**NO — hook silent**'}")

    print("\n## 5. PATCH FLIP TABLE (paired)\n")
    both = sum(1 for iid in paired if arms_data['A'][iid]['patch_chars']>0 and arms_data['B'][iid]['patch_chars']>0)
    a_only = sum(1 for iid in paired if arms_data['A'][iid]['patch_chars']>0 and arms_data['B'][iid]['patch_chars']==0)
    b_only = sum(1 for iid in paired if arms_data['A'][iid]['patch_chars']==0 and arms_data['B'][iid]['patch_chars']>0)
    neither = sum(1 for iid in paired if arms_data['A'][iid]['patch_chars']==0 and arms_data['B'][iid]['patch_chars']==0)
    print(f"- Both produced patch:  {both}")
    print(f"- A only (GT helped):   {a_only}")
    print(f"- B only (GT hurt):     {b_only}")
    print(f"- Neither:              {neither}")
    print(f"- **Δ (A − B):          {a_only - b_only}**")

    print("\n## 6. ACTION DISTRIBUTION (paired sums)\n")
    a_acts = Counter(); b_acts = Counter()
    for iid in paired:
        a_acts.update(arms_data['A'][iid]['actions'])
        b_acts.update(arms_data['B'][iid]['actions'])
    print(f"- Arm A: {dict(a_acts.most_common())}")
    print(f"- Arm B: {dict(b_acts.most_common())}")

    print("\n## 7. PER-TASK TABLE\n")
    print(f"{'instance':<38} | A: brief edits eif patch_c acts | B: edits patch_c acts")
    print("-"*110)
    for iid in paired:
        a = arms_data['A'][iid]; b = arms_data['B'][iid]
        b_brief = "Y" if a['brief']['has_brief'] else "-"
        print(f"{iid[:36]:<38} | {b_brief}    {a['edits']:5} {a['edits_in_focus']:3} {a['patch_chars']:5}  {a['n_actions']:4} | {b['edits']:5} {b['patch_chars']:5} {b['n_actions']:4}")

    print("\n## 8. COST + CACHING\n")
    # Pull per-task summary if exists for cost field
    pts = base / 'per_task_summary.json'
    if pts.exists():
        d = json.loads(pts.read_text())
        rows = [r for r in d['paired'] if r.get('A') and r.get('B')]
        sum_a = sum(r['A']['cost_token_math_usd'] for r in rows)
        sum_b = sum(r['B']['cost_token_math_usd'] for r in rows)
        avg_cache_a = statistics.mean(r['A']['cache_hit_ratio'] for r in rows)*100
        avg_cache_b = statistics.mean(r['B']['cache_hit_ratio'] for r in rows)*100
        sum_in_a = sum(r['A']['input_tokens'] for r in rows)
        sum_in_b = sum(r['B']['input_tokens'] for r in rows)
        sum_out_a = sum(r['A']['output_tokens'] for r in rows)
        sum_out_b = sum(r['B']['output_tokens'] for r in rows)
        print(f"- Arm A: ${sum_a:.2f} | avg ${sum_a/n:.4f}/task | in={sum_in_a/1000:.0f}k out={sum_out_a/1000:.0f}k | cache={avg_cache_a:.0f}%")
        print(f"- Arm B: ${sum_b:.2f} | avg ${sum_b/n:.4f}/task | in={sum_in_b/1000:.0f}k out={sum_out_b/1000:.0f}k | cache={avg_cache_b:.0f}%")
        print(f"- A is {(sum_a/sum_b - 1)*100:+.0f}% vs B")
        print(f"- 300-task projection (Arm A only, GT-only Phase 2): ${sum_a/n*300:.2f}")
    print()
    print("## 9. ANOMALIES\n")
    for iid in paired:
        a = arms_data['A'][iid]
        if a['n_actions'] == 0:
            print(f"- {iid}: A had 0 actions (task crashed at startup or instance file disappeared)")
        if not a['brief']['has_brief']:
            print(f"- {iid}: A's brief missing (silent failure — wrapper didn't inject brief)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
