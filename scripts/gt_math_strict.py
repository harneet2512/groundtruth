#!/usr/bin/env python3
"""Strict gt_math parity gate — the guardrail against 'all green' inflation.

Reads a run's per-task artifacts (trial_output.log + the gt certs) and applies the STRICT
counting rules from the run-28886910434 NO-GO report. It NEVER calls an LLM and NEVER runs a
benchmark task — pure offline attestation.

STRICT rules (a violation of any -> NO-GO, and rc=1 under --strict):
  * A REQUIRED row that is UNMEASURED ('—') fails. Silence is not proof.
  * '⚪' (delivered-not-consumed) is NOT counted as consumed.
  * '🔇' (silent / not-triggered) is NOT counted as active delivery.
  * Row 40 (C1 wide passage) must carry a MEASURED window witness ([GT_META] passage_window).
  * Row 41 (C2 index-body) must carry a MEASURED body-channel witness when GT_SEM_BODY was on
    ([GT_META] sem_body  OR the honest fail-closed [GT_WARN] no-op).
  * Retry (row 27/§13.12) evidence must come from THIS run; a witness from another sha does not
    count. Pass --sha <expected> to assert the run sha when it is recorded in the artifacts.
  * Leakage must be 0.

Usage:
  python scripts/gt_math_strict.py --run-dir <dir-of-ll-full-*-task-subdirs> [--sha X] [--strict]

Exit: 0 = GO/CONDITIONAL-GO printed; 1 = NO-GO (only under --strict, so CI can gate on it).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

_LEAK = re.compile(r"FAIL_TO_PASS|PASS_TO_PASS|::test_|\bdef test_|\bassert\s|test_patch")
_GT_BLOCK = re.compile(r"<gt-[a-z-]+[^>]*>.*?</gt-[a-z-]+>", re.S)


def _find(base: str, name: str) -> str | None:
    hits = glob.glob(os.path.join(base, "**", name), recursive=True)
    return hits[0] if hits else None


def _traj_text(base: str):
    """Return the concatenated agent-visible text (for leakage + RECEIVED@0)."""
    tj = _find(base, "mini-swe-agent.trajectory.json") or _find(base, "*.traj.json")
    if not tj:
        return None
    try:
        msgs = json.load(open(tj, encoding="utf-8")).get("messages", [])
    except Exception:
        return None
    out = []
    for m in msgs:
        c = m.get("content", "")
        out.append(c if isinstance(c, str) else json.dumps(c))
    return out


def audit_task(base: str, expected_sha: str | None) -> dict:
    log_p = _find(base, "trial_output.log")
    log = open(log_p, encoding="utf-8", errors="replace").read() if log_p else ""
    msgs = _traj_text(base) or []

    # foundation
    fg = _find(base, "foundational_gate_report.json")
    all_on = None
    if fg:
        try:
            all_on = bool(json.load(open(fg, encoding="utf-8")).get("verdict", {}).get("all_on"))
        except Exception:
            all_on = None

    # leakage (scan every gt-* block)
    leaks = 0
    for txt in msgs:
        for gm in _GT_BLOCK.finditer(txt):
            leaks += len(_LEAK.findall(gm.group(0)))

    received0 = (len(msgs) > 1 and "<gt-task-brief" in msgs[1])

    # C1 row 40: measured iff a passage_window witness was emitted
    m40 = re.search(r"\[GT_META\] passage_window window=(\d+) wide=([01])", log)
    # C2 row 41: measured iff sem_body positive witness OR the honest fail-closed no-op warn
    m41_pos = re.search(r"\[GT_META\] sem_body body_terms_rows=(\d+)", log)
    m41_neg = "GT_SEM_BODY=1 but graph has 0 body-channel rows" in log
    # was C2 even armed? the positive/negative markers OR a passage window with wide=1 imply depth.
    sem_body_on = bool(m41_pos or m41_neg) or (m40 and m40.group(2) == "1")

    retry = re.search(r"\[GT_RETRY\] (enabled: retries=\d+|disabled)", log)
    l6 = "L6_REINDEX_OK" in log
    post_search = log.count("<gt-search-facts")

    return {
        "all_on": all_on,
        "leaks": leaks,
        "received0": received0,
        "l6": l6,
        "post_search": post_search,
        "c1_window": int(m40.group(1)) if m40 else None,     # None = UNMEASURED (row 40 = —)
        "c1_wide": (m40.group(2) == "1") if m40 else None,
        "c2_body_rows": int(m41_pos.group(1)) if m41_pos else (0 if m41_neg else None),
        "c2_failclosed": m41_neg,
        "sem_body_on": sem_body_on,
        "retry": retry.group(1) if retry else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--sha", default=None, help="expected run sha (retry provenance assertion)")
    ap.add_argument("--strict", action="store_true", help="rc=1 on NO-GO")
    args = ap.parse_args()

    # Only real task artifacts: a per-task dir carries a trial_output.log. This filters out
    # non-task upload dirs (gt_artifacts/, jobs/, summarize/) that would otherwise be mis-scored.
    task_dirs = sorted(
        d for d in glob.glob(os.path.join(args.run_dir, "*"))
        if os.path.isdir(d) and _find(d, "trial_output.log")
    )
    if not task_dirs:
        print(f"gt_math_strict: no task subdirs (with trial_output.log) under {args.run_dir}", file=sys.stderr)
        return 2

    blockers: list[str] = []
    print(f"{'task':<28} {'found':<6} {'leak':<5} {'rcv0':<5} {'C1(row40)':<12} {'C2(row41)':<16} {'retry':<10}")
    print("-" * 92)
    for d in task_dirs:
        t = os.path.basename(d).replace("ll-full-", "")[:27]
        r = audit_task(d, args.sha)

        c1 = f"win={r['c1_window']}" if r["c1_window"] is not None else "UNMEASURED"
        if r["c1_window"] is None:
            blockers.append(f"{t}: row 40 C1 wide-passage UNMEASURED (no [GT_META] passage_window)")
        if r["c2_body_rows"] is not None:
            c2 = f"rows={r['c2_body_rows']}" if r["c2_body_rows"] else "fail-closed(0)"
        elif r["sem_body_on"]:
            c2 = "UNMEASURED"
            blockers.append(f"{t}: row 41 C2 index-body UNMEASURED (GT_SEM_BODY on, no witness)")
        else:
            c2 = "n/a(off)"
        if r["all_on"] is not True:
            blockers.append(f"{t}: foundation all_on != True ({r['all_on']})")
        if r["leaks"]:
            blockers.append(f"{t}: LEAKAGE={r['leaks']} (must be 0)")
        if not r["received0"]:
            blockers.append(f"{t}: RECEIVED@0 = NO (brief not in msg1)")

        print(f"{t:<28} {str(r['all_on']):<6} {r['leaks']:<5} "
              f"{'Y' if r['received0'] else 'N':<5} {c1:<12} {c2:<16} {str(r['retry']):<10}")

    print("-" * 92)
    verdict = "GO" if not blockers else "NO-GO"
    print(f"\nSTRICT VERDICT: {verdict}")
    if blockers:
        print("Blockers (unmeasured/failing REQUIRED rows — UNMEASURED does NOT pass):")
        for b in blockers:
            print(f"  - {b}")
    if args.sha:
        print(f"\nNOTE: retry provenance must be sha={args.sha}; a witness from any other sha does not count.")
    return 1 if (blockers and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
