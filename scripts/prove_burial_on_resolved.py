#!/usr/bin/env python
"""READ-ONLY proof: did GT's brief surface gold on RESOLVED Live-Lite tasks?

Thesis (CLAUDE.md KEY RULE): resolved != GT win. If the resolved tasks succeeded
WITHOUT gold in the brief, success came from the agent self-localizing, not GT —
and the burial is real on the SHIPPED (LSP-resolved) briefs (cert-proven substrate).

No code changed. Reads: eval_result.json (verdict), the dataset patch (gold files),
and the `brief_candidates=[...]` line from full_run.log (what the brief surfaced).
"""
from __future__ import annotations
import ast, glob, json, os, re, sys

ART = "D:/gt_runs/300_e51cc3f0/art_clean"
DATASET = "D:/Groundtruth/benchmarks/data/swebench_live_lite.jsonl"

def gold_files(patch: str) -> list[str]:
    # gold = source files the gold patch edits (exclude test files added by test_patch)
    fs = re.findall(r'^\+\+\+ b/(.+)$', patch or "", re.M)
    return sorted({f.strip() for f in fs if f.strip() and f.strip() != "/dev/null"})

def brief_cands(logpath: str) -> list[str]:
    try:
        txt = open(logpath, encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    m = re.search(r'brief_candidates=(\[.*?\])', txt)
    if not m:
        return []
    try:
        raw = ast.literal_eval(m.group(1))
    except Exception:
        return []
    # dedup the task-prefix variant (aiogram__x-1/aiogram/.. -> aiogram/..), keep order
    seen, out = set(), []
    for c in raw:
        c2 = re.sub(r'^[^/]+__[^/]+-\d+/', '', c)
        if c2 not in seen:
            seen.add(c2); out.append(c2)
    return out

# 1. resolved task ids = UNION of resolved_ids across all per-task SWE-bench reports
res_set = set()
for ep in glob.glob(f"{ART}/task-*/eval_result.json"):
    try:
        d = json.load(open(ep, encoding="utf-8"))
    except Exception:
        continue
    res_set.update(d.get("resolved_ids", []) or [])
res_ids = sorted(res_set)
print(f"resolved tasks (union of resolved_ids): {len(res_ids)}")

# 2. gold map for resolved (stream the big dataset once)
want = set(res_ids)
gmap = {}
for line in open(DATASET, encoding="utf-8"):
    if not line.strip():
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    iid = d.get("instance_id")
    if iid in want:
        gmap[iid] = gold_files(d.get("patch", ""))

# 3. proof table: first 10 resolved with both gold + a brief
print(f"\n{'task':40} {'gold_in_brief?':14} {'gold_rank':10} gold / brief-size")
print("-" * 110)
shown = 0
hit = 0
for tid in res_ids:
    gold = gmap.get(tid, [])
    cands = brief_cands(f"{ART}/task-{tid}/gt_debug/full_run.log")
    if not gold or not cands:
        continue
    # gold in brief: a candidate whose path endswith a gold file (or basename match)
    rank = None
    for g in gold:
        gb = os.path.basename(g)
        for i, c in enumerate(cands):
            if c == g or c.endswith("/" + g) or os.path.basename(c) == gb:
                rank = i + 1 if rank is None else min(rank, i + 1)
                break
    in_brief = rank is not None
    hit += 1 if in_brief else 0
    print(f"{tid:40} {('YES' if in_brief else 'NO'):14} {str(rank if rank else '-'):10} {gold[:2]} / {len(cands)} cands")
    shown += 1
    if shown >= 10:
        break

print("-" * 110)
print(f"of {shown} resolved tasks shown: gold-in-brief = {hit}/{shown}, gold-MISSED = {shown-hit}/{shown}")
print("(gold-MISSED on a RESOLVED task => the agent self-localized; GT's brief did not surface gold)")

# === AGGREGATE over ALL resolved tasks that have both gold + a brief ===
n=0; missed=0; r1=0; r23=0; r4=0
for tid in res_ids:
    gold = gmap.get(tid, [])
    cands = brief_cands(f"{ART}/task-{tid}/gt_debug/full_run.log")
    if not gold or not cands:
        continue
    rank=None
    for g in gold:
        gb=os.path.basename(g)
        for i,c in enumerate(cands):
            if c==g or c.endswith("/"+g) or os.path.basename(c)==gb:
                rank=i+1 if rank is None else min(rank,i+1); break
    n+=1
    if rank is None: missed+=1
    elif rank==1: r1+=1
    elif rank<=3: r23+=1
    else: r4+=1
print("\n=== AGGREGATE over ALL resolved tasks (gold + brief present) ===")
print(f"  resolved tasks measured:        {n}")
print(f"  gold MISSED (not in brief):     {missed}  ({100*missed/n:.1f}%)  <- agent self-localized")
print(f"  gold in brief @rank 1:          {r1}  ({100*r1/n:.1f}%)")
print(f"  gold in brief @rank 2-3:        {r23}  ({100*r23/n:.1f}%)")
print(f"  gold in brief @rank 4+ (buried): {r4}  ({100*r4/n:.1f}%)")
print(f"  => gold NOT cleanly surfaced (missed OR buried 4+): {missed+r4}/{n} ({100*(missed+r4)/n:.1f}%)")
