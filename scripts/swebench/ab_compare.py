#!/usr/bin/env python3
"""A/B comparator for strict-on vs strict-off freshness runs.

Pairs 4 archives:
  nolsp_strict_on   vs  nolsp_strict_off
  lsp_strict_on     vs  lsp_strict_off

For each lane, loads gt_hook_telemetry.jsonl + preds.json across tasks, then:
  1. Lane-by-lane summary (reindex outcomes, stale_leak, mechanism totals,
     followed_detector, resolved, has_patch).
  2. Strict-on vs strict-off diff for each arm.
  3. Per-task delta table (resolved / has_patch / followed shift).
  4. Recommendation tied to user-specified pass criteria.

Usage:
    python3 scripts/swebench/ab_compare.py \\
        --nolsp-on   benchmarks/swebench/cd_ab/phase_a_nolsp \\
        --nolsp-off  benchmarks/swebench/cd_ab/phase_b_nolsp \\
        --lsp-on     benchmarks/swebench/cd_ab/phase_a_lsp \\
        --lsp-off    benchmarks/swebench/cd_ab/phase_b_lsp \\
        --resolved-json-on-nolsp  ... [optional; from swe-bench harness eval]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Reuse followed_detector for per-steer metrics
sys.path.insert(0, str(Path(__file__).parent))
from followed_detector import analyze_archive as fd_analyze  # type: ignore


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def collect_lane(archive_root: Path) -> dict:
    """Build a single-lane summary from all tasks in an archive."""
    # Heuristic: find the repeat_1 subdir if present
    base = archive_root
    rep = base / "repeat_1"
    if rep.is_dir():
        base = rep

    task_dirs = sorted([p for p in base.iterdir() if p.is_dir() and "__" in p.name])
    reindex_outcomes = Counter()
    stale_leak_events = 0
    material_edit = 0
    steer_delivered = 0
    ack_engagement = 0
    ack_armed = 0
    killed = 0

    # Killed tasks file at the archive root
    killed_file = base / "killed_tasks.jsonl"
    if killed_file.exists():
        killed = len(_read_jsonl(killed_file))

    per_task_signal: dict[str, dict] = {}

    for td in task_dirs:
        tel = _read_jsonl(td / "gt_hook_telemetry.jsonl")
        task_re = Counter()
        task_leak = 0
        task_me = 0
        task_sd = 0
        task_ae = 0
        task_aa = 0
        for ev in tel:
            et = ev.get("event", "")
            if et == "reindex_result":
                task_re[ev.get("outcome", "?")] += 1
                reindex_outcomes[ev.get("outcome", "?")] += 1
            elif et == "stale_leak_detected":
                task_leak += 1
                stale_leak_events += 1
            elif et == "material_edit":
                task_me += 1
                material_edit += 1
            elif et == "steer_delivered":
                task_sd += 1
                steer_delivered += 1
            elif et == "ack_engagement":
                task_ae += 1
                ack_engagement += 1
            elif et == "ack_armed":
                task_aa += 1
                ack_armed += 1
        per_task_signal[td.name] = {
            "reindex_outcomes": dict(task_re),
            "stale_leak": task_leak,
            "material_edit": task_me,
            "steer_delivered": task_sd,
            "ack_engagement": task_ae,
            "ack_armed": task_aa,
        }

    # Preds
    preds_path = base / "preds.json"
    preds: dict[str, dict] = {}
    if preds_path.exists():
        try:
            preds = json.loads(preds_path.read_text(encoding="utf-8"))
        except Exception:
            preds = {}
    has_patch = {tid: bool((rec.get("model_patch") or "").strip())
                 for tid, rec in preds.items()}

    # Followed detector
    fd = fd_analyze(base)

    return {
        "archive": str(archive_root),
        "task_count": len(task_dirs),
        "reindex_outcomes": dict(reindex_outcomes),
        "stale_leak_events": stale_leak_events,
        "killed": killed,
        "totals": {
            "material_edit": material_edit,
            "steer_delivered": steer_delivered,
            "ack_engagement": ack_engagement,
            "ack_armed": ack_armed,
        },
        "followed_detector": {
            "total_delivered": fd.get("total_delivered", 0),
            "total_engaged": fd.get("total_engaged", 0),
            "total_targeted_edit": fd.get("total_targeted_edit", 0),
            "total_verification": fd.get("total_verification_ran", 0),
            "tasks_any_targeted_edit": fd.get("tasks_any_targeted_edit", 0),
            "tasks_ran_pytest": fd.get("tasks_ran_pytest", 0),
        },
        "preds": {
            "has_patch_count": sum(1 for v in has_patch.values() if v),
            "total": len(has_patch),
            "per_task": has_patch,
        },
        "per_task": per_task_signal,
    }


def render_lane_table(lanes: dict[str, dict]) -> str:
    # Columns we care about per lane
    lines = []
    lines.append("## 1. Lane-by-lane summary")
    lines.append("")
    hdr = ("| lane | tasks | reindex outcomes | stale_leak | material_edit | steer_delivered | ack_engagement | "
           "delivered (det) | targeted_edit (det) | has_patch | killed |")
    sep = "|---|:-:|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|"
    lines.append(hdr)
    lines.append(sep)
    for name, L in lanes.items():
        ro = ", ".join(f"{k}={v}" for k, v in sorted(L["reindex_outcomes"].items())) or "—"
        t = L["totals"]
        fd = L["followed_detector"]
        pr = L["preds"]
        lines.append(
            f"| {name} | {L['task_count']} | {ro} | {L['stale_leak_events']} | "
            f"{t['material_edit']} | {t['steer_delivered']} | {t['ack_engagement']} | "
            f"{fd['total_delivered']} | {fd['total_targeted_edit']} | "
            f"{pr['has_patch_count']}/{pr['total']} | {L['killed']} |"
        )
    return "\n".join(lines)


def render_arm_diff(lanes: dict[str, dict], arm: str) -> str:
    on = lanes[f"{arm}_strict_on"]
    off = lanes[f"{arm}_strict_off"]
    lines = []
    lines.append(f"## 2.{'' if arm == 'nolsp' else 'b'} Strict-on vs strict-off diff ({arm})")
    lines.append("")
    lines.append("| metric | strict_on | strict_off | delta |")
    lines.append("|---|:-:|:-:|:-:|")
    def row(label, onv, offv):
        d = (onv - offv) if isinstance(onv, (int, float)) and isinstance(offv, (int, float)) else "—"
        lines.append(f"| {label} | {onv} | {offv} | {d} |")

    row("reindex stale_no_indexer", on["reindex_outcomes"].get("stale_no_indexer", 0),
        off["reindex_outcomes"].get("stale_no_indexer", 0))
    row("reindex fresh", on["reindex_outcomes"].get("fresh", 0),
        off["reindex_outcomes"].get("fresh", 0))
    row("stale_leak_detected", on["stale_leak_events"], off["stale_leak_events"])
    row("material_edit", on["totals"]["material_edit"], off["totals"]["material_edit"])
    row("steer_delivered", on["totals"]["steer_delivered"], off["totals"]["steer_delivered"])
    row("ack_engagement", on["totals"]["ack_engagement"], off["totals"]["ack_engagement"])
    row("followed.delivered", on["followed_detector"]["total_delivered"],
        off["followed_detector"]["total_delivered"])
    row("followed.targeted_edit", on["followed_detector"]["total_targeted_edit"],
        off["followed_detector"]["total_targeted_edit"])
    row("has_patch", on["preds"]["has_patch_count"], off["preds"]["has_patch_count"])
    row("killed", on["killed"], off["killed"])
    return "\n".join(lines)


def render_per_task(lanes: dict[str, dict], arm: str) -> str:
    on = lanes[f"{arm}_strict_on"]
    off = lanes[f"{arm}_strict_off"]
    all_tasks = sorted(set(on["preds"]["per_task"].keys()) | set(off["preds"]["per_task"].keys())
                       | set(on["per_task"].keys()) | set(off["per_task"].keys()))
    lines = []
    lines.append(f"## 3.{'' if arm == 'nolsp' else 'b'} Per-task delta ({arm})")
    lines.append("")
    lines.append("| task | on.patch | off.patch | on.me | off.me | on.sd | off.sd | on.eng | off.eng | on.leak | off.leak |")
    lines.append("|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|")
    for t in all_tasks:
        onp = on["preds"]["per_task"].get(t, False)
        offp = off["preds"]["per_task"].get(t, False)
        onT = on["per_task"].get(t, {})
        offT = off["per_task"].get(t, {})
        lines.append(
            f"| {t} | {'Y' if onp else '-'} | {'Y' if offp else '-'} | "
            f"{onT.get('material_edit', 0)} | {offT.get('material_edit', 0)} | "
            f"{onT.get('steer_delivered', 0)} | {offT.get('steer_delivered', 0)} | "
            f"{onT.get('ack_engagement', 0)} | {offT.get('ack_engagement', 0)} | "
            f"{onT.get('stale_leak', 0)} | {offT.get('stale_leak', 0)} |"
        )
    return "\n".join(lines)


def render_recommendation(lanes: dict[str, dict], resolved: dict[str, int]) -> str:
    lines = []
    lines.append("## 4. Recommendation")
    lines.append("")
    # Success criteria from user:
    #   - strict-on stale_leak_detected = 0
    #   - stale outcomes become explicit WITHHELD instead of leaking
    #   - no major collapse in material_edit / steer_delivered / ack_engagement
    #   - resolved_count same or better, or patch quality visibly improves
    on_leak = lanes["nolsp_strict_on"]["stale_leak_events"] + lanes["lsp_strict_on"]["stale_leak_events"]
    off_leak = lanes["nolsp_strict_off"]["stale_leak_events"] + lanes["lsp_strict_off"]["stale_leak_events"]

    def _totals(lane: str, key: str) -> int:
        return lanes[lane]["totals"][key]

    arms = []
    for arm in ("nolsp", "lsp"):
        on_me = _totals(f"{arm}_strict_on", "material_edit")
        off_me = _totals(f"{arm}_strict_off", "material_edit")
        on_sd = _totals(f"{arm}_strict_on", "steer_delivered")
        off_sd = _totals(f"{arm}_strict_off", "steer_delivered")
        on_eng = _totals(f"{arm}_strict_on", "ack_engagement")
        off_eng = _totals(f"{arm}_strict_off", "ack_engagement")
        r_on = resolved.get(f"{arm}_strict_on", -1)
        r_off = resolved.get(f"{arm}_strict_off", -1)

        collapse = any([
            on_me < max(1, int(off_me * 0.8)),
            on_sd < max(1, int(off_sd * 0.8)),
            on_eng < max(1, int(off_eng * 0.8)),
        ])
        resolved_delta = (r_on - r_off) if (r_on >= 0 and r_off >= 0) else None
        arms.append((arm, collapse, r_on, r_off, resolved_delta))

    lines.append(f"- **stale_leak strict-on total:** {on_leak}  (target: 0)")
    lines.append(f"- **stale_leak strict-off total:** {off_leak}")
    for arm, collapse, r_on, r_off, delta in arms:
        lines.append(f"- **{arm}** resolved strict_on={r_on} strict_off={r_off} delta={delta}; "
                     f"mechanism collapse: {'YES (≥20% drop)' if collapse else 'no'}")

    # Simple verdict heuristic
    verdict_bits = []
    if on_leak == 0:
        verdict_bits.append("strict-on eliminated stale leakage")
    else:
        verdict_bits.append(f"strict-on still leaked {on_leak}×")
    if all(not x[1] for x in arms):
        verdict_bits.append("no mechanism collapse")
    if all(x[4] is not None and x[4] >= 0 for x in arms):
        verdict_bits.append("resolved same-or-better")

    if ("strict-on eliminated stale leakage" in verdict_bits and
        "no mechanism collapse" in verdict_bits and
        all(x[4] is None or x[4] >= 0 for x in arms)):
        verdict = "**KEEP strict freshness** — all success criteria met."
    elif on_leak == 0 and not all(x[4] is None or x[4] >= 0 for x in arms):
        verdict = "**SOFTEN strict freshness** — leak gone but resolved regressed; consider showing stale with [STALE] tag instead of withholding."
    else:
        verdict = "**REVERT strict freshness** — criteria not met."
    lines.append("")
    lines.append(f"**Verdict:** {verdict}")
    lines.append("")
    lines.append("Signals: " + "; ".join(verdict_bits))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B compare strict-on vs strict-off")
    ap.add_argument("--nolsp-on", required=True)
    ap.add_argument("--nolsp-off", required=True)
    ap.add_argument("--lsp-on", required=True)
    ap.add_argument("--lsp-off", required=True)
    ap.add_argument("--resolved-nolsp-on", type=int, default=-1)
    ap.add_argument("--resolved-nolsp-off", type=int, default=-1)
    ap.add_argument("--resolved-lsp-on", type=int, default=-1)
    ap.add_argument("--resolved-lsp-off", type=int, default=-1)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    lanes = {
        "nolsp_strict_on": collect_lane(Path(args.nolsp_on).resolve()),
        "nolsp_strict_off": collect_lane(Path(args.nolsp_off).resolve()),
        "lsp_strict_on": collect_lane(Path(args.lsp_on).resolve()),
        "lsp_strict_off": collect_lane(Path(args.lsp_off).resolve()),
    }
    resolved = {
        "nolsp_strict_on": args.resolved_nolsp_on,
        "nolsp_strict_off": args.resolved_nolsp_off,
        "lsp_strict_on": args.resolved_lsp_on,
        "lsp_strict_off": args.resolved_lsp_off,
    }
    parts = [
        "# C+D Freshness A/B — strict_on vs strict_off",
        "",
        render_lane_table(lanes),
        "",
        render_arm_diff(lanes, "nolsp"),
        "",
        render_arm_diff(lanes, "lsp"),
        "",
        render_per_task(lanes, "nolsp"),
        "",
        render_per_task(lanes, "lsp"),
        "",
        render_recommendation(lanes, resolved),
    ]
    out = "\n".join(parts)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"wrote: {args.out}")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
