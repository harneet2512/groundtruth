#!/usr/bin/env python3
"""Task-wise + per-arm Gate 1 smoke analysis.

Reads <nolsp-dir>/<task>/ and <lsp-dir>/<task>/ (preds.json + run.log +
harvested gt_hook_telemetry.jsonl). Emits a per-task breakdown and the
CANARY_VERIFY.md Recommended Metrics Table + verdict.

Defaults target the VM paths (/tmp/smoke5_{nolsp,lsp}) but accept local
override for post-fetch analysis from ab_report/smoke_{nolsp,lsp}.

Ack source-of-truth:
  ack_followed + ack_ignored + ack_not_observed come from harvested
  gt_hook_telemetry.jsonl (OpenAI trace-grading pattern — event-based,
  not keyword overlap). If per-task telemetry is missing, ack is marked
  'ack_not_observed' and the gate fails open with `unmeasured`, not silent
  PASS.
GT utilization source-of-truth:
  gt_orient / gt_lookup / gt_impact / gt_check counts come from harvested
  runtime budget state when present; trajectory `action` fields are only a
  fallback proxy for attempt counts when the harvested state is missing.
  run.log text matches are labelled `(log mentions)` and never used when a
  budget state or trajectory is present.
"""
from __future__ import annotations
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

TASKS = [
    "astropy__astropy-12907",
    "astropy__astropy-13033",
    "astropy__astropy-13236",
    "astropy__astropy-13398",
    "astropy__astropy-13453",
]
ARMS: dict[str, str] = {
    "nolsp": "/tmp/smoke5_nolsp",
    "lsp":   "/tmp/smoke5_lsp",
}
TELEMETRY: Path = Path("/tmp/smoke5_telemetry")


def read_preds(arm_dir: Path, task: str) -> tuple[int, str]:
    p = arm_dir / task / "preds.json"
    if not p.exists():
        return 0, "missing"
    try:
        data = json.loads(p.read_text(errors="ignore"))
    except Exception:
        return 0, "unparseable"
    if isinstance(data, dict):
        rec = next(iter(data.values())) if data else {}
    elif isinstance(data, list) and data:
        rec = data[0]
    else:
        rec = {}
    patch = rec.get("model_patch") or rec.get("patch") or ""
    status = rec.get("exit_status") or rec.get("status") or "unknown"
    return len(patch or ""), status


def scan_runlog(arm_dir: Path, task: str) -> dict:
    p = arm_dir / task / "run.log"
    if not p.exists():
        return {}
    text = p.read_text(errors="ignore")
    return {
        "gt_orient":  len(re.findall(r"\bgt_orient\b", text)),
        "gt_lookup":  len(re.findall(r"\bgt_lookup\b", text)),
        "gt_impact":  len(re.findall(r"\bgt_impact\b", text)),
        "gt_check":   len(re.findall(r"\bgt_check\b", text)),
        "str_replace_editor": len(re.findall(r"str_replace_editor", text)),
        "FormatError": len(re.findall(r"FunctionCallingFormatError|FormatError", text)),
        "ACCESS_TOKEN_EXPIRED": len(re.findall(r"ACCESS_TOKEN_EXPIRED", text)),
        "cost_error": len(re.findall(r"Error calculating cost", text)),
        "steps":      len(re.findall(r"=== Step", text)),
        "bytes":      len(text),
    }


def scan_traj_actions(arm_dir: Path, task: str) -> dict:
    """Count actual gt_* actions from a trajectory artifact if present.

    The run.log text is only a mention/prose proxy. Use trajectory action
    fields for invocation counts when available.
    """
    task_dir = arm_dir / task
    if not task_dir.exists():
        return {}
    candidates = sorted(
        list(task_dir.rglob("*.traj")) + list(task_dir.rglob("*.traj.json"))
    )
    if not candidates:
        return {}
    path = candidates[0]
    try:
        data = json.loads(path.read_text(errors="ignore"))
    except Exception:
        return {}
    hist = data.get("history") or data.get("trajectory") or []
    out = Counter()
    for entry in hist:
        action = (entry.get("action") or "").strip()
        if not action:
            continue
        head = action.split()[0].split("/")[-1]
        if head in ("gt_orient", "gt_lookup", "gt_impact", "gt_check"):
            out[head] += 1
    return dict(out)


def scan_budget_state(arm_dir: Path, task: str) -> dict:
    """Read harvested runtime budget state for allowed gt_* counts."""
    p = arm_dir / task / "gt_budget.state.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(errors="ignore"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for key, bucket_name in (
        ("gt_orient", "orient"),
        ("gt_lookup", "lookup"),
        ("gt_impact", "impact"),
        ("gt_check", "check"),
    ):
        bucket = data.get(bucket_name, {})
        if isinstance(bucket, dict) and isinstance(bucket.get("count"), int):
            out[key] = bucket["count"]
    return out


def load_telemetry() -> list[dict]:
    """Telemetry source order (first hit wins per task):

    1. <arm_dir>/<task>/gt_hook_telemetry.jsonl  — harvested per-task via
       docker cp by gt_telemetry_scraper.sh. Preferred.
    2. TELEMETRY/*.jsonl                          — legacy merged dump.
    """
    events: list[dict] = []
    seen_tasks: set[tuple[str, str]] = set()
    for arm_name, base in ARMS.items():
        base_p = Path(base)
        if not base_p.exists():
            continue
        for task_dir in base_p.iterdir():
            if not task_dir.is_dir():
                continue
            jsonl = task_dir / "gt_hook_telemetry.jsonl"
            if not jsonl.exists():
                continue
            for line in jsonl.read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                ev.setdefault("arm", f"gt-{arm_name}")
                ev.setdefault("instance_id", task_dir.name)
                events.append(ev)
                seen_tasks.add((arm_name, task_dir.name))
    if TELEMETRY.exists():
        for jf in TELEMETRY.glob("*.jsonl"):
            for line in jf.read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                # Skip duplicates already captured from per-task jsonl.
                a = (ev.get("arm") or "").lower()
                iid = ev.get("instance_id") or ""
                arm_key = "nolsp" if "nolsp" in a else ("lsp" if ("lsp" in a or "hybrid" in a) else None)
                if arm_key and (arm_key, iid) in seen_tasks:
                    continue
                events.append(ev)
    return events


def arm_of(ev: dict) -> str | None:
    a = (ev.get("arm") or "").lower()
    if "nolsp" in a:
        return "nolsp"
    if "hybrid" in a or a == "gt-lsp" or "lsp" in a:
        return "lsp"
    rid = (ev.get("run_id") or "").lower()
    if "nolsp" in rid:
        return "nolsp"
    if "lsp" in rid:
        return "lsp"
    return None


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nolsp-dir", default=ARMS["nolsp"],
                    help="Arm dir for gt-nolsp (default /tmp/smoke5_nolsp)")
    ap.add_argument("--lsp-dir", default=ARMS["lsp"],
                    help="Arm dir for gt-hybrid (default /tmp/smoke5_lsp)")
    ap.add_argument("--telemetry-dir", default=str(TELEMETRY),
                    help="Legacy merged telemetry dir (default /tmp/smoke5_telemetry)")
    return ap.parse_args()


def main() -> None:
    args = _parse_args()
    global ARMS, TELEMETRY
    ARMS = {"nolsp": args.nolsp_dir, "lsp": args.lsp_dir}
    TELEMETRY = Path(args.telemetry_dir)
    events = load_telemetry()
    per_arm = defaultdict(list)
    per_task = defaultdict(list)
    for ev in events:
        a = arm_of(ev)
        if a:
            per_arm[a].append(ev)
        iid = ev.get("instance_id")
        if iid and a:
            per_task[(a, iid)].append(ev)

    # ---------- per-task table ----------
    print("=" * 80)
    print("PER-TASK BREAKDOWN")
    print("=" * 80)
    header = f"{'ARM':5} {'TASK':32} {'PATCH':>6} {'EXIT':22} {'STEPS':>5} {'ORIENT':>6} {'LOOK':>5} {'IMP':>4} {'CHK':>4}"
    print(header)
    print("-" * len(header))
    per_arm_counts = defaultdict(lambda: {"nonempty": 0, "total": 0})
    for arm_name, base in ARMS.items():
        base_p = Path(base)
        for t in TASKS:
            sz, st = read_preds(base_p, t)
            rl = scan_runlog(base_p, t)
            per_arm_counts[arm_name]["total"] += 1
            if sz > 0:
                per_arm_counts[arm_name]["nonempty"] += 1
            budget_counts = scan_budget_state(base_p, t)
            traj_counts = scan_traj_actions(base_p, t)
            counts = budget_counts or traj_counts or rl
            if budget_counts:
                note = " (budget state)"
            elif traj_counts:
                note = " (attempts)"
            else:
                note = " (log mentions)"
            print(f"{arm_name:5} {t:32} {sz:>6} {st:22} "
                  f"{rl.get('steps',0):>5} {counts.get('gt_orient',0):>6} "
                  f"{counts.get('gt_lookup',0):>5} {counts.get('gt_impact',0):>4} "
                  f"{counts.get('gt_check',0):>4}{note}")

    # ---------- per-task telemetry histogram ----------
    print()
    print("=" * 80)
    print("PER-TASK HOOK EVENT HISTOGRAM")
    print("=" * 80)
    for arm_name in ARMS:
        print(f"\n[{arm_name}]")
        for t in TASKS:
            evs = per_task.get((arm_name, t), [])
            if not evs:
                print(f"  {t}: <no telemetry>")
                continue
            hist = Counter(e.get("event", "?") for e in evs)
            acks = Counter(e.get("reason", "?") for e in evs
                           if e.get("event", "").startswith("ack_"))
            denied = sum(1 for e in evs if e.get("event") == "budget_denied")
            micro = sum(1 for e in evs if e.get("event") == "micro_emitted")
            verify = sum(1 for e in evs if e.get("event") == "verify_emitted")
            lsp_p = sum(1 for e in evs if e.get("event") == "lsp_promotion")
            mat = sum(1 for e in evs if e.get("event") == "material_edit")
            wr = sum(1 for e in evs if e.get("event") == "wrapper_invoked")
            pre = sum(1 for e in evs if e.get("event") == "pre_edit_briefing")
            print(f"  {t}: events={len(evs)} pre_edit={pre} material={mat} "
                  f"wrapper={wr} micro={micro} verify={verify} lsp_prom={lsp_p} budget_denied={denied}")
            if acks:
                print(f"     ack reasons: {dict(acks)}")

    # ---------- per-arm rollups ----------
    print()
    print("=" * 80)
    print("PER-ARM ROLLUP (Recommended Metrics Table per CANARY_VERIFY.md)")
    print("=" * 80)
    print(f"{'METRIC':32} {'nolsp':>12} {'lsp':>12}")
    print("-" * 58)

    def rate(num: int, den: int) -> str:
        if den == 0:
            return "-"
        return f"{num}/{den} ({100*num/den:.0f}%)"

    metrics = {}
    for arm_name, base in ARMS.items():
        evs = per_arm.get(arm_name, [])
        by_ev = Counter(e.get("event", "?") for e in evs)
        ack_reason = Counter(e.get("reason", "?") for e in evs
                             if e.get("event", "").startswith("ack_"))
        instances_with_edit = {e.get("instance_id") for e in evs
                               if e.get("event") == "material_edit"}
        auth_fail = sum(1 for e in evs if e.get("event") == "auth_fail")
        one_step = 0
        for t in TASKS:
            rl = scan_runlog(Path(base), t)
            if rl.get("steps", 99) <= 1:
                one_step += 1
        metrics[arm_name] = {
            "tasks_total": 5,
            "nonempty": per_arm_counts[arm_name]["nonempty"],
            "tasks_with_material_edit": len(instances_with_edit),
            "gt_orient": by_ev.get("gt_orient", 0),
            "gt_check":  by_ev.get("gt_check", 0),
            "micro":     by_ev.get("micro_emitted", 0),
            "verify":    by_ev.get("verify_emitted", 0),
            "ack_followed": by_ev.get("ack_followed", 0),
            "ack_ignored": by_ev.get("ack_ignored", 0),
            "ack_not_observed": by_ev.get("ack_not_observed", 0),
            "budget_denied": by_ev.get("budget_denied", 0),
            "submit_observed": by_ev.get("submit_observed", 0),
            "lsp_promotion": by_ev.get("lsp_promotion", 0),
            "wrapper_invoked": by_ev.get("wrapper_invoked", 0),
            "pre_edit_briefing": by_ev.get("pre_edit_briefing", 0),
            "material_edit": by_ev.get("material_edit", 0),
            "auth_fail": auth_fail,
            "one_step": one_step,
            "ack_reasons": dict(ack_reason),
        }

    rows = [
        ("tasks_total", "tasks_total"),
        ("non_empty_patch_rate", "nonempty"),
        ("tasks_with_material_edit", "tasks_with_material_edit"),
        ("pre_edit_briefing", "pre_edit_briefing"),
        ("wrapper_invoked", "wrapper_invoked"),
        ("material_edit", "material_edit"),
        ("micro_emit_rate", "micro"),
        ("verify_emit_rate", "verify"),
        ("gt_orient_rate", "gt_orient"),
        ("gt_check_rate", "gt_check"),
        ("ack_followed", "ack_followed"),
        ("ack_ignored", "ack_ignored"),
        ("ack_not_observed", "ack_not_observed"),
        ("budget_denied", "budget_denied"),
        ("submit_observed", "submit_observed"),
        ("lsp_promotion", "lsp_promotion"),
        ("auth_fail_count", "auth_fail"),
        ("one_step_traj_count", "one_step"),
    ]
    for label, key in rows:
        if label == "non_empty_patch_rate":
            print(f"{label:32} {rate(metrics['nolsp']['nonempty'],5):>12} "
                  f"{rate(metrics['lsp']['nonempty'],5):>12}")
        else:
            print(f"{label:32} {metrics['nolsp'][key]:>12} {metrics['lsp'][key]:>12}")

    print()
    print("ACK REASON HISTOGRAM")
    print("-" * 58)
    for arm_name in ("nolsp", "lsp"):
        print(f"  [{arm_name}] {metrics[arm_name]['ack_reasons']}")

    # ---------- verdict ----------
    print()
    print("=" * 80)
    print("GATE 1 VERDICT (per CANARY_VERIFY.md)")
    print("=" * 80)
    def gate_pass(m: dict, arm: str) -> tuple[bool, list[str]]:
        reasons = []
        ok = True
        if m["nonempty"] < 3:
            ok = False
            reasons.append(f"non_empty_patch_rate={m['nonempty']}/5 < 50%")
        # structural events must be > 0
        for req in ("wrapper_invoked", "material_edit", "pre_edit_briefing"):
            if m[req] == 0:
                ok = False
                reasons.append(f"{req}=0")
        ack_sum = m["ack_followed"] + m["ack_ignored"]
        ack_total = ack_sum + m["ack_not_observed"]
        if ack_total == 0:
            # 0/0 is N/A — treat as unmeasured, do NOT silently pass.
            ok = False
            reasons.append("ack_unmeasured (0/0 — no ack_* events harvested)")
        elif ack_sum < 1:
            ok = False
            reasons.append(f"ack_followed+ack_ignored={ack_sum}/{ack_total} (target >=1 with *_inferred)")
        if arm == "lsp" and m["lsp_promotion"] == 0:
            ok = False
            reasons.append("lsp_promotion=0 on LSP arm")
        return ok, reasons

    ns, ns_reasons = gate_pass(metrics["nolsp"], "nolsp")
    ls, ls_reasons = gate_pass(metrics["lsp"],   "lsp")

    print(f"  nolsp : {'PASS' if ns else 'FAIL'} {('(' + '; '.join(ns_reasons) + ')') if ns_reasons else ''}")
    print(f"  lsp   : {'PASS' if ls else 'FAIL'} {('(' + '; '.join(ls_reasons) + ')') if ls_reasons else ''}")
    gate_overall = "PASS" if ns and ls else "FAIL"
    print(f"  OVERALL Gate 1: {gate_overall}")


if __name__ == "__main__":
    main()
