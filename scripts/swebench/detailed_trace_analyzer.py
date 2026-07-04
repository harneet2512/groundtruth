"""Detailed per-task cycle-by-cycle trace analyzer for GT SWE-bench runs.

Output: Markdown report with one section per task-run showing
  - What GT sent to the agent (pre_edit_briefing, micro-steer text, gt_check responses)
  - What the agent did back (tool calls, shell commands, thoughts)
  - Running utilization (orient/lookup/impact/check counters, event totals)
  - Whether each GT steer was followed behaviorally
  - Final outcome (resolved, unresolved, empty_patch, killed)

Input: tracked run archives under benchmarks/swebench/deepseek_rerun_{nolsp,lsp}_r1/repeat_1/.
Usage:  python detailed_trace_analyzer.py <output.md>
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

RERUN_ROOT = Path(r"D:\Groundtruth\benchmarks\swebench")
ARMS = [
    ("gt-nolsp", RERUN_ROOT / "deepseek_rerun_nolsp_r1" / "repeat_1", "eval_report" ),
    ("gt-lsp-hybrid", RERUN_ROOT / "deepseek_rerun_lsp_r1" / "repeat_1", "eval_report"),
]
TASKS = [
    "astropy__astropy-12907", "astropy__astropy-13033", "astropy__astropy-13236",
    "astropy__astropy-13398", "astropy__astropy-13453", "astropy__astropy-13579",
    "astropy__astropy-13977", "astropy__astropy-14096", "astropy__astropy-14182",
    "astropy__astropy-14309",
]


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def extract_last_user_msg_tail(query: list[dict]) -> str:
    """Find the last user-role message containing a <gt-evidence> block and return that block."""
    if not isinstance(query, list):
        return ""
    for m in reversed(query):
        role = m.get("role")
        if role != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        if not isinstance(c, str):
            continue
        if "<gt-evidence>" in c:
            start = c.find("<gt-evidence>")
            end = c.find("</gt-evidence>")
            if start != -1 and end != -1:
                return c[start:end + len("</gt-evidence>")]
    return ""


def clean_action(a: str) -> str:
    return (a or "").strip().split("\n")[0][:120]


def clean_obs(o: str) -> str:
    o = (o or "").strip().replace("\n", " ")
    return o[:180] + ("..." if len(o) > 180 else "")


def analyze_task(arm: str, run_dir: Path, task_id: str, eval_report: dict) -> str:
    task_dir = run_dir / task_id
    tel = load_jsonl(task_dir / "gt_hook_telemetry.jsonl")
    traj_path = task_dir / task_id / f"{task_id}.traj"
    traj = {}
    if traj_path.exists():
        try:
            traj = json.loads(traj_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            traj = {}
    steps = traj.get("trajectory", [])

    # Resolution outcome
    submitted = set(eval_report.get("submitted_ids", []))
    resolved = set(eval_report.get("resolved_ids", []))
    empty = set(eval_report.get("empty_patch_ids", []))
    errored = set(eval_report.get("error_ids", []))
    if task_id in resolved:
        outcome = "RESOLVED"
    elif task_id in submitted:
        outcome = "UNRESOLVED (patch submitted, tests failed)"
    elif task_id in empty:
        outcome = "EMPTY PATCH (no diff)"
    elif task_id in errored:
        outcome = "ERROR"
    else:
        outcome = "NO PATCH / KILLED"

    # Killed tasks for this arm
    killed_path = run_dir / "killed_tasks.jsonl"
    kills = load_jsonl(killed_path)
    killed = any(k.get("instance_id") == task_id for k in kills)
    if killed:
        outcome = f"KILLED ({[k.get('reason') for k in kills if k.get('instance_id') == task_id][0]})"

    # Build per-cycle map of GT telemetry events
    cycle_events: dict[int, list[dict]] = {}
    for e in tel:
        cy = e.get("cycle")
        if cy is None:
            continue
        cycle_events.setdefault(int(cy), []).append(e)

    # Build per-step info from traj; each step corresponds to one "cycle" from GT's perspective
    # state.gt_last_event tells us which cycle GT was at when step began
    step_info: list[dict] = []
    for i, s in enumerate(steps):
        state = s.get("state", {})
        gt_last = state.get("gt_last_event", "{}")
        try:
            last = json.loads(gt_last) if isinstance(gt_last, str) else gt_last
        except Exception:
            last = {}
        cy = last.get("cycle")
        gt_counters_s = state.get("gt_counters", "{}")
        try:
            ctrs = json.loads(gt_counters_s) if isinstance(gt_counters_s, str) else gt_counters_s
        except Exception:
            ctrs = {}
        gt_events_s = state.get("gt_events", "{}")
        try:
            evts = json.loads(gt_events_s) if isinstance(gt_events_s, str) else gt_events_s
        except Exception:
            evts = {}
        gt_ev_tail = extract_last_user_msg_tail(s.get("query", []))
        step_info.append({
            "step": i,
            "cycle": cy,
            "action": clean_action(s.get("action", "")),
            "observation": clean_obs(s.get("observation", "")),
            "thought": clean_obs(s.get("thought", "")),
            "counters": ctrs,
            "events": evts,
            "gt_inject": gt_ev_tail,
            "exec_s": s.get("execution_time"),
        })

    # Assemble timeline rows (combine telemetry events per cycle with step info if available)
    # Use cycle from traj step (== state.gt_last_event.cycle when step began)
    md: list[str] = []
    md.append(f"### `{task_id}` — arm `{arm}` — **{outcome}**\n")

    # Header stats
    total_cycles = max(cycle_events.keys()) if cycle_events else 0
    total_steps = len(steps)
    ev_counter = Counter(e.get("event", "?") for e in tel)
    final_ctrs = step_info[-1]["counters"] if step_info else {}
    final_evts = step_info[-1]["events"] if step_info else {}
    md.append(
        f"- Total trajectory steps: **{total_steps}** | GT cycles observed: **{total_cycles}** | "
        f"Task dir: `{task_dir.name}`\n"
    )
    md.append(
        f"- Final tool-call counters: orient=**{final_ctrs.get('orient',0)}**, "
        f"lookup=**{final_ctrs.get('lookup',0)}**, impact=**{final_ctrs.get('impact',0)}**, "
        f"check=**{final_ctrs.get('check',0)}**\n"
    )
    md.append(
        f"- Final event totals: material_edit=**{final_evts.get('material_edit',0)}**, "
        f"ack_armed=**{final_evts.get('ack_armed',0)}**, steer_delivered=**{final_evts.get('steer_delivered',0)}**, "
        f"ack_engagement=**{final_evts.get('ack_engagement',0)}**, "
        f"ack_followed=**{final_evts.get('ack_followed',0)}**, "
        f"ack_not_observed=**{final_evts.get('ack_not_observed',0)}**, "
        f"lsp_promotion=**{final_evts.get('lsp_promotion',0)}**, "
        f"submit_observed=**{final_evts.get('submit_observed',0)}**\n"
    )
    md.append(
        f"- Telemetry event distribution: "
        + ", ".join(f"{k}={v}" for k, v in ev_counter.most_common()) + "\n"
    )

    # Key moments narrative — pre_edit_briefing, first material_edit, steer_delivered, agent follow-up, ack outcome
    md.append("\n#### Key moments\n")

    brief = next((e for e in tel if e.get("event") == "pre_edit_briefing"), None)
    if brief:
        md.append(
            f"- **Pre-edit briefing (cycle {brief.get('cycle')}, {brief.get('ts')}):** "
            f"status = `{brief.get('status')}` "
            + (
                f"— **no candidates found, silent briefing** (GT sent nothing to the agent)."
                if brief.get("status") == "no_candidates_silent"
                else f"— briefing content emitted."
            ) + "\n"
        )

    first_me = next((e for e in tel if e.get("event") == "material_edit"), None)
    if first_me:
        md.append(
            f"- **First material_edit (cycle {first_me.get('cycle')}, {first_me.get('ts')}):** "
            f"files = `{first_me.get('files')}`, edit_count = {first_me.get('edit_count')}.\n"
        )

    steers = [e for e in tel if e.get("event") == "steer_delivered"]
    for i, s in enumerate(steers, 1):
        expected = s.get("expected_next_action", {})
        md.append(
            f"- **Steer #{i} delivered (cycle {s.get('cycle')}, {s.get('ts')}):** "
            f"file=`{s.get('file')}`, expected next action = `{expected.get('text')}`, "
            f"payload_len={s.get('payload_len')}, tier=`{s.get('tier')}`, ack_id=`{s.get('ack_id')}`.\n"
        )

    lsp_events = [e for e in tel if e.get("event") == "lsp_promotion"]
    if lsp_events:
        md.append(
            f"- **LSP promotions:** {len(lsp_events)} "
            f"(cycles: {[e.get('cycle') for e in lsp_events]})\n"
        )

    # Did the agent follow the steers? Match agent's next action after a steer_delivered against expected_next_action_text
    md.append("\n#### Steer follow-through analysis\n")
    for i, s in enumerate(steers, 1):
        expected_text = (s.get("expected_next_action") or {}).get("text", "")
        steer_cycle = s.get("cycle")
        # Find trajectory step(s) where cycle > steer_cycle
        following_actions = [
            si for si in step_info
            if isinstance(si["cycle"], int) and si["cycle"] >= (steer_cycle or 0)
        ][:3]
        followed = any(expected_text and expected_text.split()[0] in (si["action"] or "")
                       for si in following_actions)
        md.append(
            f"- **Steer #{i}** (cycle {steer_cycle}) expected `{expected_text}` — "
            f"{'AGENT FOLLOWED' if followed else 'agent did not follow'}. "
            f"Next 3 agent actions: "
            + "; ".join(f"c{si['cycle']}={si['action']!r}" for si in following_actions)
            + "\n"
        )

    # Extract GT injection text if present
    injects = [si for si in step_info if si["gt_inject"]]
    if injects:
        md.append("\n#### Actual GT evidence/steer text seen by agent (first injection)\n")
        md.append("```\n" + injects[0]["gt_inject"][:1200] + "\n```\n")

    # Per-cycle timeline table
    md.append("\n#### Per-cycle timeline\n")
    md.append("| cycle | ts | GT event(s) | agent action | obs preview | orient | lookup | impact | check |\n")
    md.append("|:-:|:-:|---|---|---|:-:|:-:|:-:|:-:|\n")
    # Build per-cycle rows: merge telemetry events and step info
    all_cycles = sorted(set(list(cycle_events.keys()) + [si["cycle"] for si in step_info if isinstance(si["cycle"], int)]))
    step_by_cycle: dict[int, list[dict]] = {}
    for si in step_info:
        cy = si["cycle"]
        if isinstance(cy, int):
            step_by_cycle.setdefault(cy, []).append(si)
    for cy in all_cycles:
        events_this = cycle_events.get(cy, [])
        noteworthy = [e for e in events_this if e.get("event") not in ("cycle", "cycle_end", "git_diff_found")]
        gt_descr = ", ".join(
            f"{e.get('event')}"
            + (f"({e.get('status')})" if e.get('status') else "")
            + (f"→{e.get('expected_next_action', {}).get('text','')[:40]}" if e.get("event") == "steer_delivered" else "")
            for e in noteworthy
        ) or "—"
        sis = step_by_cycle.get(cy, [])
        ts = (events_this[0].get("ts") if events_this else (sis[0].get("exec_s") if sis else "")) or ""
        if sis:
            si = sis[0]
            action = si["action"].replace("|", "\\|")[:80]
            obs = si["observation"].replace("|", "\\|")[:100]
            ctr = si["counters"]
        else:
            action = "—"
            obs = "—"
            ctr = {}
        md.append(
            f"| {cy} | {ts} | {gt_descr} | `{action}` | {obs} | "
            f"{ctr.get('orient','-')} | {ctr.get('lookup','-')} | "
            f"{ctr.get('impact','-')} | {ctr.get('check','-')} |\n"
        )

    md.append("\n---\n")
    return "".join(md)


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\Users\Lenovo\Downloads\GT_DeepSeek_DetailedTrace_20260423.md")
    out: list[str] = []
    out.append("# Detailed Cycle-by-Cycle Trace — DeepSeek-v3.2-maas Controlled Rerun\n\n")
    out.append("**Date:** 2026-04-23 00:33→01:32 UTC. "
               "**Suite:** `frozen_gt_astropy10` (10 tasks × 2 arms = 20 task-runs). "
               "**Model:** `openai/deepseek-v3.2-maas`.\n\n")
    out.append("Per-task: what GT sent, what the agent responded, running utilization at each cycle, "
               "whether steers were followed, and final outcome.\n\n")

    for arm, run_dir, _ in ARMS:
        out.append(f"\n## Arm: `{arm}`\n\n")
        out.append(f"Archive: `{run_dir}`.\n")

        eval_report_path = run_dir.parent / "eval_report.json"
        eval_report = {}
        if eval_report_path.exists():
            eval_report = json.loads(eval_report_path.read_text(encoding="utf-8"))

        # Per-arm summary
        resolved = [x for x in eval_report.get("resolved_ids", []) if x.startswith("astropy")]
        submitted = [x for x in eval_report.get("submitted_ids", []) if x.startswith("astropy")]
        empty = [x for x in eval_report.get("empty_patch_ids", []) if x.startswith("astropy")]
        out.append(
            f"- Eval outcome: resolved=**{len(resolved)}/10**, submitted={len(submitted)}, "
            f"empty_patch={len(empty)} → resolved: {resolved}\n\n"
        )

        for task_id in TASKS:
            out.append(analyze_task(arm, run_dir, task_id, eval_report))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(out), encoding="utf-8")
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
