#!/usr/bin/env python3
"""Task-agnostic layer audit for OH GT full wrapper output.jsonl.

Reads each JSONL record, stringifies the task payload, and checks for
structural evidence of layers L1–L6. Does not rely on instance IDs, gold
patches, or task-specific strings (beyond generic GT markers).

Usage:
  python oh_gt_full_layer_audit.py /path/to/output.jsonl
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Minimal fallback brief from wrapper (degraded path) — L1 not fully proven if alone.
_FALLBACK_BRIEF_MARKERS = (
    "GT graph built inside the task container",
    "GT graph built. Brief generation failed",
)

# L3: evidence family tags from post_edit (exclude L3B which is post_view-only).
_L3_EDIT_FAMILY = re.compile(
    r"\[GT_(CHANGE|CONTRACT|PATTERN|STRUCTURAL|SEMANTIC|CALLER|TEST|IMPORT)\]",
    re.IGNORECASE,
)

# L4: agent actually invoked an L4 tool with an argument (not template-only footer).
_L4_INVOCATION = re.compile(
    r"\bgt_query\s+\S+|\bgt_search\s+\S+\s+\S+|\bgt_navigate\s+\S+\s+\S+|\bgt_validate\s+\S+",
)


def _blob(record: dict) -> str:
    try:
        return json.dumps(record, default=str)
    except (TypeError, ValueError):
        return str(record)


def audit_record(record: dict) -> dict[str, bool]:
    """Return per-layer booleans for one JSONL object."""

    s = _blob(record)
    s_low = s.lower()

    brief_blob = ""
    if isinstance(record, dict):
        for key in ("instruction", "messages", "history", "trajectory"):
            part = record.get(key)
            if part is not None:
                brief_blob += json.dumps(part, default=str)
    brief_blob = brief_blob or s

    # L2: pretask tag (json.dumps escapes quotes inside strings; match flexibly).
    l2 = bool(re.search(r"<gt-pretask[^>]{0,500}L2", s))

    # L1: real pretask brief injected (not only generic fallback).
    has_task_brief = "<gt-task-brief>" in s
    looks_fallback_only = any(m in brief_blob for m in _FALLBACK_BRIEF_MARKERS)
    brief_substantive = (
        "candidate cluster" in s_low
        or "edit plan" in s_low
        or ("candidates" in s_low and "file" in s_low)
        or ("```" in brief_blob and len(brief_blob) > 800)
    )
    l1 = bool(
        has_task_brief
        and (not looks_fallback_only or brief_substantive or l2)
    )

    # L3 post-edit hook: edit trigger + at least one non-view evidence family tag.
    l3 = "post_edit:" in s and bool(_L3_EDIT_FAMILY.search(s))

    # L3b: post_view hook structural coupling markers (post_view.py).
    l3b = "post_view:" in s and "[GT_L3B]" in s

    # L4 tools: real shell invocations with arguments.
    l4 = bool(_L4_INVOCATION.search(s))

    # L5: finish advisory when material edits lack gt_validate coverage.
    l5 = bool(re.search(r"<gt-advisory[^>]{0,120}L5", s))

    # L6: incremental reindex after edits.
    l6 = "<gt-reindex" in s

    return {
        "L1_brief": l1,
        "L2_pretask_telemetry": l2,
        "L3_post_edit": l3,
        "L3b_post_view": l3b,
        "L4_tools": l4,
        "L5_advisory": l5,
        "L6_reindex": l6,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: oh_gt_full_layer_audit.py <output.jsonl>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 2

    layers = list(audit_record({}).keys())
    totals = {k: 0 for k in layers}
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        n += 1
        res = audit_record(rec)
        for k, v in res.items():
            if v:
                totals[k] += 1
        iid = rec.get("instance_id", rec.get("id", f"row_{n}"))
        flags = " ".join(f"{k}={'Y' if res[k] else 'N'}" for k in layers)
        print(f"{iid}\t{flags}")

    print("", file=sys.stderr)
    print(f"tasks={n}", file=sys.stderr)
    for k in layers:
        pct = 100.0 * totals[k] / n if n else 0.0
        print(f"{k}\t{totals[k]}/{n}\t{pct:.0f}%", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
