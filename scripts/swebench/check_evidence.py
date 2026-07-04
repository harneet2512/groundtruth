#!/usr/bin/env python3
"""Check evidence quality across all tasks in a run."""
import json, sys, os

logdir = sys.argv[1]
total = 0
fired = 0
gate_passed = 0

for f in sorted(os.listdir(logdir)):
    if not f.endswith(".evidence.jsonl"):
        continue
    name = f.replace(".evidence.jsonl", "")[:60]
    entries = [json.loads(l) for l in open(os.path.join(logdir, f)) if l.strip()]
    total += 1
    task_fired = False
    for e in entries:
        adm = e.get("v13_admissibility", {})
        sel = e.get("selected", [])
        cands = e.get("candidates", [])
        fams = e.get("post_edit_families_shown", [])
        gate = adm.get("output_gate_passed", False)
        nm = adm.get("name_match_in_output", "N/A")
        imp = adm.get("edges_import", 0)
        sf = adm.get("edges_same_file", 0)
        nmr = adm.get("edges_name_match_rejected", 0)

        if sel:
            task_fired = True
        if gate:
            gate_passed += 1

        print(f"  {name}")
        print(f"    candidates={len(cands)} selected={len(sel)} gate_passed={gate} name_match_in_output={nm}")
        print(f"    families: {', '.join(fams) if fams else 'none'}")
        print(f"    edges: import={imp} same_file={sf} name_match_rejected={nmr}")
        for s in sel:
            print(f"      [{s['family']}] {s['name']} score={s['score']}: {s.get('summary','')[:50]}")

    if task_fired:
        fired += 1

print(f"\n=== SUMMARY ===")
print(f"Tasks with evidence logs: {total}")
print(f"Tasks where GT output shown: {fired}/{total}")
print(f"Evidence events with gate passed: {gate_passed}")
print(f"PASS: {'YES' if fired == total else 'NO — GT did not fire on all tasks'}")
