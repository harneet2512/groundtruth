import json, glob, os
os.chdir("/home/Lenovo")
for f in sorted(glob.glob("*.ablation_*.json")):
    d = json.load(open(f))
    arm = f.split(".")[0]
    ids = sorted(d.get("resolved_ids", []))
    short = [i.split("-")[-1] for i in ids]
    print(f"{arm}: {len(ids)}/10 resolved = {short}")
