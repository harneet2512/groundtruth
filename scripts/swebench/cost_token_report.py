"""Detailed token + cost report for an OH eval run.

Token telemetry is broken for deepseek-v4-flash everywhere the workflow looked:
OH metrics.accumulated_token_usage = 0, metrics.token_usages = [], litellm
accumulated_cost = 0 (cost-map miss), and the run log has no clean per-call counts.
The ONE reliable source is OH's logged completions — `log_completions: true` writes
each raw request/response to log_completions_folder, and DeepSeek's API response
carries the real `usage` {prompt_tokens, completion_tokens, ...}. This scans those.

Usage:
  python3 scripts/swebench/cost_token_report.py --root <dir> [--emit summary.json]
    --root: scanned for **/logs/completions/**/*.json (primary) and **/output.jsonl
            (task count + metrics fallback).
    --emit: write a JSON totals file (for per-shard -> merge aggregation).
  python3 scripts/swebench/cost_token_report.py --merge <glob_dir>   # sum *.json summaries

Cost is an ESTIMATE from per-million prices (override via env); ground truth for an
unmapped model is the DeepSeek API balance delta.
  GT_PRICE_IN_PER_M (0.27)  GT_PRICE_CACHED_IN_PER_M (0.07)  GT_PRICE_OUT_PER_M (1.10)
"""
from __future__ import annotations
import argparse
import glob
import json
import os

PRICE_IN = float(os.environ.get("GT_PRICE_IN_PER_M", "0.27"))
PRICE_CACHED_IN = float(os.environ.get("GT_PRICE_CACHED_IN_PER_M", "0.07"))
PRICE_OUT = float(os.environ.get("GT_PRICE_OUT_PER_M", "1.10"))


def _num(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0


def _usage_from(obj):
    """Pull a usage dict out of a logged completion, tolerant of nesting."""
    if not isinstance(obj, dict):
        return None
    if "usage" in obj and isinstance(obj["usage"], dict):
        return obj["usage"]
    for k in ("response", "litellm_response", "raw_response", "result"):
        u = _usage_from(obj.get(k)) if isinstance(obj.get(k), dict) else None
        if u:
            return u
    return None


def scan(root: str):
    calls = prompt = completion = cache_read = cache_write = 0
    # primary: logged completions (real DeepSeek usage)
    for cf in glob.glob(os.path.join(root, "**", "logs", "completions", "**", "*.json"), recursive=True):
        try:
            obj = json.load(open(cf, encoding="utf-8"))
        except Exception:
            continue
        u = _usage_from(obj)
        if not u:
            continue
        calls += 1
        prompt += _num(u.get("prompt_tokens"))
        completion += _num(u.get("completion_tokens"))
        details = u.get("prompt_tokens_details") or {}
        cache_read += _num(u.get("cache_read_tokens") or details.get("cached_tokens"))
        cache_write += _num(u.get("cache_write_tokens") or details.get("cache_creation_tokens"))
    tasks = 0
    for oj in glob.glob(os.path.join(root, "**", "output.jsonl"), recursive=True):
        with open(oj, encoding="utf-8") as f:
            tasks += sum(1 for ln in f if ln.strip())
    return dict(tasks=tasks, calls=calls, prompt=prompt, completion=completion,
                cache_read=cache_read, cache_write=cache_write)


def report(t: dict):
    fresh_in = max(t["prompt"] - t["cache_read"], 0)
    est = (fresh_in * PRICE_IN + t["cache_read"] * PRICE_CACHED_IN + t["completion"] * PRICE_OUT) / 1_000_000
    total = t["prompt"] + t["completion"]
    print("=== TOKEN + COST REPORT ===")
    print(f"tasks={t['tasks']}  llm_calls={t['calls']}")
    print(f"prompt_tokens={int(t['prompt']):,}  completion_tokens={int(t['completion']):,}")
    print(f"cache_read_tokens={int(t['cache_read']):,}  cache_write_tokens={int(t['cache_write']):,}")
    print(f"total_tokens={int(total):,}")
    print(f"ESTIMATED_cost_usd={est:.4f}  (@ in=${PRICE_IN}/M cached=${PRICE_CACHED_IN}/M out=${PRICE_OUT}/M)")
    if t["tasks"]:
        print(f"est_cost_per_task_usd={est / t['tasks']:.5f}  total_tokens_per_task={int(total / t['tasks']):,}")
    if t["calls"] == 0:
        print("WARNING: 0 completions found — log_completions may be off or folder not captured.")
    print("NOTE: ground-truth cost = DeepSeek API balance delta; this is a token-based estimate.")
    return est


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp")
    ap.add_argument("--emit", default="")
    ap.add_argument("--merge", default="")
    args = ap.parse_args()
    if args.merge:
        agg = dict(tasks=0.0, calls=0.0, prompt=0.0, completion=0.0, cache_read=0.0, cache_write=0.0)
        for jf in glob.glob(os.path.join(args.merge, "**", "token_summary*.json"), recursive=True):
            try:
                d = json.load(open(jf, encoding="utf-8"))
            except Exception:
                continue
            for k in agg:
                agg[k] += _num(d.get(k))
        report(agg)
        return
    t = scan(args.root)
    report(t)
    if args.emit:
        os.makedirs(os.path.dirname(args.emit) or ".", exist_ok=True)
        json.dump(t, open(args.emit, "w"))
        print(f"emitted -> {args.emit}")


if __name__ == "__main__":
    main()
