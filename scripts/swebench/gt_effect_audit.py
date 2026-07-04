#!/usr/bin/env python3
"""Stage-2 EFFECT screen — did GT plausibly CAUSE the outcome, not just emit?

The 3-month trap: every lever was scored on EMIT ("the layer fired"), never EFFECT
("GT changed the agent's decision"). gt_oracle events, layer counts, delivery_rate are
all emit. This screens each task on the §4 CAUSATION signals from the agent's actual
trajectory (output.jsonl) + the gold patch + the frozen baseline:

  resolved            official outcome (test_result)
  gt_localized_gold   a <gt-localization> candidate names a gold file
  agent_hit_gold      the agent's final patch edits a gold file
  self_localizable    the issue text names the gold file/symbol (fair-probe FAIL = GT can't claim cause)
  leakage             FAIL_TO_PASS / test names in a GT surface (must be 0)

Verdict (correct-or-quiet — never claims a win it can't support):
  NO            unresolved
  UNPROVEN      resolved but self-localizable OR GT did not localize gold  -> luck/self-loc, not a GT win
  NEEDS_MANUAL  resolved + GT localized gold + agent hit gold + not-self-localizable -> candidate; read it chronologically
  LEAK          any leakage>0 -> disqualified regardless of outcome

This is a SCREEN, not the full §4. It collapses the bulk (clearly-not-a-win) and
flags the handful that warrant a real chronological read. It does NOT assert "consumed
+ reasoned through that context" — that still needs the human/agent chronological pass.

usage: gt_effect_audit.py <artifacts_dir> [--dataset PATH] [--baseline PATH] [--only ids.csv]
"""
from __future__ import annotations
import json, glob, os, re, sys, argparse

def _gold_files(dataset: str) -> dict[str, set]:
    out = {}
    for l in open(dataset, encoding="utf-8"):
        if not l.strip(): continue
        d = json.loads(l); p = d.get("patch", "") or ""
        gf = set(re.findall(r"^\+\+\+ b/(.+)$", p, re.M))
        # gold "real-fix" files: drop pure changelog/notes/docs noise from the causal set
        real = {f.strip() for f in gf if not re.search(r"(CHANGES|changelog|releasenotes|/notes/|\.rst$|\.md$|whatsnew)", f, re.I)}
        out[d["instance_id"]] = real or {f.strip() for f in gf}
    return out

def _gold_symbols(dataset: str) -> dict[str, set]:
    out = {}
    for l in open(dataset, encoding="utf-8"):
        if not l.strip(): continue
        d = json.loads(l); p = d.get("patch", "") or ""
        syms = set(re.findall(r"^[-+].*\bdef\s+([a-zA-Z_]\w+)", p, re.M))
        syms |= set(re.findall(r"@@.*\bdef\s+([a-zA-Z_]\w+)", p))
        out[d["instance_id"]] = {s for s in syms if len(s) >= 4 and not s.startswith("__")}
    return out

def _find_oj(art: str, tid: str):
    g = glob.glob(os.path.join(art, f"task-{tid}", "**", "output.jsonl"), recursive=True)
    return g[0] if g else None

def _bn(f): return os.path.basename(f.replace("\\", "/"))

def audit_task(oj: str, gold: set, gsyms: set, issue_cache: dict):
    rec = None
    for line in open(oj, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line); break
        except Exception:
            continue
    if rec is None:
        return None  # empty/malformed trajectory -> skip (correct-or-quiet)
    h = rec.get("history", []) or []
    issue = rec.get("problem_statement", "") or ""
    # GT localization candidate files (only inside <gt-localization> blocks)
    gt_loc = set()
    leak = 0
    for e in h:
        msg = str(e.get("message", "") or e.get("content", "") or "")
        for m in re.finditer(r"<gt-localization[^>]*>(.*?)</gt-localization>", msg, re.S):
            for fp in re.findall(r"([A-Za-z0-9_./-]+\.(?:py|go|js|ts|rs|java))", m.group(1)):
                gt_loc.add(_bn(fp))
        # leakage: FAIL_TO_PASS / explicit test fn inside a GT surface
        for m in re.finditer(r"<gt-[a-z]+[^>]*>(.*?)</gt-", msg, re.S):
            if re.search(r"FAIL_TO_PASS|PASS_TO_PASS|::test_|def test_", m.group(1)):
                leak += 1
    # agent edited files (final patch)
    tr = rec.get("test_result", {}) or {}
    patch = tr.get("git_patch", "") or rec.get("git_patch", "") or ""
    edited = set(_bn(f) for f in re.findall(r"diff --git a/(\S+) b/", patch))
    # resolved
    resolved = False
    rep = tr.get("report", {}) if isinstance(tr, dict) else {}
    if isinstance(rep, dict):
        for v in rep.values():
            if isinstance(v, dict) and v.get("resolved"): resolved = True
    goldbn = {_bn(f) for f in gold}
    gt_localized_gold = bool(gt_loc & goldbn)
    agent_hit_gold = bool(edited & goldbn)
    # fair-probe: does the issue text NAME the gold file or a gold symbol?
    il = issue.lower()
    self_localizable = any(g.rsplit(".", 1)[0].lower() in il for g in goldbn) or any(s.lower() in il for s in gsyms)
    return dict(gt_loc=sorted(gt_loc)[:4], edited=sorted(edited)[:4], gold=sorted(goldbn),
                resolved=resolved, gt_localized_gold=gt_localized_gold, agent_hit_gold=agent_hit_gold,
                self_localizable=self_localizable, leakage=leak)

def verdict(a: dict) -> str:
    if a["leakage"] > 0: return "LEAK"
    if not a["resolved"]: return "NO"
    if not a["gt_localized_gold"]: return "UNPROVEN(gt-missed-gold)"
    if a["self_localizable"]: return "UNPROVEN(self-localizable)"
    if not a["agent_hit_gold"]: return "UNPROVEN(agent-missed-gold)"
    return "NEEDS_MANUAL(candidate)"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("art"); ap.add_argument("--dataset", default="benchmarks/data/swebench_live_lite.jsonl")
    ap.add_argument("--baseline", default=".claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    gold = _gold_files(args.dataset); gsym = _gold_symbols(args.dataset)
    base = set(json.load(open(args.baseline)).get("resolved_ids", [])) if os.path.exists(args.baseline) else set()
    only = set(x.strip() for x in args.only.split(",")) if args.only else None
    rows = []
    for d in sorted(glob.glob(os.path.join(args.art, "task-*"))):
        tid = os.path.basename(d)[5:]
        if only and tid not in only: continue
        oj = _find_oj(args.art, tid)
        if not oj: continue
        a = audit_task(oj, gold.get(tid, set()), gsym.get(tid, set()), {})
        if a is None: continue
        # OFFICIAL resolved verdict lives in eval_result.json (the grader), NOT output.jsonl
        ej = os.path.join(args.art, f"task-{tid}", "eval_result.json")
        if os.path.exists(ej):
            try: a["resolved"] = json.load(open(ej)).get("resolved_instances", 0) > 0
            except Exception: pass
        v = verdict(a)
        flip = a["resolved"] and tid not in base
        rows.append((tid, v, flip, a))
    # report
    from collections import Counter
    c = Counter(r[1] for r in rows)
    print(f"=== Stage-2 EFFECT screen: {len(rows)} tasks ===")
    for k, n in sorted(c.items()): print(f"  {k:28} {n}")
    cand = [r for r in rows if r[1].startswith("NEEDS_MANUAL")]
    print(f"\nCANDIDATE GT-wins (resolved + GT-localized-gold + hit-gold + not-self-localizable) = {len(cand)}:")
    for tid, v, flip, a in cand:
        print(f"  {tid:42} flip={flip} gt_loc={a['gt_loc']} gold={a['gold'][:2]}")
    print(f"\nflips screened UNPROVEN (raw flip but not GT-caused): "
          f"{sum(1 for r in rows if r[2] and r[1].startswith('UNPROVEN'))}")
    print(f"leakage>0: {sum(1 for r in rows if r[1]=='LEAK')}")

if __name__ == "__main__":
    main()
