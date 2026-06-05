#!/usr/bin/env python3
"""check_gold_in_brief.py — BUG-1/BUG-3 verifier: is GOLD the brief's PRIMARY edit-target?

Reads the FIRST agent-facing instruction from output.jsonl, extracts ONLY the
`<gt-task-brief>...</gt-task-brief>` block (NOT the surrounding issue text — that is where the
v1 false-positive came from: it matched the gold basename in the issue body / scope-chain), and
classifies where gold lands:

  - gold_is_primary_target : gold is the "1. <path>" rendered candidate (the file the brief
    scaffolds with EDIT-TARGET CONTRACTS). This is the thing that drives the agent.
  - gold_in_rendered_list  : gold appears in ANY "N. <path>" numbered candidate line.
  - gold_only_in_scopechain: gold appears ONLY in the "Scope chain (... ? ... ? ...)" graph
    enumeration — present but NOT surfaced as a target (the weasyprint misdirection).
  - gold_absent            : gold not in the brief block at all (the matplotlib misdirection).

PASS criterion (--require-primary, default) = gold_is_primary_target on every mapped task.
Without the flag, PASS = gold_in_rendered_list (weaker). Gold-aware ONLY as a known-failure
harness — never used in product logic.

Usage:
  check_gold_in_brief.py --artifacts DIR --map "kozea__weasyprint-2300=block.py,matplotlib__matplotlib-28933=lines.py"
  check_gold_in_brief.py --output task-x/output.jsonl --gold lines.py [--require-primary]
"""
from __future__ import annotations
import argparse, json, os, re, sys, glob

_BRIEF_RE = re.compile(r"<gt-task-brief>(.*?)</gt-task-brief>", re.DOTALL)
_NUM_RE = re.compile(r"^\s*(\d+)\.\s+(\S+)")          # "1. weasyprint/css/.../properties.py (..."
_SCOPE_RE = re.compile(r"scope chain", re.IGNORECASE)


def _first_brief_block(output_jsonl: str) -> str:
    """The <gt-task-brief> block from the first agent instruction that carries one."""
    with open(output_jsonl, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            hist = rec.get("history") if isinstance(rec, dict) else None
            cands = []
            if isinstance(hist, list):
                for h in hist:
                    if not isinstance(h, dict):
                        continue
                    c = h.get("message") or h.get("content") or ""
                    if isinstance(c, list):
                        c = " ".join(d.get("text", "") for d in c if isinstance(d, dict))
                    if isinstance(c, str):
                        cands.append(c)
            for k in ("message", "content", "instruction"):
                v = rec.get(k) if isinstance(rec, dict) else None
                if isinstance(v, str):
                    cands.append(v)
            for c in cands:
                m = _BRIEF_RE.search(c)
                if m:
                    return m.group(1).strip()
    return ""


def _rendered_candidates(brief: str) -> list[tuple[int, str]]:
    """The numbered 'N. <path>' candidate lines (excludes the scope-chain line)."""
    out = []
    for ln in brief.splitlines():
        if _SCOPE_RE.search(ln):
            continue
        m = _NUM_RE.match(ln)
        if m:
            out.append((int(m.group(1)), m.group(2)))
    return out


def check_one(output_jsonl: str, gold: str) -> dict:
    brief = _first_brief_block(output_jsonl)
    if not brief:
        return {"ok": False, "reason": "no_gt_task_brief_block", "gold": gold}
    cands = _rendered_candidates(brief)
    primary = cands[0][1] if cands else ""
    gold_is_primary = bool(primary) and gold in primary
    gold_in_list = any(gold in p for _, p in cands)
    gold_anywhere = gold in brief
    gold_only_scopechain = gold_anywhere and not gold_in_list
    return {
        "gold": gold,
        "gold_is_primary_target": gold_is_primary,
        "gold_in_rendered_list": gold_in_list,
        "gold_only_in_scopechain_or_prose": gold_only_scopechain,
        "gold_absent_from_brief": not gold_anywhere,
        "primary_target": primary,
        "rendered_candidates": [p for _, p in cands][:8],
        "brief_chars": len(brief),
    }


def _find_output(artifacts: str, task_id: str) -> str | None:
    for pat in (
        os.path.join(artifacts, f"*{task_id}*", "**", "output.jsonl"),
        os.path.join(artifacts, f"*{task_id}*", "output.jsonl"),
        os.path.join(artifacts, "**", f"*{task_id}*", "**", "output.jsonl"),
    ):
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts")
    ap.add_argument("--map", help="task_id=goldbasename,...")
    ap.add_argument("--output")
    ap.add_argument("--gold")
    ap.add_argument("--require-primary", action="store_true", default=True,
                    help="PASS only if gold is the PRIMARY edit-target (default on)")
    ap.add_argument("--allow-in-list", dest="require_primary", action="store_false",
                    help="weaker: PASS if gold is anywhere in the rendered candidate list")
    args = ap.parse_args()

    results = []
    if args.output and args.gold:
        results.append(("(single)", check_one(args.output, args.gold)))
    elif args.artifacts and args.map:
        for pair in args.map.split(","):
            tid, _, gold = pair.partition("=")
            tid, gold = tid.strip(), gold.strip()
            out = _find_output(args.artifacts, tid)
            results.append((tid, check_one(out, gold) if out else
                            {"ok": False, "reason": "no_output_jsonl", "gold": gold}))
    else:
        ap.error("need --output+--gold OR --artifacts+--map")

    all_ok = True
    for tid, r in results:
        ok = r.get("gold_is_primary_target") if args.require_primary else r.get("gold_in_rendered_list")
        r["ok"] = bool(ok)
        all_ok = all_ok and bool(ok)
        print(f"\n=== {tid} ===")
        print(json.dumps(r, indent=2))
    crit = "PRIMARY edit-target" if args.require_primary else "in rendered list"
    print(f"\n{'PASS' if all_ok else 'FAIL'} — gold must be {crit} in the <gt-task-brief>")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
