#!/usr/bin/env python3
"""Parse a single pier job result.json and emit markdown summary lines.

Usage: deepswe_parse_result.py <result.json> [task_id]

Pier writes JobStats (pier/models/job/result.py) to jobs/<name>/result.json.
The verdict lives in evals[*].reward_stats["reward"]: {1.0: [resolved ids], 0.0: [failed ids]}.
reward == 1.0 means the task's base (regression) AND new (feature) tests both passed.
"""
import json
import sys


def main() -> int:
    path = sys.argv[1]
    try:
        d = json.load(open(path))
    except Exception as e:  # noqa: BLE001
        print(f"- [WARN] failed to read {path}: {e}")
        return 0

    resolved: list[str] = []
    failed: list[str] = []
    for ev in (d.get("evals") or {}).values():
        for buckets in (ev.get("reward_stats") or {}).values():
            for val, ids in buckets.items():
                (resolved if float(val) >= 1.0 else failed).extend(ids)

    if resolved and not failed:
        verdict = "[PASS] RESOLVED"
    elif failed:
        verdict = "[FAIL] NOT RESOLVED"
    else:
        verdict = "[WARN] no reward (infra/agent error)"

    print(f"- verdict: **{verdict}**")
    if resolved:
        print(f"- resolved: {resolved}")
    if failed:
        print(f"- failed: {failed}")
    print(
        f"- cost_usd={d.get('cost_usd')} "
        f"in_tok={d.get('n_input_tokens')} out_tok={d.get('n_output_tokens')} "
        f"errored_trials={d.get('n_errored_trials')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
