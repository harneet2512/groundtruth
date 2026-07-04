#!/usr/bin/env python3
"""Periodic cost monitor — appends a snapshot every N seconds to cost_breakdown.json.

Sums tokens across llm_completions/* for each arm in a paired run dir, computes
cost using the model rate table from aggregate_per_task_summary.py, and writes
a cumulative + delta record.

Designed to run in tmux alongside an in-flight smoke. SIGTERM-safe (graceful exit).

Usage:
    python scripts/swebench/cost_monitor.py --run-dir <paired_run_root> \
        --interval 300 --abort-threshold-usd 5.0
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Reuse rate table + completion iteration from the aggregator.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aggregate_per_task_summary import (  # type: ignore  # noqa: E402
    PRICE_TABLE,
    find_oh_run_dirs,
    iter_completions,
    model_to_rates,
)

_STOP = False


def _sigterm(_sig, _frame):
    global _STOP
    _STOP = True
    print("# cost_monitor: SIGTERM received, stopping after this iteration", flush=True)


def snapshot_arm(arm_dir: Path) -> dict[str, Any]:
    in_tot = 0
    out_tot = 0
    cached_tot = 0
    n_calls = 0
    cost_field_sum = 0.0
    cost_token_math = 0.0
    seen_models: set[str] = set()
    for combo in find_oh_run_dirs(arm_dir):
        completions_root = combo / "llm_completions"
        if not completions_root.is_dir():
            continue
        for inst_dir in completions_root.iterdir():
            if not inst_dir.is_dir():
                continue
            iid = inst_dir.name
            for d in iter_completions(combo, iid):
                n_calls += 1
                model = (
                    d.get("model")
                    or (d.get("metadata") or {}).get("model")
                    or "deepseek-v3.2"
                )
                seen_models.add(model)
                rates = model_to_rates(model)
                u = (d.get("response") or {}).get("usage") or {}
                pt = int(u.get("prompt_tokens") or 0)
                ct = int(u.get("completion_tokens") or 0)
                cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
                in_tot += pt
                out_tot += ct
                cached_tot += cached
                in_uncached = max(0, pt - cached)
                cost_token_math += (
                    in_uncached * rates["in"] / 1_000_000
                    + ct * rates["out"] / 1_000_000
                )
                c = d.get("cost")
                if c is not None:
                    try:
                        cost_field_sum += float(c)
                    except (TypeError, ValueError):
                        pass
    return {
        "n_calls": n_calls,
        "input_tokens": in_tot,
        "output_tokens": out_tot,
        "cached_input_tokens": cached_tot,
        "cost_token_math_usd": round(cost_token_math, 6),
        "cost_field_usd": round(cost_field_sum, 6),
        "models_seen": sorted(seen_models),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Periodic cost monitor for a paired run")
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--interval", type=int, default=300, help="Seconds between snapshots")
    p.add_argument("--abort-threshold-usd", type=float, default=None,
                   help="Print loud warning if total exceeds this (does NOT actually abort)")
    p.add_argument("--out", type=Path, default=None,
                   help="Output JSONL (default: <run-dir>/cost_breakdown.jsonl)")
    args = p.parse_args(argv)

    run_dir: Path = args.run_dir
    if not run_dir.is_dir():
        print(f"ERROR: run-dir does not exist: {run_dir}", file=sys.stderr)
        return 2

    out = args.out or (run_dir / "cost_breakdown.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    print(f"# cost_monitor: run_dir={run_dir} interval={args.interval}s out={out}", flush=True)
    last_total = 0.0
    iter_n = 0
    while not _STOP:
        iter_n += 1
        ts = time.time()
        rec: dict[str, Any] = {"ts": ts, "iter": iter_n, "arms": {}}
        for arm in ("A", "B"):
            arm_dir = run_dir / f"arm_{arm}"
            if arm_dir.is_dir():
                rec["arms"][arm] = snapshot_arm(arm_dir)
        total_token_math = sum(
            (rec["arms"].get(a, {}) or {}).get("cost_token_math_usd", 0.0)
            for a in ("A", "B")
        )
        total_field = sum(
            (rec["arms"].get(a, {}) or {}).get("cost_field_usd", 0.0)
            for a in ("A", "B")
        )
        rec["total_cost_token_math_usd"] = round(total_token_math, 6)
        rec["total_cost_field_usd"] = round(total_field, 6)
        rec["delta_since_last"] = round(total_token_math - last_total, 6)
        last_total = total_token_math

        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

        # Print compact line.
        a_calls = (rec["arms"].get("A") or {}).get("n_calls", 0)
        b_calls = (rec["arms"].get("B") or {}).get("n_calls", 0)
        a_cost = (rec["arms"].get("A") or {}).get("cost_token_math_usd", 0.0)
        b_cost = (rec["arms"].get("B") or {}).get("cost_token_math_usd", 0.0)
        warn = ""
        if args.abort_threshold_usd is not None and total_token_math >= args.abort_threshold_usd:
            warn = f" *** OVER THRESHOLD ${args.abort_threshold_usd} ***"
        print(
            f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] "
            f"A: {a_calls} calls ${a_cost:.4f} | "
            f"B: {b_calls} calls ${b_cost:.4f} | "
            f"total token-math ${total_token_math:.4f}{warn}",
            flush=True,
        )

        # Sleep in small chunks so SIGTERM is responsive.
        slept = 0
        while slept < args.interval and not _STOP:
            time.sleep(min(2, args.interval - slept))
            slept += 2

    print("# cost_monitor: stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
