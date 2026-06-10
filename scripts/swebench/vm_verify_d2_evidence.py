#!/usr/bin/env python3
"""Verify D2: micro/nudge stripped, only verification evidence (if any) passes."""
import json, glob

for d in sorted(glob.glob("/tmp/qwen_fc_ablation/preflight_1777332192/smoke_D/astropy*/*.traj")):
    t = json.load(open(d))
    traj = t.get("trajectory", [])
    print(f"D2 trajectory: {len(traj)} steps")

    leaked_micro = 0
    leaked_nudge = 0
    leaked_briefing = 0
    verification_evidence = 0
    any_gt_evidence = 0

    for i, step in enumerate(traj):
        state = step.get("state", {})
        if not isinstance(state, dict):
            continue
        ev = state.get("gt_evidence", "")
        if not ev:
            continue
        any_gt_evidence += 1

        if "[SHORTLIST]" in ev or "[NEXT]" in ev:
            leaked_micro += 1
            print(f"  LEAK step {i}: MICRO: {ev[:100]}")
        elif "[GT LIVE NUDGE]" in ev:
            leaked_nudge += 1
            print(f"  LEAK step {i}: NUDGE: {ev[:100]}")
        elif "[GT-ACK]" in ev or "gt_ack" in ev:
            leaked_micro += 1
            print(f"  LEAK step {i}: ACK: {ev[:100]}")
        elif "[SUBMIT NOW]" in ev:
            leaked_briefing += 1
            print(f"  LEAK step {i}: STEP-LIMIT: {ev[:100]}")
        elif "[VERIFIED]" in ev or "[WARNING]" in ev or "[CONTRACT]" in ev:
            verification_evidence += 1
            print(f"  PASS step {i}: VERIFICATION: {ev[:100]}")
        else:
            print(f"  UNKNOWN step {i}: {ev[:150]}")

    print(f"\nSummary:")
    print(f"  Total gt_evidence present: {any_gt_evidence}")
    print(f"  Leaked micro: {leaked_micro}")
    print(f"  Leaked nudge: {leaked_nudge}")
    print(f"  Leaked briefing/step-limit: {leaked_briefing}")
    print(f"  Verification evidence: {verification_evidence}")

    if leaked_micro == 0 and leaked_nudge == 0 and leaked_briefing == 0:
        print(f"\n  VERDICT: CLEAN — no ungated evidence leaked")
    else:
        print(f"\n  VERDICT: CONTAMINATED — {leaked_micro + leaked_nudge + leaked_briefing} leaks")
