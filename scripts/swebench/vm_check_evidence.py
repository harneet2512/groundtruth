#!/usr/bin/env python3
"""Check if gt_evidence appeared in any D/14096 observation."""
import json, glob

for arm in ["A", "D"]:
    for d in sorted(glob.glob(f"/tmp/qwen_fc_ablation/run_*/runs/{arm}")):
        traj_file = f"{d}/astropy__astropy-14096/astropy__astropy-14096.traj"
        try:
            t = json.load(open(traj_file))
        except Exception:
            continue
        trajectory = t.get("trajectory", [])
        print(f"\n=== {arm}/14096 ({len(trajectory)} steps) ===")
        evidence_steps = 0
        for i, step in enumerate(trajectory):
            obs = step.get("observation", "") or ""
            # Check for ANY evidence-like content
            has_evidence = False
            for marker in ["gt_evidence", "gt-evidence", "sibling", "import edge",
                          "Repository pattern", "Verified import", "IMPORT",
                          "SIBLING", "pattern", "callers"]:
                if marker.lower() in obs.lower():
                    has_evidence = True
                    # Print a snippet
                    idx = obs.lower().index(marker.lower())
                    snippet = obs[max(0,idx-50):idx+100]
                    print(f"  Step {i}: found '{marker}' in observation")
                    print(f"    ...{snippet[:150]}...")
                    evidence_steps += 1
                    break
        if evidence_steps == 0:
            print(f"  NO evidence found in any observation")
        else:
            print(f"  Evidence found in {evidence_steps}/{len(trajectory)} steps")

        # Also check the state hook log path
        print(f"  Hook log: checking if state.json had gt_evidence...")
        # Check last few observations for template rendering
        for i in range(min(5, len(trajectory))):
            step = trajectory[i]
            obs = step.get("observation", "") or ""
            if len(obs) > 100:
                print(f"  Step {i} obs length: {len(obs)} chars, first 200: {obs[:200]}")
