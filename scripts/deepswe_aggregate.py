#!/usr/bin/env python3
"""Aggregate pier TRIAL-level result.json verdicts into one table.

Usage: deepswe_aggregate.py <artifacts_root>

Walks <root>/**/result.json, keeps only TRIAL-level files (those with
verifier_result), and tallies resolved/total from verifier_result.rewards.reward.
cost_usd is null for models litellm has no pricing for (e.g. deepseek-v4-flash);
in that case we report token totals instead.
"""
import glob
import json
import os
import sys


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    resolved: list[str] = []
    failed: list[str] = []
    noverdict: list[str] = []
    in_tok = 0
    out_tok = 0
    cost = 0.0
    cost_any = False

    for f in glob.glob(os.path.join(root, "**", "result.json"), recursive=True):
        try:
            d = json.load(open(f))
        except Exception:  # noqa: BLE001
            continue
        vr = d.get("verifier_result")
        if not isinstance(vr, dict):
            continue  # job-level JobStats — skip
        task = d.get("task_name") or "?"
        reward = (vr.get("rewards") or {}).get("reward")
        if reward is None:
            noverdict.append(task)
        elif float(reward) >= 1.0:
            resolved.append(task)
        else:
            failed.append(task)
        ar = d.get("agent_result") or {}
        in_tok += ar.get("n_input_tokens") or 0
        out_tok += ar.get("n_output_tokens") or 0
        if ar.get("cost_usd"):
            cost += ar["cost_usd"]
            cost_any = True

    total = len(resolved) + len(failed) + len(noverdict)
    pct = f" ({100 * len(resolved) / total:.0f}%)" if total else ""
    print("# DeepSWE Baseline — Aggregate")
    print()
    print(f"- **Resolved: {len(resolved)}/{total}{pct}**")
    if noverdict:
        print(f"- no-verdict (agent/infra error): {len(noverdict)} → {sorted(noverdict)}")
    print(f"- tokens: in={in_tok:,} out={out_tok:,}")
    if cost_any:
        print(f"- **cost: ${cost:.4f}**")
    else:
        print("- cost: n/a (litellm has no pricing for this model; token totals above)")
    print()
    if resolved:
        print(f"- RESOLVED: {sorted(resolved)}")
    if failed:
        print(f"- NOT RESOLVED: {sorted(failed)}")
    if total == 0:
        print("- [WARN] no trial verdicts parsed — every task likely hit an infra/resource failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
