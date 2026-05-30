#!/usr/bin/env python3
"""Detect zero-completion infra-death tasks eligible for a single clean retry.

Retry gate — ALL must hold (strict "never retry an attempt that STARTED"):
  1. The task produced ZERO llm completions (it never actually ran), AND
  2. it either has no record in output.jsonl, OR its record is an infra death
     (docker/network error) with an empty patch.

A task with >=1 completion, or any non-empty patch, is NEVER eligible — that is
a completed attempt and retrying it would be a second roll (looks like cheating).
This recovers ONLY infrastructure deaths where the one allowed attempt never
happened, giving each task its single legitimate shot.

Side effects:
  - Writes an audit log (every task + verdict + evidence) so the retry decision
    is provable: anyone can confirm no STARTED task was retried.
  - Archives output.jsonl -> output.pre_retry.jsonl and rewrites output.jsonl
    with the eligible records REMOVED, so the OpenHands harness re-attempts them
    instead of treating the dead record as finished (it skips existing records).
  - Prints the comma-separated eligible instance IDs as the LAST stdout line
    (diagnostics go to stderr) for the workflow to consume.

Single retry is enforced structurally: this script runs once per shard.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys

# Same infra-death signatures the metrics classifier uses
# (scripts/swebench/compute_localization_metrics.py).
INFRA_ERROR_PATTERNS = (
    "UnixHTTPConnectionPool",
    "Read timed out",
    "BrokenPipeError",
    "ConnectionResetError",
    "docker.errors.APIError",
)


def _tid(rec: dict) -> str:
    return rec.get("instance_id") or (rec.get("instance") or {}).get("instance_id") or ""


def load_records(path: str) -> dict[str, dict]:
    recs: dict[str, dict] = {}
    if not os.path.exists(path):
        return recs
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = _tid(r)
            if tid:
                recs[tid] = r
    return recs


def err_text(rec: dict) -> str:
    e = rec.get("error") or rec.get("error_message") or ""
    if isinstance(e, dict):
        e = json.dumps(e)
    return str(e)


def is_infra(rec: dict) -> bool:
    return any(p in err_text(rec) for p in INFRA_ERROR_PATTERNS)


def has_patch(rec: dict) -> bool:
    p = (rec.get("test_result") or {}).get("git_patch") or rec.get("git_patch") or ""
    return bool((p or "").strip())


def completion_count(completions_root: str, tid: str) -> int:
    """Count llm completion files for an instance.

    Layout: <root>/.../CodeActAgent/<model>_maxiter_<N>/llm_completions/<tid>/*.json
    """
    n = 0
    for d in glob.glob(os.path.join(completions_root, "**", "llm_completions", tid), recursive=True):
        if os.path.isdir(d):
            n += sum(1 for fn in os.listdir(d) if fn.endswith(".json"))
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-jsonl", required=True)
    ap.add_argument("--completions-root", required=True)
    ap.add_argument("--task-ids", required=True, help="comma-separated instance IDs")
    ap.add_argument("--audit-log", required=True)
    args = ap.parse_args()

    task_ids = [t.strip() for t in args.task_ids.split(",") if t.strip()]
    recs = load_records(args.output_jsonl)

    eligible: list[str] = []
    audit: list[dict] = []
    for tid in task_ids:
        comps = completion_count(args.completions_root, tid)
        rec = recs.get(tid)
        infra = is_infra(rec) if rec else False
        patch = has_patch(rec) if rec else False

        if comps > 0:
            ok, reason = False, f"SKIP started (completions={comps})"
        elif patch:
            ok, reason = False, "SKIP produced a patch (completed attempt)"
        elif rec is None:
            ok, reason = True, "RETRY no record + 0 completions (died pre-inference)"
        elif infra:
            ok, reason = True, "RETRY infra death + 0 completions + empty patch"
        else:
            ok, reason = False, f"SKIP non-infra failure (err={err_text(rec)[:80]!r})"

        audit.append(
            {
                "instance_id": tid,
                "eligible": ok,
                "reason": reason,
                "completions": comps,
                "had_record": rec is not None,
                "infra": infra,
                "has_patch": patch,
            }
        )
        if ok:
            eligible.append(tid)

    os.makedirs(os.path.dirname(args.audit_log) or ".", exist_ok=True)
    with open(args.audit_log, "w", encoding="utf-8") as f:
        json.dump({"eligible": eligible, "audit": audit}, f, indent=2)

    for a in audit:
        print(f"[retry-gate] {a['instance_id']}: {a['reason']}", file=sys.stderr)

    # Remove eligible records so the harness re-attempts them (archive first).
    if eligible and os.path.exists(args.output_jsonl):
        shutil.copy(args.output_jsonl, args.output_jsonl.replace(".jsonl", ".pre_retry.jsonl"))
        kept: list[str] = []
        with open(args.output_jsonl, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    r = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if _tid(r) not in eligible:
                    kept.append(s)
        with open(args.output_jsonl, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + ("\n" if kept else ""))

    # LAST stdout line = the IDs the workflow re-runs (empty string if none).
    print(",".join(eligible))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
