#!/usr/bin/env python3
"""check_brief_delivery.py — prove the L1 GT brief was DELIVERED to the agent.

The only acceptable delivery proof: the AGENT-FACING first-turn instruction inside
a real output.jsonl contains the expected non-empty GT content. This script does
NOT look at gt_brief_full, producer logs, or any non-agent-facing field — only the
instruction the agent actually received.

Asserts on that instruction:
  - exactly one <gt-task-brief> open tag and one </gt-task-brief> close tag
  - if <gt-graph-map> is present, its body (whitespace-stripped) length > 0
  - --require-graph-map : <gt-graph-map> must be present AND non-empty
  - no hidden [GT_*] diagnostic leakage ([GT_META]/[GT_BRIEF_DIAG]/[GT_RANK_DIAG]/...)
  - --require-contract-line : a 'Contract:' line must be present

JSONL-parsed, never grep. Exit 0 on PASS, nonzero on FAIL.

Usage:
  python scripts/verify/check_brief_delivery.py <output.jsonl> [--json]
      [--require-graph-map] [--require-contract-line] [--allow-empty-graph-map]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Hidden diagnostics that must be filtered to stderr and NEVER reach the agent.
# (Mirrors oh_gt_full_wrapper _HIDDEN_PREFIXES + the brief-runner diag prints.)
HIDDEN_DIAG_MARKERS = [
    "[GT_META]", "[GT_STATUS]", "[GT_CONFIG]", "[GT_TRACE]", "[GT_DELIVERY]",
    "[GT_COST]", "[GT_PAYLOAD]", "[GT_LLM_CONFIG]", "[GT_SUMMARY]",
    "[GT_BRIEF_DIAG]", "[GT_RANK_DIAG]", "[GT_BRIEF_FAILED]", "[GT_BRIEF_TRACEBACK]",
]


def extract_first_turn_instruction(path: Path) -> str:
    """Return the agent-facing first-turn instruction (the brief-bearing user input).

    Looks ONLY at the top-level `instruction` field and history `content`/`message`
    — never `gt_brief`/`gt_brief_full` (the logging copies). Returns "" if none found.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            instr = rec.get("instruction")
            if isinstance(instr, str) and "<gt-task-brief>" in instr:
                return instr
            hist = rec.get("history")
            if isinstance(hist, list):
                for e in hist:
                    if isinstance(e, dict):
                        c = e.get("content") or e.get("message") or ""
                        if isinstance(c, str) and "<gt-task-brief>" in c:
                            return c
    return ""


def _graph_map_body(instr: str) -> tuple[bool, int]:
    m = re.search(r"<gt-graph-map>(.*?)</gt-graph-map>", instr, re.S)
    if not m:
        return (False, 0)
    return (True, len(m.group(1).strip()))


def check_brief_delivery(
    path: str,
    *,
    require_graph_map: bool = False,
    require_contract_line: bool = False,
    allow_empty_graph_map: bool = False,
) -> dict:
    p = Path(path)
    result: dict = {
        "check": "check_brief_delivery",
        "path": str(p),
        "passed": False,
        "instruction_len": 0,
        "task_brief_open": 0,
        "task_brief_close": 0,
        "graph_map_present": False,
        "graph_map_body_len": 0,
        "leak_found": False,
        "leaked_markers": [],
        "reasons": [],
    }
    if not p.exists():
        result["reasons"].append(f"file not found: {p}")
        return result

    instr = extract_first_turn_instruction(p)
    if not instr:
        result["reasons"].append("no agent-facing instruction containing <gt-task-brief> found")
        return result

    result["instruction_len"] = len(instr)
    result["task_brief_open"] = instr.count("<gt-task-brief>")
    result["task_brief_close"] = instr.count("</gt-task-brief>")
    present, body_len = _graph_map_body(instr)
    result["graph_map_present"] = present
    result["graph_map_body_len"] = body_len
    leaked = sorted({m for m in HIDDEN_DIAG_MARKERS if m in instr})
    result["leak_found"] = bool(leaked)
    result["leaked_markers"] = leaked

    reasons = result["reasons"]
    if result["task_brief_open"] != 1:
        reasons.append(f"expected exactly 1 <gt-task-brief> open tag, found {result['task_brief_open']}")
    if result["task_brief_close"] != 1:
        reasons.append(f"expected exactly 1 </gt-task-brief> close tag, found {result['task_brief_close']}")
    if present and body_len == 0 and not allow_empty_graph_map:
        reasons.append("<gt-graph-map> present but body is EMPTY (delivery shred)")
    if require_graph_map and (not present or body_len == 0):
        reasons.append("--require-graph-map: <gt-graph-map> missing or empty")
    if leaked:
        reasons.append(f"hidden diagnostic leakage in agent instruction: {leaked}")
    if require_contract_line and "Contract:" not in instr:
        reasons.append("--require-contract-line: no 'Contract:' line in the brief")

    result["passed"] = not reasons
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Prove L1 GT brief delivery in a real output.jsonl")
    ap.add_argument("path", help="path to output.jsonl")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--require-graph-map", action="store_true")
    ap.add_argument("--require-contract-line", action="store_true")
    ap.add_argument("--allow-empty-graph-map", action="store_true", default=False)
    args = ap.parse_args()

    r = check_brief_delivery(
        args.path,
        require_graph_map=args.require_graph_map,
        require_contract_line=args.require_contract_line,
        allow_empty_graph_map=args.allow_empty_graph_map,
    )
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        verdict = "PASS" if r["passed"] else "FAIL"
        print(f"[{verdict}] {r['path']}")
        print(f"  instruction_len={r['instruction_len']} "
              f"task_brief_open={r['task_brief_open']} task_brief_close={r['task_brief_close']}")
        print(f"  graph_map_present={r['graph_map_present']} graph_map_body_len={r['graph_map_body_len']}")
        print(f"  leak_found={r['leak_found']} leaked_markers={r['leaked_markers']}")
        for reason in r["reasons"]:
            print(f"  - {reason}")
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
