#!/usr/bin/env python3
"""Aggregate GT obedience from per-task hook telemetry on the VM."""
from __future__ import annotations

import collections
import json
from pathlib import Path

ARMS = {
    "nolsp": Path("/tmp/smoke5_nolsp"),
    "lsp": Path("/tmp/smoke5_lsp"),
}


def read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def arm_of(ev: dict, fallback: str) -> str:
    a = (ev.get("arm") or "").lower()
    if "nolsp" in a:
        return "nolsp"
    if "hybrid" in a or "lsp" in a:
        return "lsp"
    rid = (ev.get("run_id") or "").lower()
    if "nolsp" in rid:
        return "nolsp"
    if "lsp" in rid:
        return "lsp"
    return fallback


def intervention_type(ev: dict) -> str:
    channel = ev.get("channel")
    if channel:
        return str(channel)
    event = ev.get("event", "")
    if event.startswith("submit_"):
        return "submit"
    return "unknown"


def observed_action(ev: dict) -> str:
    if ev.get("event") == "ack_followed":
        return ev.get("reason", "followed")
    if ev.get("event") == "ack_ignored":
        return ev.get("reason", "ignored")
    if ev.get("event") == "ack_not_observed":
        return "not_observed"
    if ev.get("event") == "submit_observed":
        return ev.get("status", "submit")
    return ev.get("event", "")


def main() -> None:
    per_arm = {
        "nolsp": collections.defaultdict(list),
        "lsp": collections.defaultdict(list),
    }
    per_task = {}
    for arm, root in ARMS.items():
        if not root.exists():
            continue
        for task_dir in sorted([p for p in root.iterdir() if p.is_dir()]):
            telem = task_dir / "gt_hook_telemetry.jsonl"
            evs = read_jsonl(telem)
            key = (arm, task_dir.name)
            per_task[key] = evs
            per_arm[arm][task_dir.name] = evs

    # Intervention map: id -> first seen intervention event + resolution.
    overall = collections.Counter()
    overall_by_type = collections.defaultdict(collections.Counter)
    overall_by_arm = collections.defaultdict(collections.Counter)
    per_arm_type = collections.defaultdict(lambda: collections.Counter())
    detail_rows = []

    for arm, tasks in per_arm.items():
        interventions = {}
        for task, evs in tasks.items():
            for ev in evs:
                iid = ev.get("intervention_id")
                if not iid:
                    continue
                rec = interventions.setdefault(iid, {
                    "arm": arm,
                    "task": task,
                    "type": intervention_type(ev),
                    "expected_next_action": ev.get("expected_next_action", ""),
                    "severity": ev.get("tier") or ev.get("confidence_tier") or "",
                    "reason": ev.get("reason", ""),
                    "events": [],
                    "ack": None,
                    "observed": None,
                })
                rec["events"].append(ev)
                if ev.get("event") in {"ack_followed", "ack_ignored", "ack_not_observed"}:
                    rec["ack"] = ev.get("event")
                    rec["observed"] = observed_action(ev)
                elif ev.get("event") == "submit_observed" and rec["observed"] is None:
                    rec["observed"] = observed_action(ev)
        for iid, rec in interventions.items():
            ack = rec["ack"] or "ack_not_observed"
            t = rec["type"]
            overall[ack] += 1
            overall_by_type[t][ack] += 1
            overall_by_arm[arm][ack] += 1
            per_arm_type[(arm, t)][ack] += 1
            detail_rows.append((arm, rec["task"], iid, t, rec["expected_next_action"], ack, rec["observed"], rec["reason"]))

    def fmt(counter: collections.Counter) -> str:
        return f"follow={counter.get('ack_followed',0)} ignore={counter.get('ack_ignored',0)} not_obs={counter.get('ack_not_observed',0)}"

    print("=== OVERALL ===")
    print(fmt(overall))
    denom = overall.get("ack_followed", 0) + overall.get("ack_ignored", 0) + overall.get("ack_not_observed", 0)
    resolved = overall.get("ack_followed", 0) + overall.get("ack_ignored", 0)
    if denom:
        print(f"follow_rate={overall.get('ack_followed',0)/max(1,resolved):.3f} ignore_rate={overall.get('ack_ignored',0)/max(1,resolved):.3f} unresolved_rate={overall.get('ack_not_observed',0)/denom:.3f}")
    print()
    print("=== BY ARM ===")
    for arm in ("nolsp", "lsp"):
        c = overall_by_arm.get(arm, collections.Counter())
        print(f"{arm}: {fmt(c)}")
    print()
    print("=== BY INTERVENTION TYPE ===")
    for t in sorted(overall_by_type):
        c = overall_by_type[t]
        total = sum(c.values())
        if total == 0:
            continue
        resolved = c.get("ack_followed", 0) + c.get("ack_ignored", 0)
        follow_rate = c.get("ack_followed", 0) / resolved if resolved else 0.0
        unresolved_rate = c.get("ack_not_observed", 0) / total if total else 0.0
        print(f"{t}: total={total} {fmt(c)} follow_rate={follow_rate:.3f} unresolved_rate={unresolved_rate:.3f}")
    print()
    print("=== BY ARM + TYPE ===")
    for (arm, t), c in sorted(per_arm_type.items()):
        total = sum(c.values())
        if total == 0:
            continue
        print(f"{arm}/{t}: total={total} {fmt(c)}")
    print()
    print("=== DETAIL (one row per intervention_id) ===")
    for row in sorted(detail_rows):
        arm, task, iid, t, exp, ack, obs, reason = row
        print(
            f"{arm:6} {task:32} {str(iid):20} "
            f"type={str(t):8} ack={str(ack):15} "
            f"observed={str(obs):22} expected={str(exp)}"
        )


if __name__ == "__main__":
    main()
