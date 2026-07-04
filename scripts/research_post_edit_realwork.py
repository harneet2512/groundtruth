#!/usr/bin/env python
"""READ-ONLY research: does the post-edit hook do REAL work on the 79 resolved tasks?

Truth source = output.jsonl (what the AGENT saw), not telemetry. Per CLAUDE.md:
fired != delivered != consumed. This measures DELIVERED (substantive vs empty) and
samples the actual content so substance is judged, not assumed. Consumption needs a
chronological read (done separately on a few).
"""
from __future__ import annotations
import glob, json, os, re, sys

ART = "D:/gt_runs/300_e51cc3f0/art_clean"
# post-edit families ONLY (exclude turn-0 brief: gt-localization, gt-scope)
POST = ["[CONTRACT", "[SIBLINGS]", "[GT_VERIFY]", "<gt-nudge", "spec.obligation",
        "semantic_drift", "obligation_resurface", "co-change", "[COCHANGE"]

res = set()
for ep in glob.glob(f"{ART}/task-*/eval_result.json"):
    try: res.update(json.load(open(ep, encoding="utf-8")).get("resolved_ids", []) or [])
    except Exception: pass

def oj_for(tid):
    g = glob.glob(f"{ART}/task-{tid}/**/output.jsonl", recursive=True)
    return g[0] if g else None

per_task = {}          # tid -> {fam: count}
contract_samples = []  # (tid, text) substantive judge
oblig_samples = []
verify_samples = []
tasks_with_postedit = 0

for tid in sorted(res):
    oj = oj_for(tid)
    if not oj: continue
    fam = {}
    for line in open(oj, encoding="utf-8", errors="replace"):
        # only count content the agent RECEIVED (observation/user/tool records)
        if '"role": "assistant"' in line:  # skip the agent's own text
            continue
        for t in POST:
            c = line.count(t)
            if c: fam[t] = fam.get(t, 0) + c
        # grab a real CONTRACT / obligation / verify payload to judge substance
        if "[CONTRACT" in line and len(contract_samples) < 6:
            m = re.search(r'\[CONTRACT[^\]]*\][^"]{0,400}', line)
            if m: contract_samples.append((tid, m.group(0)))
        if ("spec.obligation" in line or "obligation_resurface" in line) and len(oblig_samples) < 6:
            m = re.search(r'(obligation[^"]{0,300})', line)
            if m: oblig_samples.append((tid, m.group(0)))
        if "[GT_VERIFY]" in line and len(verify_samples) < 4:
            m = re.search(r'\[GT_VERIFY\][^"]{0,300}', line)
            if m: verify_samples.append((tid, m.group(0)))
    if fam:
        per_task[tid] = fam
        tasks_with_postedit += 1

# aggregate family totals
agg = {}
for fam in per_task.values():
    for k, v in fam.items(): agg[k] = agg.get(k, 0) + v

print(f"resolved tasks: {len(res)}  |  with output.jsonl found: {sum(1 for t in res if oj_for(t))}")
print(f"tasks with ANY post-edit delivery the agent saw: {tasks_with_postedit}")
print("\n=== post-edit family totals (occurrences across all agent-visible records) ===")
for k, v in sorted(agg.items(), key=lambda x: -x[1]):
    ntasks = sum(1 for f in per_task.values() if k in f)
    print(f"   {k:22} total={v:5}   in {ntasks:3} tasks")

print("\n=== SAMPLE [CONTRACT] payloads (substantive = real callers/args, not empty) ===")
for tid, s in contract_samples: print(f"  [{tid}] {s[:240]}")
print("\n=== SAMPLE obligation payloads ===")
for tid, s in oblig_samples: print(f"  [{tid}] {s[:240]}")
print("\n=== SAMPLE [GT_VERIFY] payloads ===")
for tid, s in verify_samples: print(f"  [{tid}] {s[:240]}")
