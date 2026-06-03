"""Extract the REAL outcome of a DeepSWE pier run from the jobs/ dir.

Why this exists (learned from the GCP validation run, 2026-06-03):
- The job-level result.json (jobs/<ts>/result.json) is an AGGREGATE — it has no
  n_agent_steps / verifier_result. The per-trial result.json
  (jobs/<ts>/<task>__*/result.json) has them. `find jobs -name result.json | head`
  grabs the wrong one.
- result.json's `step_results` is EMPTY. The per-turn trajectory (commands, gt_hook
  calls, <gt-evidence>) lives in agent/mini-swe-agent.trajectory.json. The pass/fail
  outcome lives in verifier/test-stdout.txt.
- "Brief written to disk" is NOT proof the agent ran. The proof is n_agent_steps>0 +
  exit_status + reward. A run can write the brief and still do 0 useful work (the GHA
  284-empty-reprompt case) or crash before the agent (the cwd / key cases).

This surfaces, in the workflow log: did the AGENT actually run, did it submit, the
reward, the test pass/fail tally, the FAILING tests (the precise correctness gap),
and the GT hook firings from the real trajectory.

Usage: python3 scripts/verify/deepswe_outcome.py [jobs_dir]
"""
from __future__ import annotations
import glob
import json
import os
import re
import sys
from collections import Counter

jobs = sys.argv[1] if len(sys.argv) > 1 else "jobs"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

print("=== DeepSWE TRIAL OUTCOME (real proof, not brief-written) ===")

# exit_status + model_stats live in the agent trajectory.json info, NOT the trial
# result.json — pull them from there.
traj_info = {}
_traj = glob.glob(os.path.join(jobs, "*", "*__*", "agent", "mini-swe-agent.trajectory.json"))
if _traj:
    try:
        traj_info = (json.load(open(_traj[-1], encoding="utf-8")).get("info") or {})
    except Exception:
        traj_info = {}

trials = sorted(glob.glob(os.path.join(jobs, "*", "*__*", "result.json")))
if not trials:
    print("AGENT_RAN_STEPS=UNKNOWN  -- no trial result.json (harness broke before the agent?)")
else:
    d = json.load(open(trials[-1], encoding="utf-8"))
    info = d.get("info") or {}
    vr = d.get("verifier_result") or {}
    exc = d.get("exception_info")
    print(f"AGENT_RAN_STEPS={d.get('n_agent_steps')}   (>0 = harness healthy; 0/None = broke before agent)")
    print(f"EXIT_STATUS={traj_info.get('exit_status') or info.get('exit_status')}   (Submitted = agent finished + submitted a patch)")
    print(f"API_CALLS={(traj_info.get('model_stats') or info.get('model_stats') or {}).get('api_calls')}")
    print(f"REWARD={(vr.get('rewards') or {}).get('reward')}   (1.0 = task resolved)")
    print(f"EXCEPTION={(exc or {}).get('exception_type') if exc else None}")

# verifier: pass/fail tally + the failing tests (the precise correctness gap)
vouts = glob.glob(os.path.join(jobs, "*", "*__*", "verifier", "test-stdout.txt"))
if vouts:
    txt = ANSI.sub("", open(vouts[-1], encoding="utf-8", errors="replace").read())
    tally = [l.strip() for l in txt.splitlines() if re.search(r"\d+ (passing|failing|pending)", l)]
    print("--- verifier tally ---")
    for l in tally[-5:]:
        print("  " + l)
    m = re.search(r"\n\s*\d+ failing", txt)
    if m:
        print("--- failing tests (the correctness gap) ---")
        for l in txt[m.start():m.start() + 3500].splitlines():
            if re.search(r"^\s*\d+\)|Error|expected|Unable to resolve|throw|AssertionError", l):
                print("  " + l.strip()[:160])
else:
    print("(no verifier/test-stdout.txt found)")

# GT hook firings from the REAL per-turn trajectory (NOT result.json, whose step_results is empty)
trajs = glob.glob(os.path.join(jobs, "*", "*__*", "agent", "mini-swe-agent.trajectory.json"))
if trajs:
    t = open(trajs[-1], encoding="utf-8", errors="replace").read()
    c = Counter(re.findall(
        r"gt_hook|gt understand|gt verify|<gt-evidence>|behavioral_contract|post_edit|post_view|CONSENSUS",
        t, re.I))
    print("--- GT hook firings (agent trajectory) ---")
    for k, v in c.most_common():
        print(f"  {v:4} {k}")
