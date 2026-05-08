#!/usr/bin/env python3
"""Gate checks for OpenHands + GT full-wrapper smoke output.jsonl.

Validates that L1–L6 signals are present with minimal content quality (not just tags).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PATH_IN_BRIEF = re.compile(r"[a-zA-Z0-9_./-]+/[a-zA-Z0-9_./-]+\.[a-zA-Z0-9]{1,4}\b")
EVIDENCE_TAG = re.compile(
    r"\[(?:GT_CHANGE|GT_CONTRACT|GT_CALLER|GT_SIBLING|GT_PATTERN|GT_STRUCTURAL|GT_SEMANTIC)\]"
)
L3B_MARK = re.compile(r"\[GT_L3B\]|gt_l3b", re.I)
L4_TOOL = re.compile(r"\bgt_(query|search|navigate|validate)\b")
REINDEX = re.compile(r"<gt-reindex", re.I)
BRIEF_BAD = re.compile(r"GT graph built inside|GT graph built\.", re.I)


def _load_records(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def check_instance(rec: dict) -> list[str]:
    errs: list[str] = []
    iid = rec.get("instance_id", "?")
    text_blob = json.dumps(rec, ensure_ascii=False)
    brief = str(rec.get("gt_brief") or "")
    merged_brief_blob = brief + "\n" + text_blob

    if BRIEF_BAD.search(merged_brief_blob) and "[GT_BRIEF_FAILED]" not in merged_brief_blob:
        errs.append(f"{iid}: L1 brief looks like placeholder (graph-built stub)")

    if not PATH_IN_BRIEF.search(merged_brief_blob) and "[GT_BRIEF_FAILED]" not in merged_brief_blob:
        errs.append(f"{iid}: L1 brief missing plausible repo path tokens")

    if "<gt-pretask" not in text_blob and "gt-pretask" not in text_blob.replace("\\", ""):
        errs.append(f"{iid}: L2 missing <gt-pretask")

    if not EVIDENCE_TAG.search(text_blob):
        errs.append(f"{iid}: L3 missing evidence family tag in trajectory")

    if not L3B_MARK.search(text_blob):
        errs.append(f"{iid}: L3b missing [GT_L3B]")

    if not L4_TOOL.search(text_blob):
        errs.append(f"{iid}: L4 no gt_query/gt_search/gt_navigate/gt_validate mention")

    if not REINDEX.search(text_blob):
        errs.append(f"{iid}: L6 missing <gt-reindex")

    tel = rec.get("gt_telemetry")
    if tel is not None and isinstance(tel, dict):
        if tel.get("overall_utilization", 1) == 0:
            errs.append(f"{iid}: telemetry overall_utilization is zero")

    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description="OH GT full smoke output gate")
    ap.add_argument("jsonl", type=Path, help="Path to output.jsonl")
    args = ap.parse_args()
    if not args.jsonl.is_file():
        print(f"FATAL: not a file: {args.jsonl}", file=sys.stderr)
        return 2

    all_errs: list[str] = []
    for rec in _load_records(args.jsonl):
        all_errs.extend(check_instance(rec))

    if all_errs:
        print("SMOKE_GATE_FAIL", len(all_errs), "issues", flush=True)
        for e in all_errs[:50]:
            print(" -", e, flush=True)
        return 1

    print("SMOKE_GATE_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
