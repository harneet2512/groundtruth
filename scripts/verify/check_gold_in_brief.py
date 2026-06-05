#!/usr/bin/env python3
"""check_gold_in_brief.py — BUG-1 verifier: did the GT brief surface the GOLD file?

BUG-1 was: the L1 brief rendered the witnessless grep-floor `loc.candidates` list and
dropped the v74 composite rank-2 gold (e.g. matplotlib lines.py). The fix routes the
v74 ranked_full files into the localization fallback. This script PROVES the fix from the
agent's perspective: it reads the FIRST agent-facing instruction (the brief the agent
actually saw) out of output.jsonl and asserts the gold file appears in it, reporting its
1-based rank within the localization file list and the confidence header tier.

It is gold-aware ONLY as a verification harness on KNOWN-failures (kozea/weasyprint-2300,
matplotlib-28933) — never used in product logic. Pure read of the delivered text.

Usage:
  check_gold_in_brief.py --artifacts DIR --map "kozea__weasyprint-2300=block.py,matplotlib__matplotlib-28933=lines.py"
  check_gold_in_brief.py --output task-x/output.jsonl --gold lines.py
Exit 0 if every mapped gold is present in its brief; 1 otherwise.
"""
from __future__ import annotations
import argparse, json, os, re, sys, glob


def _first_agent_instruction(output_jsonl: str) -> str:
    """The first user/instruction message text the agent received (carries the brief)."""
    with open(output_jsonl, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            # OH output.jsonl: a record with history[]; the first message action holds the task
            hist = rec.get("history") if isinstance(rec, dict) else None
            if isinstance(hist, list):
                for h in hist:
                    if not isinstance(h, dict):
                        continue
                    content = h.get("message") or h.get("content") or ""
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    if isinstance(content, str) and "<gt-task-brief>" in content:
                        return content
            # flat record fallback
            for k in ("message", "content", "instruction"):
                v = rec.get(k) if isinstance(rec, dict) else None
                if isinstance(v, str) and "<gt-task-brief>" in v:
                    return v
    return ""


def _localization_block(text: str) -> str:
    """Extract the localization/likely-files section from the brief text."""
    # the brief renders a header like 'Likely files' / 'Localization' followed by a list
    m = re.search(
        r"(localiz|likely file|candidate file|files? to|where to look)(.*?)(</gt-task-brief>|<gt-graph-map>|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return m.group(0) if m else text


def _gold_rank(loc_text: str, gold: str) -> int:
    """1-based rank of the first numbered list line mentioning gold; 0 if absent there."""
    rank = 0
    for ln in loc_text.splitlines():
        mnum = re.match(r"\s*(\d+)\.\s+(.*)", ln)
        if mnum:
            rank += 1
            if gold in mnum.group(2):
                return rank
    return 0


def _conf_tier(text: str) -> str:
    m = re.search(r"\b(HIGH|MEDIUM|MED|LOW)\b\s*confidence", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    for t in ("[VERIFIED]", "[WARNING]", "[INFO]"):
        if t in text:
            return t
    return "?"


def check_one(output_jsonl: str, gold: str) -> dict:
    instr = _first_agent_instruction(output_jsonl)
    if not instr:
        return {"ok": False, "reason": "no_brief_in_output", "gold": gold}
    loc = _localization_block(instr)
    in_loc = gold in loc
    in_brief = gold in instr
    rank = _gold_rank(loc, gold)
    return {
        "ok": bool(in_brief),
        "gold": gold,
        "in_localization_section": in_loc,
        "in_brief_anywhere": in_brief,
        "gold_rank_in_loc_list": rank,
        "confidence_tier": _conf_tier(loc),
        "brief_chars": len(instr),
    }


def _find_output(artifacts: str, task_id: str) -> str | None:
    for pat in (
        os.path.join(artifacts, f"*{task_id}*", "output.jsonl"),
        os.path.join(artifacts, f"task-{task_id}", "output.jsonl"),
        os.path.join(artifacts, "**", f"*{task_id}*output.jsonl"),
    ):
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts")
    ap.add_argument("--map", help="task_id=goldbasename,task_id=goldbasename")
    ap.add_argument("--output")
    ap.add_argument("--gold")
    args = ap.parse_args()

    results = []
    if args.output and args.gold:
        results.append(("(single)", check_one(args.output, args.gold)))
    elif args.artifacts and args.map:
        for pair in args.map.split(","):
            tid, _, gold = pair.partition("=")
            tid, gold = tid.strip(), gold.strip()
            out = _find_output(args.artifacts, tid)
            if not out:
                results.append((tid, {"ok": False, "reason": "no_output_jsonl", "gold": gold}))
                continue
            results.append((tid, check_one(out, gold)))
    else:
        ap.error("need --output+--gold OR --artifacts+--map")

    all_ok = True
    for tid, r in results:
        all_ok = all_ok and r.get("ok", False)
        print(f"\n=== {tid} ===")
        print(json.dumps(r, indent=2))
    print("\n" + ("ALL_GOLD_IN_BRIEF: PASS" if all_ok else "GOLD_MISSING: FAIL"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
