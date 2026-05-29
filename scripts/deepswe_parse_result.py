#!/usr/bin/env python3
"""Parse pier TRIAL-level result.json verdicts under a root dir (or a single file).

Usage: deepswe_parse_result.py <root_dir_or_file>

Pier writes TWO result.json per task:
  - job-level  jobs/<task>/result.json            (JobStats; no verifier_result)
  - trial-level jobs/<task>/<trial>/result.json   (TrialResult; has verifier_result)
The verdict lives in the TRIAL-level file: verifier_result.rewards.reward
(1.0 = base AND new tests passed = RESOLVED). We skip job-level files.
"""
import glob
import json
import os
import sys


def _result_files(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    return sorted(glob.glob(os.path.join(root, "**", "result.json"), recursive=True))


def main() -> int:
    root = sys.argv[1]
    rows = []
    for f in _result_files(root):
        try:
            d = json.load(open(f))
        except Exception:  # noqa: BLE001
            continue
        vr = d.get("verifier_result")
        if not isinstance(vr, dict):
            continue  # job-level JobStats — skip
        reward = (vr.get("rewards") or {}).get("reward")
        rows.append((d.get("task_name") or "?", reward, d.get("agent_result") or {}))

    if not rows:
        print("- [WARN] no trial-level result.json (verifier_result) found")
        return 0

    for task, reward, ar in rows:
        if reward is None:
            verdict = "[WARN] no reward (agent/infra error)"
        elif float(reward) >= 1.0:
            verdict = "[PASS] RESOLVED"
        else:
            verdict = "[FAIL] NOT RESOLVED"
        print(
            f"- {task}: **{verdict}** (reward={reward}) "
            f"steps={ar.get('n_agent_steps')} "
            f"in_tok={ar.get('n_input_tokens')} out_tok={ar.get('n_output_tokens')} "
            f"cost_usd={ar.get('cost_usd')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
