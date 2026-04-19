#!/usr/bin/env python3
"""Live Gate 1 MUST-item telemetry poll. Reads gt_hook_telemetry.jsonl and
trajectories for each task under an OUTDIR and prints a per-task table of
event kinds and trajectory tool-call counts.

Usage: gt_live_poll.py <outdir> <arm_label>
"""
import sys, os, json, glob, re, collections


def scan(outdir: str, arm_label: str) -> None:
    print()
    print(f"=== {arm_label} ({outdir}) ===")
    tasks = sorted(
        d for d in os.listdir(outdir)
        if os.path.isdir(os.path.join(outdir, d)) and d.startswith("astropy")
    )
    hdr_cols = [
        "task", "step", "ev", "id?",
        "or", "lk", "im", "ck",
        "brief", "ckpt", "med",
        "mi+", "mi-", "vf+", "vf-",
        "sub+", "sub-", "subx",
        "bud", "lsp",
        "ack+", "ack-", "ack0",
    ]
    widths = [30, 4, 4, 4, 3, 3, 3, 3, 5, 4, 4, 4, 4, 4, 4, 5, 5, 5, 4, 4, 4, 4, 4]
    parts = []
    for w, c in zip(widths, hdr_cols):
        parts.append(c.rjust(w) if w <= 6 else c.ljust(w))
    print(" ".join(parts))

    arm_norm = arm_label.strip().lower()
    expected_arm = "gt-nolsp" if "nolsp" in arm_norm else "gt-hybrid"
    totals = collections.Counter()

    for t in tasks:
        tel = os.path.join(outdir, t, "gt_hook_telemetry.jsonl")
        kinds = collections.Counter()
        arm_mismatch = 0
        id_missing = 0
        ev = 0
        if os.path.exists(tel):
            with open(tel, errors="ignore") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    ev += 1
                    if e.get("arm") and e.get("arm") != expected_arm:
                        arm_mismatch += 1
                    if not e.get("arm") or not e.get("run_id") or not e.get("instance_id"):
                        id_missing += 1
                    k = e.get("event") or e.get("kind") or "?"
                    kinds[k] += 1

        brief = (kinds.get("briefing_injected", 0) + kinds.get("briefing", 0)
                 + kinds.get("pre_edit_briefing", 0))
        ckpt = kinds.get("checkpoint_startup", 0) + kinds.get("startup", 0)
        medit = kinds.get("material_edit", 0)
        mi_plus = kinds.get("micro_emitted", 0)
        mi_minus = kinds.get("micro_suppressed", 0)
        vf_plus = kinds.get("verify_emitted", 0)
        vf_minus = kinds.get("verify_suppressed", 0)
        sub_plus = kinds.get("submit_observed", 0)
        sub_minus = kinds.get("submit_gate_blocked", 0)
        sub_bypass = kinds.get("submit_gate_bypassed", 0)
        bud = kinds.get("budget_denied", 0)
        lsp = kinds.get("lsp_promotion", 0)
        ackf = kinds.get("ack_followed", 0)
        acki = kinds.get("ack_ignored", 0)
        ackn = kinds.get("ack_not_observed", 0)

        trj = {"orient": 0, "lookup": 0, "impact": 0, "check": 0}
        traj_paths = (
            glob.glob(os.path.join(outdir, t, "**", "*.traj"), recursive=True)
            + glob.glob(os.path.join(outdir, t, "**", "*.traj.json"), recursive=True)
        )
        for tf in traj_paths:
            try:
                data = json.load(open(tf))
            except Exception:
                continue
            history = data.get("history") or data.get("trajectory") or []
            if not isinstance(history, list):
                continue
            for h in history:
                if not isinstance(h, dict):
                    continue
                a = h.get("action") or h.get("thought_action") or ""
                if not a:
                    continue
                a_l = a.lstrip()
                for k in trj:
                    if a_l.startswith("gt_" + k):
                        trj[k] += 1

        log = os.path.join(outdir, t, "run.log")
        step = "?"
        if os.path.exists(log):
            try:
                m = re.findall(r"STEP (\d+)", open(log, errors="ignore").read())
                if m:
                    step = m[-1]
            except Exception:
                pass

        row = [
            t, step, str(ev), str(id_missing),
            str(trj["orient"]), str(trj["lookup"]), str(trj["impact"]), str(trj["check"]),
            str(brief), str(ckpt), str(medit),
            str(mi_plus), str(mi_minus), str(vf_plus), str(vf_minus),
            str(sub_plus), str(sub_minus), str(sub_bypass),
            str(bud), str(lsp),
            str(ackf), str(acki), str(ackn),
        ]
        line_parts = []
        for w, cell in zip(widths, row):
            line_parts.append(cell.rjust(w) if w <= 6 else cell.ljust(w))
        print(" ".join(line_parts))
        if arm_mismatch:
            print(f"  [scraper cross-contam: {arm_mismatch}/{ev} events have other arm]")

        totals["ev"] += ev
        totals["id_missing"] += id_missing
        totals["brief"] += brief
        totals["ckpt"] += ckpt
        totals["medit"] += medit
        totals["mi_plus"] += mi_plus
        totals["mi_minus"] += mi_minus
        totals["vf_plus"] += vf_plus
        totals["vf_minus"] += vf_minus
        totals["sub_plus"] += sub_plus
        totals["sub_minus"] += sub_minus
        totals["sub_bypass"] += sub_bypass
        totals["bud"] += bud
        totals["lsp"] += lsp
        totals["ack_followed"] += ackf
        totals["ack_ignored"] += acki
        totals["ack_not_observed"] += ackn
        for k, v in trj.items():
            totals["trj_" + k] += v

    print(f"  TOTAL: ev={totals['ev']} id_missing={totals['id_missing']} "
          f"brief={totals['brief']} ckpt={totals['ckpt']} medit={totals['medit']} "
          f"mi+={totals['mi_plus']} mi-={totals['mi_minus']} "
          f"vf+={totals['vf_plus']} vf-={totals['vf_minus']} "
          f"sub+={totals['sub_plus']} sub-={totals['sub_minus']} subx={totals['sub_bypass']} "
          f"bud={totals['bud']} lsp={totals['lsp']} "
          f"ack+={totals['ack_followed']} ack-={totals['ack_ignored']} "
          f"ack0={totals['ack_not_observed']} "
          f"trj[or/lk/im/ck]={totals['trj_orient']}/{totals['trj_lookup']}/"
          f"{totals['trj_impact']}/{totals['trj_check']}")
    if totals["ack_followed"] + totals["ack_ignored"] + totals["ack_not_observed"] == 0:
        print("  ACK STATUS: pending - no resolved follow/ignore/not-observed windows yet; "
              "this live poll is not a final ack breakdown.")
    elif totals["ack_followed"] == 0 and totals["ack_ignored"] == 0:
        print("  ACK STATUS: unresolved - only not-observed windows have surfaced so far; "
              "this live poll is not a final follow/ignore breakdown.")


if __name__ == "__main__":
    outdir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/smoke5_nolsp"
    label = sys.argv[2] if len(sys.argv) > 2 else "NOLSP"
    scan(outdir, label)
