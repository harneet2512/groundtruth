#!/usr/bin/env python3
"""Aggregate all per-task pier result.json files into one verdict table.

Usage: deepswe_aggregate.py <artifacts_root>

Walks <artifacts_root>/**/result.json (one per matrix task), tallies
resolved/total from reward_stats, and sums cost/tokens. Emits markdown.
"""
import glob
import json
import os
import sys


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    resolved: list[str] = []
    failed: list[str] = []
    cost = 0.0
    in_tok = 0
    out_tok = 0
    errored = 0
    seen = 0
    parse_err = 0

    for f in glob.glob(os.path.join(root, "**", "result.json"), recursive=True):
        seen += 1
        try:
            d = json.load(open(f))
        except Exception:  # noqa: BLE001
            parse_err += 1
            continue
        cost += d.get("cost_usd") or 0.0
        in_tok += d.get("n_input_tokens") or 0
        out_tok += d.get("n_output_tokens") or 0
        errored += d.get("n_errored_trials") or 0
        for ev in (d.get("evals") or {}).values():
            for buckets in (ev.get("reward_stats") or {}).values():
                for val, ids in buckets.items():
                    (resolved if float(val) >= 1.0 else failed).extend(ids)

    total = len(resolved) + len(failed)
    pct = f" ({100 * len(resolved) / total:.0f}%)" if total else ""
    print("# DeepSWE Baseline — Aggregate")
    print()
    print(f"- **Resolved: {len(resolved)}/{total}{pct}**")
    print(f"- result.json files found: {seen} (parse errors: {parse_err})")
    print(f"- errored trials: {errored}")
    print(f"- **total cost: ${cost:.4f}**  (in_tok={in_tok:,} out_tok={out_tok:,})")
    print()
    if resolved:
        print(f"- resolved: {sorted(resolved)}")
    if failed:
        print(f"- failed: {sorted(failed)}")
    if total == 0:
        print("- [WARN] no verdicts parsed — every task likely hit an infra/resource failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
