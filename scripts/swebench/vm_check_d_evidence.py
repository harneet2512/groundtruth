#!/usr/bin/env python3
"""Check D trajectory for any evidence injection vs A."""
import json, glob, os

# Find D smoke trajectory
for d in sorted(glob.glob("/tmp/qwen_fc_ablation/preflight_1777331477/smoke_D/astropy*/*.traj")):
    t = json.load(open(d))
    traj = t.get("trajectory", [])
    print(f"D trajectory: {len(traj)} steps")

    # Check each observation for gt_evidence content
    for i, step in enumerate(traj):
        obs = step.get("observation", "") or ""
        # The template renders: {{observation}}\n{% if gt_evidence %}\n{{gt_evidence}}\n{% endif %}
        # So evidence would appear AFTER the observation text, separated by newlines
        # Check if obs has any non-standard content
        if len(obs) > 5000:
            print(f"  Step {i}: obs={len(obs)} chars (large)")

        # Check state dict if available
        state = step.get("state", {})
        if isinstance(state, dict) and "gt_evidence" in state:
            print(f"  Step {i}: gt_evidence in state! = {str(state['gt_evidence'])[:200]}")

    # Check info for any GT markers
    info = t.get("info", {})
    for k, v in info.items():
        if "gt" in str(k).lower() or "evidence" in str(k).lower():
            print(f"  info.{k} = {str(v)[:200]}")

    # Compare obs lengths with A
    print("\nComparing with A:")
    for a in sorted(glob.glob("/tmp/qwen_fc_ablation/preflight_1777331147/smoke_A/astropy*/*.traj")):
        ta = json.load(open(a))
        traja = ta.get("trajectory", [])
        print(f"  A trajectory: {len(traja)} steps")

        # Compare first few obs lengths
        for i in range(min(5, len(traj), len(traja))):
            obs_d = len(traj[i].get("observation", "") or "")
            obs_a = len(traja[i].get("observation", "") or "")
            diff = obs_d - obs_a
            marker = " <-- DIFFERENT" if abs(diff) > 100 else ""
            print(f"  Step {i}: A={obs_a}ch D={obs_d}ch delta={diff:+d}{marker}")
