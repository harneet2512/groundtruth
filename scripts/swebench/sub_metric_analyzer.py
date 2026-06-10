#!/usr/bin/env python3
"""Sub-metric analyzer for GT follow-through.

Computes three metrics per task from existing local archive files:

  localization_followed  — agent patched at least one file GT recommended
  contract_followed      — agent preserved contracts GT warned about (or N/A)
  verification_followed  — agent opened callers / ran tests GT suggested (or N/A)

Exists because `ack_followed` in the hook is broken: its `targeted_edit_inferred`
path under-counts behavioral follow-through. Direct inspection of telemetry +
patch for Wave 1+5 NOLSP resolved tasks shows the agent DID edit the file GT
recommended, but the hook reported `ack_not_observed` / `expiry_no_activity`.
See plans/we-are-switching-to-glowing-quilt.md for the verdict + evidence.

Usage:
    python3 scripts/swebench/sub_metric_analyzer.py <archive_dir> [<archive_dir> ...]
    python3 scripts/swebench/sub_metric_analyzer.py --json benchmarks/swebench/wave_1_5_nolsp/

Zero cost: local files only, no VM, no LLM.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
VIEW_CMDS = re.compile(r"^\s*(view_file|cat|less|head|tail|sed\s+-n|grep\s+-n)\b")
PYTEST_CMDS = re.compile(r"\bpytest\b|\bpython\s+-m\s+pytest\b|\bunittest\b")
CONTRACT_KEYWORDS = ("MUST", "PRESERVE", "return type", "OBLIGATION", "contract",
                     "DO NOT change return", "return shape")


def _basename(p: str) -> str:
    """Normalize a path to its basename for suffix-match comparison."""
    if not p:
        return ""
    return p.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _suffix_match(a: str, b: str) -> bool:
    """True if a and b share the same basename (hook's file-match heuristic)."""
    if not a or not b:
        return False
    ba, bb = _basename(a), _basename(b)
    return bool(ba) and ba == bb


def parse_telemetry(telemetry_path: Path) -> dict:
    """Extract GT-recommended files/symbols/contracts/verification cues from telemetry."""
    recommended_files: set[str] = set()
    recommended_symbols: set[str] = set()
    contract_hints: list[str] = []
    verification_targets: set[str] = set()  # callers / test files suggested
    ack_followed_events = 0
    ack_not_observed_events = 0
    ack_ignored_events = 0
    steer_delivered_count = 0
    ack_armed_count = 0

    if not telemetry_path.exists():
        return {
            "recommended_files": [],
            "recommended_symbols": [],
            "contract_hints": [],
            "verification_targets": [],
            "ack_followed_raw": 0,
            "ack_not_observed_raw": 0,
            "ack_ignored_raw": 0,
            "steer_delivered_raw": 0,
            "ack_armed_raw": 0,
            "telemetry_line_count": 0,
        }

    lines = telemetry_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue

        event = j.get("event", "")

        if event == "ack_followed":
            ack_followed_events += 1
        elif event == "ack_not_observed":
            ack_not_observed_events += 1
        elif event == "ack_ignored":
            ack_ignored_events += 1
        elif event == "steer_delivered":
            steer_delivered_count += 1
        elif event == "ack_armed":
            ack_armed_count += 1

        # Recommended target from expected_next_action or file field
        ena = j.get("expected_next_action") or {}
        tgt = ena.get("target") if isinstance(ena, dict) else None
        if tgt:
            recommended_files.add(tgt)
        f = j.get("file")
        if f:
            recommended_files.add(f)
        sym = j.get("symbol") or (ena.get("symbol") if isinstance(ena, dict) else None)
        if sym:
            recommended_symbols.add(sym)

        # Contract hints from evidence_preview / briefing text
        preview = j.get("evidence_preview") or ""
        if any(kw in preview for kw in CONTRACT_KEYWORDS):
            contract_hints.append(preview[:200])

        # Verification cues: expected_next_action_text mentions gt_check / callers
        ena_text = ena.get("text", "") if isinstance(ena, dict) else ""
        if ena_text:
            if "gt_check" in ena_text or "gt_lookup" in ena_text:
                # Extract any file path argument
                for tok in ena_text.split():
                    if "/" in tok and "." in tok:
                        verification_targets.add(tok)

    return {
        "recommended_files": sorted(recommended_files),
        "recommended_symbols": sorted(recommended_symbols),
        "contract_hints": contract_hints[:10],
        "verification_targets": sorted(verification_targets),
        "ack_followed_raw": ack_followed_events,
        "ack_not_observed_raw": ack_not_observed_events,
        "ack_ignored_raw": ack_ignored_events,
        "steer_delivered_raw": steer_delivered_count,
        "ack_armed_raw": ack_armed_count,
        "telemetry_line_count": len(lines),
    }


def parse_patch(patch_path: Path) -> dict:
    """Extract edited files + a copy of the diff body."""
    if not patch_path.exists():
        return {"edited_files": [], "patch_present": False, "diff_text": ""}

    text = patch_path.read_text(encoding="utf-8", errors="replace")
    edited = []
    for m in DIFF_HEADER.finditer(text):
        # Use the "b/" side (post-edit) as canonical
        edited.append(m.group(2))

    return {
        "edited_files": edited,
        "patch_present": bool(text.strip()),
        "diff_text": text,
    }


def parse_trajectory(traj_path: Path) -> dict:
    """Extract agent actions from SWE-agent trajectory for verification-metric scoring."""
    if not traj_path.exists():
        return {"actions": [], "total_steps": 0, "ran_any_pytest": False,
                "opened_files": [], "traj_present": False}

    try:
        data = json.loads(traj_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {"actions": [], "total_steps": 0, "ran_any_pytest": False,
                "opened_files": [], "traj_present": False}

    steps = data.get("trajectory") if isinstance(data, dict) else data
    if not isinstance(steps, list):
        return {"actions": [], "total_steps": 0, "ran_any_pytest": False,
                "opened_files": [], "traj_present": False}

    actions: list[str] = []
    opened: list[str] = []
    ran_pytest = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        raw_action = step.get("action") or ""
        action = str(raw_action).strip() if raw_action else ""
        if not action:
            continue
        actions.append(action[:200])
        if PYTEST_CMDS.search(action):
            ran_pytest = True
        # Naive path extraction: any token with / and .
        for tok in action.split():
            if "/" in tok and "." in tok and not tok.startswith("-"):
                opened.append(tok.strip(",;'\""))

    return {
        "actions": actions,
        "total_steps": len(actions),
        "ran_any_pytest": ran_pytest,
        "opened_files": list(dict.fromkeys(opened)),  # dedupe preserving order
        "traj_present": True,
    }


def compute_metrics(tel: dict, patch: dict, traj: dict) -> dict:
    """Compute the three sub-metrics from parsed telemetry + patch + trajectory."""
    # ── localization_followed ──────────────────────────────────────────────
    rec_files = tel["recommended_files"]
    edited_files = patch["edited_files"]

    if not rec_files:
        loc = None  # N/A — no GT recommendation was delivered
        loc_reason = "no GT recommendation delivered"
        loc_hits: list[str] = []
    elif not edited_files:
        loc = False
        loc_reason = "no patch produced"
        loc_hits = []
    else:
        hits = []
        for rf in rec_files:
            for ef in edited_files:
                if _suffix_match(rf, ef):
                    hits.append(rf)
                    break
        loc = bool(hits)
        loc_reason = f"{len(hits)}/{len(rec_files)} recommended files matched in patch"
        loc_hits = hits

    # ── contract_followed ──────────────────────────────────────────────────
    # Heuristic: if GT delivered contract hints AND the patch modifies a
    # function signature line (`def foo(...)`) or removes a type annotation,
    # flag as possibly violated.
    contract_hints = tel["contract_hints"]
    if not contract_hints:
        contract = None
        contract_reason = "no contract hints delivered"
    else:
        # Conservative: check if patch rewrites a def signature or a return-
        # annotation line. We can't perfectly check semantic preservation
        # without running the tests.
        diff = patch["diff_text"]
        signature_touched = bool(re.search(
            r"^-\s*def\s+\w+\s*\(", diff, flags=re.MULTILINE
        ))
        # A `-` line removing a def-line means the signature was rewritten.
        return_annot_removed = bool(re.search(
            r"^-.*->\s*\S+:\s*$", diff, flags=re.MULTILINE
        ))
        if signature_touched or return_annot_removed:
            contract = False
            contract_reason = (
                "signature or return-annotation line removed by patch"
            )
        else:
            contract = True
            contract_reason = (
                "no signature/return-annotation removal detected"
            )

    # ── verification_followed ─────────────────────────────────────────────
    ver_targets = tel["verification_targets"]
    if not ver_targets and not tel["recommended_files"]:
        verification = None
        verification_reason = "no verification cues delivered"
    else:
        # Ran any pytest? Any opened file that suffix-matches a rec'd file?
        ran_pytest = traj["ran_any_pytest"]
        opened = traj["opened_files"]
        overlap = []
        for rt in ver_targets or tel["recommended_files"]:
            for op in opened:
                if _suffix_match(rt, op):
                    overlap.append(rt)
                    break
        if ran_pytest or overlap:
            verification = True
            verification_reason = (
                f"ran_pytest={ran_pytest}, file-overlap={len(overlap)}"
            )
        else:
            verification = False
            verification_reason = "no pytest + no rec'd-file open"

    return {
        "localization_followed": loc,
        "localization_reason": loc_reason,
        "localization_hits": loc_hits,
        "contract_followed": contract,
        "contract_reason": contract_reason,
        "verification_followed": verification,
        "verification_reason": verification_reason,
    }


def analyze_task(task_dir: Path) -> dict:
    """Analyze one task directory."""
    task_id = task_dir.name
    tel_path = task_dir / "gt_hook_telemetry.jsonl"
    # patch and traj live inside <task_id>/<task_id>/ subdir
    inner = task_dir / task_id
    patch_path = inner / f"{task_id}.patch"
    traj_path = inner / f"{task_id}.traj"

    tel = parse_telemetry(tel_path)
    patch = parse_patch(patch_path)
    traj = parse_trajectory(traj_path)
    metrics = compute_metrics(tel, patch, traj)

    # Discrepancy: did the agent follow localization but ack_followed = 0?
    discrepancy = (
        metrics["localization_followed"] is True
        and tel["ack_followed_raw"] == 0
    )

    return {
        "task_id": task_id,
        "recommended_files": tel["recommended_files"],
        "edited_files": patch["edited_files"],
        "contract_hints_count": len(tel["contract_hints"]),
        "verification_targets": tel["verification_targets"],
        "patch_present": patch["patch_present"],
        "traj_steps": traj["total_steps"],
        "steer_delivered_raw": tel["steer_delivered_raw"],
        "ack_armed_raw": tel["ack_armed_raw"],
        "ack_followed_raw": tel["ack_followed_raw"],
        "ack_not_observed_raw": tel["ack_not_observed_raw"],
        "ack_ignored_raw": tel["ack_ignored_raw"],
        **metrics,
        "discrepancy": discrepancy,
    }


def analyze_archive(archive: Path) -> dict:
    """Analyze all tasks under an archive directory."""
    tasks: list[dict] = []
    for item in sorted(archive.iterdir()):
        if item.is_dir() and item.name.startswith(("astropy__", "django__", "sympy__")):
            tasks.append(analyze_task(item))

    # Aggregate
    def _rate(key: str) -> tuple[int, int, float | None]:
        vals = [t[key] for t in tasks if t[key] is not None]
        if not vals:
            return (0, 0, None)
        hits = sum(1 for v in vals if v)
        return (hits, len(vals), hits / len(vals))

    loc_hits, loc_total, loc_rate = _rate("localization_followed")
    con_hits, con_total, con_rate = _rate("contract_followed")
    ver_hits, ver_total, ver_rate = _rate("verification_followed")

    discrepancies = [t["task_id"] for t in tasks if t["discrepancy"]]

    # Load arm summary for cross-check
    try:
        summary = json.loads((archive / "gt_arm_summary.json").read_text())
        summary_ack_followed_rate = summary.get("ack_followed_rate", 0.0)
        summary_has_patch_rate = summary.get("has_patch_rate", 0.0)
        arm = summary.get("arm", "?")
    except Exception:
        summary_ack_followed_rate = 0.0
        summary_has_patch_rate = 0.0
        arm = "?"

    return {
        "archive": str(archive),
        "arm": arm,
        "task_count": len(tasks),
        "tasks": tasks,
        "summary_has_patch_rate": summary_has_patch_rate,
        "summary_ack_followed_rate": summary_ack_followed_rate,
        "localization_followed": {
            "hits": loc_hits, "total": loc_total, "rate": loc_rate,
        },
        "contract_followed": {
            "hits": con_hits, "total": con_total, "rate": con_rate,
        },
        "verification_followed": {
            "hits": ver_hits, "total": ver_total, "rate": ver_rate,
        },
        "discrepancies": discrepancies,
        "discrepancy_count": len(discrepancies),
    }


def render_markdown(result: dict) -> str:
    """Render analysis as markdown."""
    lines = []
    lines.append(f"### {os.path.basename(result['archive'])}")
    lines.append("")
    lines.append(f"- **Arm:** `{result['arm']}`  |  **Tasks:** {result['task_count']}")
    lines.append(f"- `has_patch_rate (summary)`: {result['summary_has_patch_rate']:.2f}")
    lines.append(f"- `ack_followed_rate (summary, broken)`: {result['summary_ack_followed_rate']:.2f}")
    lines.append("")
    lines.append("**Sub-metrics:**")
    lines.append("")
    lines.append("| metric | hits / denom | rate |")
    lines.append("|---|:-:|:-:|")
    for label, key in [
        ("localization_followed", "localization_followed"),
        ("contract_followed", "contract_followed"),
        ("verification_followed", "verification_followed"),
    ]:
        d = result[key]
        rate_str = f"{d['rate']:.2f}" if d["rate"] is not None else "N/A"
        lines.append(f"| `{label}` | {d['hits']} / {d['total']} | {rate_str} |")
    lines.append("")
    lines.append(f"**Discrepancies (real follow-through missed by ack_followed):** "
                 f"{result['discrepancy_count']} — {result['discrepancies']}")
    lines.append("")
    lines.append("**Per-task:**")
    lines.append("")
    lines.append("| task | rec'd files | edited files | loc | con | ver | ack_raw | discrepancy |")
    lines.append("|---|---|---|:-:|:-:|:-:|:-:|:-:|")
    for t in result["tasks"]:
        rf = ", ".join(os.path.basename(x) for x in t["recommended_files"][:3]) or "—"
        ef = ", ".join(os.path.basename(x) for x in t["edited_files"][:3]) or "—"
        loc = {True: "YES", False: "no", None: "--"}[t["localization_followed"]]
        con = {True: "YES", False: "no", None: "--"}[t["contract_followed"]]
        ver = {True: "YES", False: "no", None: "--"}[t["verification_followed"]]
        disc = "**BROKEN**" if t["discrepancy"] else ""
        lines.append(f"| {t['task_id']} | {rf} | {ef} | {loc} | {con} | {ver} "
                     f"| {t['ack_followed_raw']} | {disc} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("archives", nargs="+", help="Archive directories")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    args = ap.parse_args()

    results = []
    for a in args.archives:
        p = Path(a).resolve()
        if not p.is_dir():
            print(f"SKIP (not a directory): {p}", file=sys.stderr)
            continue
        results.append(analyze_archive(p))

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0

    for r in results:
        print(render_markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
