#!/usr/bin/env python3
"""Compute per-run tokens, cost, steps, and GT metrics from SWE-bench-Live run artifacts.

WHY: litellm + OpenRouter both return null cost for deepseek-v4-flash, so OH's
`metrics.accumulated_cost` is 0. The TOKENS are captured, though — in each history
event's `llm_metrics.accumulated_token_usage`. We take the last (cumulative) reading per
task and compute cost deterministically from token counts x the rates in
benchmarks/pricing/deepseek_pricing.json. Reproducible: anyone re-running this on the
same artifacts gets the same numbers.

Cost formula (per task):
  billable_input = prompt_tokens - cache_read_tokens   # cache-miss prompt tokens
  cost = billable_input/1e6 * input_per_1m
       + cache_read_tokens/1e6 * cache_hit_per_1m
       + completion_tokens/1e6 * output_per_1m

Usage:
  python scripts/metrics/compute_run_metrics.py --artifacts <dir> --model deepseek-v4-flash \
      --out-json run_metrics.json --out-md RUN_METRICS.md [--run-id <id>]

<dir> is a directory containing one or more `task-<id>/` subtrees (as produced by
`gh run download`), each with results/**/output.jsonl and eval_result.json.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path


def _load_pricing(model: str) -> dict:
    p = Path(__file__).resolve().parents[2] / "benchmarks" / "pricing" / "deepseek_pricing.json"
    rates = json.loads(p.read_text()).get("models", {}).get(model)
    if not rates:
        raise SystemExit(f"no pricing for model={model} in {p}")
    return rates


def _last_token_usage(output_jsonl: str) -> dict:
    """Return the last cumulative accumulated_token_usage in the trajectory, or zeros."""
    with open(output_jsonl, encoding="utf-8", errors="replace") as f:
        line = f.readline()
    if not line.strip():
        return {}
    d = json.loads(line)
    best = {}
    # top-level metrics first (final state), then walk history for the max cumulative
    for src in [d.get("metrics", {})] + [e.get("llm_metrics", {}) for e in d.get("history", [])]:
        atu = (src or {}).get("accumulated_token_usage") or {}
        if (atu.get("prompt_tokens") or 0) >= (best.get("prompt_tokens") or 0) and atu:
            best = atu
    return best


def _action_stats(output_jsonl: str) -> dict:
    with open(output_jsonl, encoding="utf-8", errors="replace") as f:
        d = json.loads(f.readline() or "{}")
    hist = d.get("history", [])
    NON_ACTIONS = {"think", "recall", "message", "null", "", None}
    actions = [e for e in hist if e.get("action") and e.get("action") not in NON_ACTIONS]
    edits = [e for e in hist if e.get("action") in ("edit", "write") or "str_replace" in str(e.get("args", {}))]
    patch = (d.get("test_result", {}) or {}).get("git_patch", "") or d.get("git_patch", "") or ""
    return {
        "history_events": len(hist),
        "action_count": len(actions),
        "edit_count": len(edits),
        "patch_chars": len(patch),
        "has_patch": "diff" in patch,
    }


def _resolved(task_dir: str) -> str:
    er = glob.glob(os.path.join(task_dir, "**", "eval_result.json"), recursive=True)
    if not er:
        return "unknown"
    try:
        r = json.load(open(er[0], encoding="utf-8"))
    except Exception:
        return "unknown"
    if r.get("status") == "eval_no_report":
        return "no_report"
    if r.get("resolved_instances") == 1 or r.get("resolved_ids"):
        return "RESOLVED"
    return "NO"


def _gt_layers(task_dir: str) -> dict:
    """Count GT layer-event emissions if the JSONL is present (cross-ref only)."""
    le = glob.glob(os.path.join(task_dir, "**", "gt_layer_events_*.jsonl"), recursive=True)
    out: dict[str, int] = {}
    if not le:
        return out
    for line in open(le[0], encoding="utf-8", errors="replace"):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("emitted"):
            out[ev.get("layer", "?")] = out.get(ev.get("layer", "?"), 0) + 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--out-json", default="run_metrics.json")
    ap.add_argument("--out-md", default="RUN_METRICS.md")
    a = ap.parse_args()

    rates = _load_pricing(a.model)
    task_dirs = sorted(d for d in glob.glob(os.path.join(a.artifacts, "task-*")) if os.path.isdir(d))
    if not task_dirs:
        # also accept the flat <task>/results layout
        task_dirs = sorted(d for d in glob.glob(os.path.join(a.artifacts, "*")) if os.path.isdir(d))

    rows = []
    tot = {"prompt": 0, "completion": 0, "cache_read": 0, "cost": 0.0, "actions": 0, "edits": 0}
    for td in task_dirs:
        ojs = glob.glob(os.path.join(td, "**", "output.jsonl"), recursive=True)
        if not ojs:
            continue
        task = os.path.basename(td).replace("task-", "")
        atu = _last_token_usage(ojs[0])
        prompt = int(atu.get("prompt_tokens") or 0)
        completion = int(atu.get("completion_tokens") or 0)
        cache_read = int(atu.get("cache_read_tokens") or 0)
        billable_input = max(0, prompt - cache_read)
        cost = (
            billable_input / 1e6 * rates["input_per_1m"]
            + cache_read / 1e6 * rates["cache_hit_per_1m"]
            + completion / 1e6 * rates["output_per_1m"]
        )
        st = _action_stats(ojs[0])
        row = {
            "task": task,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_read_tokens": cache_read,
            "billable_input_tokens": billable_input,
            "cost_usd": round(cost, 8),
            "action_count": st["action_count"],
            "edit_count": st["edit_count"],
            "has_patch": st["has_patch"],
            "resolved": _resolved(td),
            "gt_layers_emitted": _gt_layers(td),
        }
        rows.append(row)
        tot["prompt"] += prompt
        tot["completion"] += completion
        tot["cache_read"] += cache_read
        tot["cost"] += cost
        tot["actions"] += st["action_count"]
        tot["edits"] += st["edit_count"]

    n = len(rows)
    resolved = sum(1 for r in rows if r["resolved"] == "RESOLVED")
    summary = {
        "run_id": a.run_id,
        "model": a.model,
        "pricing_per_1m": rates,
        "task_count": n,
        "resolved": resolved,
        "resolved_rate": round(resolved / n, 8) if n else 0.0,
        "total_prompt_tokens": tot["prompt"],
        "total_completion_tokens": tot["completion"],
        "total_cache_read_tokens": tot["cache_read"],
        "total_cost_usd": round(tot["cost"], 8),
        "mean_cost_per_task_usd": round(tot["cost"] / n, 8) if n else 0.0,
        "total_action_count": tot["actions"],
        "mean_actions_per_task": round(tot["actions"] / n, 8) if n else 0.0,
        "total_edit_count": tot["edits"],
        "tasks": rows,
    }
    Path(a.out_json).write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # readable markdown
    L = []
    L.append(f"# Run metrics — {a.run_id or a.artifacts}")
    L.append("")
    L.append(f"- model: `{a.model}` | tasks: {n} | resolved: {resolved}/{n} ({summary['resolved_rate']:.2%})")
    L.append(f"- **total cost: ${summary['total_cost_usd']:.6f}** (mean ${summary['mean_cost_per_task_usd']:.6f}/task)")
    L.append(f"- tokens: prompt {tot['prompt']:,} | completion {tot['completion']:,} | cache-read {tot['cache_read']:,}")
    L.append(f"- actions: {tot['actions']} total, {summary['mean_actions_per_task']:.1f} mean/task | edits: {tot['edits']}")
    L.append(f"- pricing (USD/1M): input {rates['input_per_1m']}, cache-hit {rates['cache_hit_per_1m']}, output {rates['output_per_1m']}")
    L.append("")
    L.append("| task | resolved | prompt | completion | cache_read | cost $ | actions | edits |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        L.append(f"| {r['task']} | {r['resolved']} | {r['prompt_tokens']:,} | {r['completion_tokens']:,} | "
                 f"{r['cache_read_tokens']:,} | {r['cost_usd']:.6f} | {r['action_count']} | {r['edit_count']} |")
    L.append("")
    L.append("Cost = (prompt-cache_read)/1e6*input_rate + cache_read/1e6*cache_rate + completion/1e6*output_rate. "
             "Rates are configurable in benchmarks/pricing/deepseek_pricing.json. See METRICS_EXPLAINED.md.")
    Path(a.out_md).write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {a.out_json} + {a.out_md}: {n} tasks, ${summary['total_cost_usd']:.6f} total, {resolved}/{n} resolved")


if __name__ == "__main__":
    main()
