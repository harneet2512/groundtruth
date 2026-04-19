#!/usr/bin/env python3
"""Live Gate 1 MUST-item telemetry poll.

Reads gt_task_log.json as the primary source of truth for each task under an
OUTDIR, and uses gt_hook_telemetry.jsonl only as a fallback when a field is not
present in the task log. Prints a per-task table of hook counts and trajectory
tool-call counts.

Usage: gt_live_poll.py <outdir> <arm_label>
"""
import sys, os, json, glob, re, collections


def _load_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, errors="ignore") as f:
                return json.load(f)
    except Exception:
        return None
    return None


def _load_telemetry_counts(path: str) -> tuple[collections.Counter, int, int]:
    kinds = collections.Counter()
    arm_mismatch = 0
    id_missing = 0
    ev = 0
    if os.path.exists(path):
        with open(path, errors="ignore") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ev += 1
                k = e.get("event") or e.get("kind") or "?"
                kinds[k] += 1
                if not e.get("arm") or not e.get("run_id") or not e.get("instance_id"):
                    id_missing += 1
    return kinds, ev, id_missing


def scan(outdir: str, arm_label: str) -> None:
    print()
    print(f"=== {arm_label} ({outdir}) ===")
    print("PRIMARY SOURCE: gt_task_log.json (telemetry fallback only when a field is missing)")
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
    present = collections.Counter()

    for t in tasks:
        task_dir = os.path.join(outdir, t)
        task_log = _load_json(os.path.join(task_dir, "gt_task_log.json")) or {}
        tel = os.path.join(task_dir, "gt_hook_telemetry.jsonl")
        tele_kinds, tele_ev, tele_id_missing = _load_telemetry_counts(tel)
        log_event_counts = task_log.get("event_counts") if isinstance(task_log.get("event_counts"), dict) else {}
        has_task_event_counts = bool(log_event_counts)

        # Prefer the task log for summary/state fields; fall back to telemetry
        # only when the task log lacks a count.
        def from_log_or_tele(key: str, tele_key: str | None = None):
            if key in task_log and isinstance(task_log[key], int):
                return task_log[key]
            if key == "brief" and isinstance(task_log.get("briefing"), dict):
                fired = task_log["briefing"].get("fired")
                if fired is not None:
                    return 1 if fired else 0
            if key == "ckpt":
                if isinstance(task_log.get("checkpoint_startup_count"), int):
                    return task_log["checkpoint_startup_count"]
            if key == "medit":
                if isinstance(task_log.get("material_edit_count"), int):
                    return task_log["material_edit_count"]
            if key == "mi+":
                if isinstance(task_log.get("micro_emit_count"), int):
                    return task_log["micro_emit_count"]
            if key == "mi-":
                if isinstance(task_log.get("micro_suppress_count"), int):
                    return task_log["micro_suppress_count"]
            if key == "vf+":
                if isinstance(task_log.get("verify_emit_count"), int):
                    return task_log["verify_emit_count"]
            if key == "vf-":
                if isinstance(task_log.get("verify_suppress_count"), int):
                    return task_log["verify_suppress_count"]
            if key == "sub+":
                if isinstance(task_log.get("submit_observed_count"), int):
                    return task_log["submit_observed_count"]
            if key == "sub-":
                if isinstance(task_log.get("submit_gate_blocked_count"), int):
                    return task_log["submit_gate_blocked_count"]
            if key == "subx":
                if isinstance(task_log.get("submit_gate_bypassed_count"), int):
                    return task_log["submit_gate_bypassed_count"]
            if key == "bud":
                if isinstance(task_log.get("budget_denied_count"), int):
                    return task_log["budget_denied_count"]
            if key == "lsp":
                if isinstance(task_log.get("lsp_promotion_count"), int):
                    return task_log["lsp_promotion_count"]
            if key == "ack+":
                if isinstance(task_log.get("ack_followed_count"), int):
                    return task_log["ack_followed_count"]
            if key == "ack-":
                if isinstance(task_log.get("ack_ignored_count"), int):
                    return task_log["ack_ignored_count"]
            if key == "ack0":
                if isinstance(task_log.get("ack_not_observed_count"), int):
                    return task_log["ack_not_observed_count"]

            # Fallback to telemetry events if the task log does not have a
            # dedicated field.
            if tele_key is None:
                tele_key = key
            if tele_key in tele_kinds:
                return tele_kinds.get(tele_key, 0)
            return None

        # Support the existing per-task summary fields from gt_task_log.json.
        tool_summary = task_log.get("tool_calls_summary")
        if not isinstance(tool_summary, dict):
            tool_summary = {}

        def tool_count(name: str):
            v = tool_summary.get(name)
            if isinstance(v, int):
                return v
            return None

        arm_mismatch = 0
        id_missing = tele_id_missing if not task_log else 0
        ev = tele_ev if not has_task_event_counts else sum(log_event_counts.values())

        brief = from_log_or_tele("brief", "briefing")
        ckpt = from_log_or_tele("ckpt", "checkpoint_startup")
        medit = from_log_or_tele("medit", "material_edit")
        mi_plus = from_log_or_tele("mi+", "micro_emitted")
        mi_minus = from_log_or_tele("mi-", "micro_suppressed")
        vf_plus = from_log_or_tele("vf+", "verify_emitted")
        vf_minus = from_log_or_tele("vf-", "verify_suppressed")
        sub_plus = from_log_or_tele("sub+", "submit_observed")
        sub_minus = from_log_or_tele("sub-", "submit_gate_blocked")
        sub_bypass = from_log_or_tele("subx", "submit_gate_bypassed")
        bud = from_log_or_tele("bud", "budget_denied")
        lsp = from_log_or_tele("lsp", "lsp_promotion")
        ackf = from_log_or_tele("ack+", "ack_followed")
        acki = from_log_or_tele("ack-", "ack_ignored")
        ackn = from_log_or_tele("ack0", "ack_not_observed")

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

        tool_orient = tool_count("orient") if tool_count("orient") is not None else trj["orient"]
        tool_lookup = tool_count("lookup") if tool_count("lookup") is not None else trj["lookup"]
        tool_impact = tool_count("impact") if tool_count("impact") is not None else trj["impact"]
        tool_check = tool_count("check") if tool_count("check") is not None else trj["check"]

        row = [
            t, step, str(ev), str(id_missing),
            str(tool_orient),
            str(tool_lookup),
            str(tool_impact),
            str(tool_check),
            str(brief) if brief is not None else "--",
            str(ckpt) if ckpt is not None else "--",
            str(medit) if medit is not None else "--",
            str(mi_plus) if mi_plus is not None else "--",
            str(mi_minus) if mi_minus is not None else "--",
            str(vf_plus) if vf_plus is not None else "--",
            str(vf_minus) if vf_minus is not None else "--",
            str(sub_plus) if sub_plus is not None else "--",
            str(sub_minus) if sub_minus is not None else "--",
            str(sub_bypass) if sub_bypass is not None else "--",
            str(bud) if bud is not None else "--",
            str(lsp) if lsp is not None else "--",
            str(ackf) if ackf is not None else "--",
            str(acki) if acki is not None else "--",
            str(ackn) if ackn is not None else "--",
        ]
        line_parts = []
        for w, cell in zip(widths, row):
            line_parts.append(cell.rjust(w) if w <= 6 else cell.ljust(w))
        print(" ".join(line_parts))
        if arm_mismatch:
            print(f"  [scraper cross-contam: {arm_mismatch}/{ev} events have other arm]")

        totals["ev"] += ev
        totals["id_missing"] += id_missing
        totals["trj_orient"] += tool_orient
        totals["trj_lookup"] += tool_lookup
        totals["trj_impact"] += tool_impact
        totals["trj_check"] += tool_check
        for name, value in [
            ("brief", brief), ("ckpt", ckpt), ("medit", medit),
            ("mi_plus", mi_plus), ("mi_minus", mi_minus),
            ("vf_plus", vf_plus), ("vf_minus", vf_minus),
            ("sub_plus", sub_plus), ("sub_minus", sub_minus), ("sub_bypass", sub_bypass),
            ("bud", bud), ("lsp", lsp),
            ("ack_followed", ackf), ("ack_ignored", acki), ("ack_not_observed", ackn),
        ]:
            if isinstance(value, int):
                totals[name] += value
                present[name] += 1

    def fmt_total(name: str) -> str:
        return str(totals[name]) if present[name] > 0 else "--"

    print(f"  TOTAL: ev={totals['ev']} id_missing={totals['id_missing']} "
          f"brief={fmt_total('brief')} ckpt={fmt_total('ckpt')} medit={fmt_total('medit')} "
          f"mi+={fmt_total('mi_plus')} mi-={fmt_total('mi_minus')} "
          f"vf+={fmt_total('vf_plus')} vf-={fmt_total('vf_minus')} "
          f"sub+={fmt_total('sub_plus')} sub-={fmt_total('sub_minus')} subx={fmt_total('sub_bypass')} "
          f"bud={fmt_total('bud')} lsp={fmt_total('lsp')} "
          f"ack+={fmt_total('ack_followed')} ack-={fmt_total('ack_ignored')} "
          f"ack0={fmt_total('ack_not_observed')} "
          f"trj[or/lk/im/ck]={totals['trj_orient']}/{totals['trj_lookup']}/"
          f"{totals['trj_impact']}/{totals['trj_check']}")
    if present["ack_followed"] + present["ack_ignored"] + present["ack_not_observed"] == 0:
        print("  ACK STATUS: unavailable - ack fields are not present in the task log yet "
              "and telemetry fallback is missing for this poll.")
    elif totals["ack_followed"] == 0 and totals["ack_ignored"] == 0:
        print("  ACK STATUS: unresolved - only not-observed windows have surfaced so far; "
              "this live poll is not a final follow/ignore breakdown.")


if __name__ == "__main__":
    outdir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/smoke5_nolsp"
    label = sys.argv[2] if len(sys.argv) > 2 else "NOLSP"
    scan(outdir, label)
