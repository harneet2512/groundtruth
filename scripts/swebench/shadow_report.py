#!/usr/bin/env python3
"""shadow_report.py — offline analyzer for the SS-8 shadow-holdout instrument (gt-math E10).

READ-ONLY. Reads a run's per-task runtime ledgers (``gt_runtime_ledger_<task>.jsonl``) and,
when present, the per-task trajectory (``mini-swe-agent.trajectory.json``), then PAIRS the two
shadow arms PER FACT CLASS:

    DELIVER arm  — rows ``outcome == "delivered"`` with ``chars_delivered > 0`` (real model bytes)
    HOLDOUT arm  — rows ``outcome == "shadow_holdout"`` (the withheld render; zero model bytes)

and computes the E10 contrast endpoints (delivered-vs-withheld) per class: event counts, dose
(delivered chars vs chars_would), distinct tasks, and — when a trajectory is present — three
trajectory proxies looking forward from each eligible event (subsequent action referencing the
fact's file/entity, independent reacquisition of it, and steps to the first later reference).

It NEVER runs an oracle, launches the seam, or writes into the run; it only reads. It is proven
against the arm-4 recorded shape (``D:/gt_runs/29236533134/art``), which has ZERO holdouts, so
it GRACEFULLY reports a zero-holdout run: it still classifies the DELIVER arm and states plainly
that no causal contrast is available (there is nothing to pair against).

Usage:
    python scripts/swebench/shadow_report.py <run_dir> [--json OUT.json]

``<run_dir>`` may be a run root (containing ``art/``), an ``art`` dir (containing task subdirs),
or a single task dir. The task set is discovered by locating every ``gt_runtime_ledger_*.jsonl``.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

# ── ledger vocabulary ────────────────────────────────────────────────────────
_DELIVER_OUTCOME = "delivered"
_HOLDOUT_OUTCOME = "shadow_holdout"
_LEDGER_GLOB = "gt_runtime_ledger_*.jsonl"
_LEDGER_PREFIX = "gt_runtime_ledger_"
_TRAJ_NAME = "mini-swe-agent.trajectory.json"

# search / edit verb heuristics for the trajectory proxies (chronological, per-message).
_SEARCH_TOKENS = ("grep", "rg ", "find ", "ripgrep", "ag ", "ack ", "ls ", "cat ", "less ")
_EDIT_TOKENS = (" > ", ">>", "sed -i", "apply_patch", "tee ", "patch ", "edit ", "write(")


def _canonical_class(raw: str) -> "str | None":
    """Resolve a ledger ``layer`` / explicit ``fact_class`` to its canonical fact class via the
    shared kernel (so DELIVER-arm ``layer`` and HOLDOUT-arm ``fact_class`` land in the SAME
    bucket). Falls back to the stripped raw string if the kernel is not importable."""
    try:
        from groundtruth.runtime.shadow_holdout import canonical_class
        return canonical_class(raw)
    except Exception:  # noqa: BLE001 — analyzer must run without the package on PYTHONPATH
        s = (raw or "").strip()
        if s.startswith("ga."):
            s = s[3:]
        return s or None


# --------------------------------------------------------------------------- #
# discovery + loading
# --------------------------------------------------------------------------- #
def discover_tasks(run_dir: str) -> "list[tuple[str, Path, Path | None]]":
    """Every task under ``run_dir`` as ``(task_id, ledger_path, traj_path_or_None)``, sorted by
    task id. A task is any directory containing a ``gt_runtime_ledger_*.jsonl``."""
    root = Path(run_dir)
    out: dict[str, tuple[str, Path, "Path | None"]] = {}
    # search the root and up to two levels down (root / art / <task>) for ledgers.
    for ledger in sorted(root.rglob(_LEDGER_GLOB)):
        base = ledger.name
        task = base[:-6] if base.endswith(".jsonl") else base
        if task.startswith(_LEDGER_PREFIX):
            task = task[len(_LEDGER_PREFIX):]
        if not task:  # a bare gt_runtime_ledger.jsonl (no task suffix) -> use the dir name
            task = ledger.parent.name
        traj = ledger.parent / _TRAJ_NAME
        out.setdefault(task, (task, ledger, traj if traj.exists() else None))
    return [out[k] for k in sorted(out)]


def load_ledger_rows(path: Path) -> "list[dict]":
    """The runtime-ledger rows in stored (chronological) order. Malformed lines are skipped."""
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        pass
    return rows


def load_trajectory_messages(path: "Path | None") -> "list[dict] | None":
    """The trajectory's ``messages`` list (role/content records) in order, or None when absent /
    unreadable / schema-foreign."""
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    msgs = doc.get("messages") if isinstance(doc, dict) else doc
    if isinstance(msgs, list):
        return [m for m in msgs if isinstance(m, dict)]
    return None


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def classify_events(rows: "list[dict]") -> "dict[str, dict[str, list[dict]]]":
    """Bucket ledger rows into per-class DELIVER / HOLDOUT arms.

    DELIVER = ``outcome=="delivered"`` AND ``chars_delivered>0`` (real model bytes; excludes the
    host-side ``ga.*`` repair_support telemetry rows which carry chars=0). HOLDOUT =
    ``outcome=="shadow_holdout"``. Class = canonical(explicit ``fact_class`` else ``layer``)."""
    out: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        outcome = r.get("outcome")
        if outcome == _DELIVER_OUTCOME and int(r.get("chars_delivered") or 0) > 0:
            arm = "deliver"
        elif outcome == _HOLDOUT_OUTCOME:
            arm = "holdout"
        else:
            continue
        cls = _canonical_class(r.get("fact_class") or r.get("layer") or "") or "unknown"
        out.setdefault(cls, {"deliver": [], "holdout": []})[arm].append(r)
    return out


# --------------------------------------------------------------------------- #
# trajectory proxies (forward-looking from an eligible event; chronological)
# --------------------------------------------------------------------------- #
def _assistant_command(msg: dict) -> str:
    c = msg.get("content")
    return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)


def _event_endpoints(row: dict, msgs: "list[dict] | None") -> "dict[str, object]":
    """The forward-looking trajectory proxies for ONE eligible event. All PROXIES (heuristic,
    labelled as such): ``referenced`` (a later assistant action names the fact's file basename),
    ``reacquired`` (a later assistant action SEARCHES for it), ``steps_to_reference`` (assistant
    turns until the first later reference, or None). ``UNMEASURED`` when there is no trajectory
    or the event carries no file entity."""
    fp = (row.get("file_path") or "").strip()
    if msgs is None:
        return {"measured": False, "reason": "no_trajectory"}
    entity = os.path.basename(fp) if fp else ""
    if not entity:
        return {"measured": False, "reason": "no_entity"}
    assistant_ordinals = [i for i, m in enumerate(msgs) if m.get("role") == "assistant"]
    iteration = int(row.get("iteration") or 0)
    # the event rode the observation following assistant action #iteration (1-based); look at
    # assistant actions strictly AFTER it. Out-of-range iteration -> look at all assistant msgs.
    start = iteration if 0 <= iteration <= len(assistant_ordinals) else 0
    later = assistant_ordinals[start:]
    referenced = False
    reacquired = False
    steps_to_reference: "int | None" = None
    for step, msg_idx in enumerate(later, start=1):
        cmd = _assistant_command(msgs[msg_idx])
        if entity and entity in cmd:
            if not referenced:
                referenced = True
                steps_to_reference = step
            if any(tok in cmd for tok in _SEARCH_TOKENS):
                reacquired = True
    return {
        "measured": True,
        "referenced": referenced,
        "reacquired": reacquired,
        "steps_to_reference": steps_to_reference,
    }


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def build_report(run_dir: str) -> dict:
    """The full read-only shadow-holdout report for a run dir."""
    tasks = discover_tasks(run_dir)
    # per-class accumulation across tasks; trajectory endpoints computed per-task then pooled.
    per_class: dict[str, dict] = {}
    total_deliver = total_holdout = 0
    tasks_with_holdout: set[str] = set()

    for task_id, ledger_path, traj_path in tasks:
        rows = load_ledger_rows(ledger_path)
        msgs = load_trajectory_messages(traj_path)
        events = classify_events(rows)
        for cls, arms in events.items():
            slot = per_class.setdefault(cls, {
                "deliver_rows": [], "holdout_rows": [],
                "deliver_endpoints": [], "holdout_endpoints": [],
                "deliver_tasks": set(), "holdout_tasks": set(),
            })
            for r in arms["deliver"]:
                slot["deliver_rows"].append(r)
                slot["deliver_endpoints"].append(_event_endpoints(r, msgs))
                slot["deliver_tasks"].add(task_id)
                total_deliver += 1
            for r in arms["holdout"]:
                slot["holdout_rows"].append(r)
                slot["holdout_endpoints"].append(_event_endpoints(r, msgs))
                slot["holdout_tasks"].add(task_id)
                tasks_with_holdout.add(task_id)
                total_holdout += 1

    classes = {}
    for cls, slot in sorted(per_class.items()):
        deliver = _pool_arm(slot["deliver_rows"], slot["deliver_endpoints"], "chars_delivered")
        holdout = _pool_arm(slot["holdout_rows"], slot["holdout_endpoints"], "chars_would")
        deliver["distinct_tasks"] = len(slot["deliver_tasks"])
        holdout["distinct_tasks"] = len(slot["holdout_tasks"])
        classes[cls] = {
            "deliver": deliver,
            "holdout": holdout,
            "contrast_available": deliver["n"] > 0 and holdout["n"] > 0,
        }

    return {
        "schema": "gt.shadow_report.v1",
        "run_dir": str(Path(run_dir)),
        "tasks_discovered": len(tasks),
        "tasks_with_holdout": len(tasks_with_holdout),
        "total_deliver_events": total_deliver,
        "total_holdout_events": total_holdout,
        "zero_holdout_run": total_holdout == 0,
        "classes": classes,
    }


def _pool_arm(rows: "list[dict]", endpoints: "list[dict]", chars_key: str) -> dict:
    n = len(rows)
    chars = sum(int(r.get(chars_key) or 0) for r in rows)
    measured = [e for e in endpoints if e.get("measured")]
    def _rate(field: str) -> "float | None":
        vals = [1.0 if e.get(field) else 0.0 for e in measured]
        return round(statistics.mean(vals), 6) if vals else None
    steps = [e["steps_to_reference"] for e in measured
             if e.get("steps_to_reference") is not None]
    return {
        "n": n,
        "chars": chars,
        "measured_events": len(measured),
        "referenced_rate": _rate("referenced"),
        "reacquired_rate": _rate("reacquired"),
        "median_steps_to_reference": (round(statistics.median(steps), 4) if steps else None),
    }


def render_text(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"SHADOW-HOLDOUT REPORT (E10)  run={report['run_dir']}")
    lines.append(
        f"tasks={report['tasks_discovered']}  "
        f"deliver_events={report['total_deliver_events']}  "
        f"holdout_events={report['total_holdout_events']}  "
        f"tasks_with_holdout={report['tasks_with_holdout']}"
    )
    if report["zero_holdout_run"]:
        lines.append(
            "ZERO-HOLDOUT RUN: no delivered-vs-withheld contrast is available (nothing was "
            "withheld -- GT_SS_SHADOW off or GT_SS_SHADOW_RATE=0). Reporting the DELIVER-arm "
            "baseline distribution per class only."
        )
    lines.append("")
    hdr = (f"{'class':<18} {'del_n':>6} {'hld_n':>6} {'del_ch':>8} {'would_ch':>9} "
           f"{'del_ref':>8} {'hld_ref':>8} {'contrast':>9}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for cls, blk in report["classes"].items():
        d, h = blk["deliver"], blk["holdout"]
        lines.append(
            f"{cls:<18} {d['n']:>6} {h['n']:>6} {d['chars']:>8} {h['chars']:>9} "
            f"{_fmt(d['referenced_rate']):>8} {_fmt(h['referenced_rate']):>8} "
            f"{('YES' if blk['contrast_available'] else 'no'):>9}"
        )
    return "\n".join(lines)


def _fmt(v: "float | None") -> str:
    return "n/a" if v is None else f"{v:.3f}"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Offline shadow-holdout (E10) analyzer.")
    ap.add_argument("run_dir", help="run root, art dir, or a single task dir")
    ap.add_argument("--json", dest="json_out", default="", help="write the full report JSON here")
    args = ap.parse_args(argv)
    if not Path(args.run_dir).exists():
        print(f"shadow_report: run_dir not found: {args.run_dir}", file=sys.stderr)
        return 2
    report = build_report(args.run_dir)
    print(render_text(report))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True, default=list)
        print(f"\n[wrote {args.json_out}]")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
