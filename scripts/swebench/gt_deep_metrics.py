#!/usr/bin/env python3
"""Deep per-run metric dumper — writes gt_deep_metrics_<task>.json at 8-decimal
precision from the run's recorded telemetry + the agent's OWN observations
(output.jsonl), per the constitution rule "Deep per-run logging at 8-decimal
precision". Every run (OH / mini-swe-agent / DeepSWE) calls this after the agent
finishes. A run without this file is not done.

Usage: gt_deep_metrics.py <task_id> [results_dir] [--baseline <baseline_deep.json>]
Sources (best-effort, all optional — absence is recorded, never fatal):
  /tmp/gt_run_summary_<task>.json   per-layer eligible/emitted/suppressed/rendered_tokens/util
  /tmp/gt_layer_events_<task>.jsonl  layer firings
  /tmp/gt_interactions_<task>.jsonl  every delivery
  <results_dir>/**/output.jsonl      the agent trajectory (TRUTH for delivery + tokens)
"""
from __future__ import annotations

import glob
import json
import os
import sys


def d8(x) -> float:
    """Round to 8 decimal places — full precision, never 2-dp. NaN/None -> 0.0."""
    try:
        return round(float(x), 8)
    except (TypeError, ValueError):
        return 0.0


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _find_output_jsonl(task: str, results_dir: str) -> str | None:
    for base in (results_dir, f"/tmp/results_{task}", "/tmp/gt"):
        if not base:
            continue
        hits = glob.glob(os.path.join(base, "**", "output.jsonl"), recursive=True)
        if hits:
            return hits[0]
    return None


def _from_trajectory(task: str, results_dir: str) -> dict:
    """The AGENT'S side — derived from output.jsonl history (the only delivery truth)."""
    oj = _find_output_jsonl(task, results_dir)
    out = {
        "output_jsonl": oj or "",
        "action_count": 0,
        "edits": 0,
        "first_edit_action": 0,
        "flows_delivered": 0,
        "contracts_delivered": 0,
        "consensus_delivered": 0,
        "test_delivered": 0,
        "gt_observation_chars_total": 0,
        "resolved": None,
        "has_patch": False,
    }
    if not oj or not os.path.exists(oj):
        return out
    try:
        d = json.loads(open(oj, encoding="utf-8").readline())
    except (OSError, json.JSONDecodeError, StopIteration):
        return out
    hist = d.get("history", [])
    n = 0
    for e in hist:
        if e.get("action"):
            n += 1
            a = e.get("args", {})
            if e.get("action") in ("edit",) or "str_replace" in str(a.get("command", "")):
                out["edits"] += 1
                if not out["first_edit_action"]:
                    out["first_edit_action"] = n
        c = e.get("content") or ""
        if c:
            if "flows:" in c:
                out["flows_delivered"] += 1
            if "[CONTRACT]" in c:
                out["contracts_delivered"] += 1
            if "gt-scope" in c or "CONSENSUS" in c:
                out["consensus_delivered"] += 1
            if "[TEST" in c.upper() or "Called by" in c:
                out["test_delivered"] += 1
            if c.startswith("[GT]") or "<gt-" in c:
                out["gt_observation_chars_total"] += len(c)
    out["action_count"] = n
    tr = d.get("test_result") or {}
    p = tr.get("git_patch") or d.get("git_patch") or ""
    out["has_patch"] = bool(p.strip())
    out["resolved"] = d.get("resolved")
    return out


def _from_cost_log(log_path: str) -> dict:
    """LLM token/cost efficiency from the run log's [GT_COST] lines:
    `[GT_COST] call=N in=X out=Y cached=Z cost=$W total=$T ...`. Summed across calls."""
    import re
    out = {"llm_calls": 0, "llm_tokens_in": 0, "llm_tokens_out": 0,
           "llm_tokens_cached": 0, "llm_cost_usd": 0.0}
    if not log_path or not os.path.exists(log_path):
        return out
    pat = re.compile(
        r"\[GT_COST\]\s+call=(\d+)\s+in=(\d+)\s+out=(\d+)\s+cached=(\d+)\s+cost=\$([0-9.]+)"
    )
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pat.search(line)
                if not m:
                    continue
                out["llm_calls"] += 1
                out["llm_tokens_in"] += int(m.group(2))
                out["llm_tokens_out"] += int(m.group(3))
                out["llm_tokens_cached"] += int(m.group(4))
                out["llm_cost_usd"] += float(m.group(5))
    except OSError:
        pass
    return out


def build(task: str, results_dir: str, log_path: str = "") -> dict:
    summ = _load_json(f"/tmp/gt_run_summary_{task}.json") or {}
    per_layer_raw = summ.get("per_layer", {})
    per_layer = {}
    inj_tokens_total = 0.0
    for layer, m in per_layer_raw.items():
        rt = d8(m.get("rendered_tokens_total", 0))
        inj_tokens_total += rt
        elig = d8(m.get("eligible", 0))
        emit = d8(m.get("emitted", 0))
        per_layer[layer] = {
            "eligible": elig,
            "emitted": emit,
            "suppressed": d8(m.get("suppressed", 0)),
            "rendered_tokens_total": rt,
            "utilization_score": d8(m.get("utilization_score", 0)),
            "next_action_count": d8(m.get("next_action_count", 0)),
            "emit_rate": d8(emit / elig) if elig else 0.0,
        }
    traj = _from_trajectory(task, results_dir)
    cost = _from_cost_log(log_path)
    actions = traj.get("action_count", 0) or 0
    llm_total = cost["llm_tokens_in"] + cost["llm_tokens_out"]
    # token/cost EFFICIENCY (the constitution's honest token story: GT injection vs LLM usage)
    efficiency = {
        "llm_calls": d8(cost["llm_calls"]),
        "llm_tokens_in": d8(cost["llm_tokens_in"]),
        "llm_tokens_out": d8(cost["llm_tokens_out"]),
        "llm_tokens_cached": d8(cost["llm_tokens_cached"]),
        "llm_tokens_total": d8(llm_total),
        "llm_cost_usd": d8(cost["llm_cost_usd"]),
        "gt_injected_tokens_total": d8(inj_tokens_total),
        "tokens_per_action": d8(llm_total / actions) if actions else 0.0,
        "cost_per_action_usd": d8(cost["llm_cost_usd"] / actions) if actions else 0.0,
        # GT's added context as a fraction of total LLM input — the honest overhead figure
        "gt_injection_overhead_pct": d8(100.0 * inj_tokens_total / cost["llm_tokens_in"]) if cost["llm_tokens_in"] else 0.0,
    }
    deep = {
        "task_id": task,
        "schema": "gt_deep_metrics.v1",
        "precision_decimals": 8,
        "layers_active": summ.get("layers_active", []),
        "total_layer_events": d8(summ.get("total_layer_events", 0)),
        "total_agent_events": d8(summ.get("total_agent_events", 0)),
        "gt_injected_tokens_total": d8(inj_tokens_total),
        "efficiency": efficiency,
        "per_layer": per_layer,
        "agent": {k: (d8(v) if isinstance(v, (int, float)) else v) for k, v in traj.items()},
    }
    return deep


def pair(gt: dict, base: dict) -> dict:
    """GT-on vs baseline deltas at 8-dp (negative delta on action_count = GT is better)."""
    g, b = gt.get("agent", {}), base.get("agent", {})
    def dlt(k):
        return d8(d8(g.get(k, 0)) - d8(b.get(k, 0)))
    return {
        "task_id": gt.get("task_id"),
        "schema": "gt_metrics_delta.v1",
        "precision_decimals": 8,
        "action_count_delta": dlt("action_count"),
        "first_edit_delta": dlt("first_edit_action"),
        "token_delta": d8(d8(gt.get("gt_injected_tokens_total", 0))),  # GT-side injected cost
        "resolved_gt": g.get("resolved"),
        "resolved_baseline": b.get("resolved"),
        "flows_delivered": d8(g.get("flows_delivered", 0)),
        "contracts_delivered": d8(g.get("contracts_delivered", 0)),
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: gt_deep_metrics.py <task_id> [results_dir] [--baseline <file>]")
        return 2
    task = args[0]
    results_dir = args[1] if len(args) > 1 else f"/tmp/results_{task}"
    log_path = ""
    if "--log" in sys.argv:
        log_path = sys.argv[sys.argv.index("--log") + 1]
    elif os.path.exists(f"/tmp/agent_{task}.log"):
        log_path = f"/tmp/agent_{task}.log"
    deep = build(task, results_dir, log_path)
    out_path = f"/tmp/gt_deep_metrics_{task}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(deep, f, indent=2)
    eff = deep["efficiency"]
    print(f"[GT_DEEP] wrote {out_path}: actions={deep['agent']['action_count']} "
          f"flows={deep['agent']['flows_delivered']} llm_tokens={eff['llm_tokens_total']} "
          f"cost=${eff['llm_cost_usd']} tokens/action={eff['tokens_per_action']} "
          f"gt_overhead={eff['gt_injection_overhead_pct']}% layers={len(deep['per_layer'])}")
    if "--baseline" in sys.argv:
        bpath = sys.argv[sys.argv.index("--baseline") + 1]
        base = _load_json(bpath)
        if base:
            delta = pair(deep, base)
            dpath = f"/tmp/gt_metrics_delta_{task}.json"
            with open(dpath, "w", encoding="utf-8") as f:
                json.dump(delta, f, indent=2)
            print(f"[GT_DEEP] wrote {dpath}: action_count_delta={delta['action_count_delta']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
