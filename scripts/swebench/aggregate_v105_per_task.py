#!/usr/bin/env python3
"""v1.0.5 per-task summary aggregator — rolls up the 5 telemetry layers.

Given a telemetry directory ``/tmp/gt_telemetry_<instance_id>/`` written by
the v1.0.5 apparatus, produces ``per_task_summary.json`` with the fields the
plan calls out:

  instance_id, arm
  turns_used, completed, patch_generated, eval_resolved
  total tokens (input / output, cache split)
  total cost
  wall time
  GT layer counts: hook fires, endpoint calls per endpoint, gate triggers,
                   soft-escapes
  behavioral metrics: focus_file_edit_rate, focus_function_edit_rate,
                      hook_engagement_rate, endpoint_engagement_rate,
                      gate_re_edit_rate

Token / cost / wall fields are filled when an OH-side `output.jsonl`,
`llm_completions/*.json`, or trajectory file is provided via flags; otherwise
they're emitted as ``null`` rather than guessed.

Usage:
  python aggregate_v105_per_task.py \\
      --telemetry-dir /tmp/gt_telemetry_<id> \\
      --output-dir   /home/Lenovo/groundtruth/runs/v105_probe/<id>/ \\
      [--oh-output-jsonl /path/to/output.jsonl] \\
      [--llm-completions-dir /path/to/llm_completions/] \\
      [--results-json /path/to/results.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _safe_float(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _safe_int(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _layer_counts(records: list[dict]) -> int:
    return len(records)


def _endpoint_breakdown(layer4: list[dict]) -> dict[str, dict[str, Any]]:
    by_endpoint: dict[str, dict[str, Any]] = {}
    for rec in layer4:
        ep = rec.get("endpoint", "unknown")
        slot = by_endpoint.setdefault(
            ep,
            {"calls": 0, "budget_exhausted": 0, "tier_distribution": {}, "latency_ms_total": 0.0},
        )
        slot["calls"] += 1
        out = rec.get("output_preview", "") or ""
        if out.startswith("BUDGET_EXHAUSTED:"):
            slot["budget_exhausted"] += 1
        for tier, n in (rec.get("tier_distribution") or {}).items():
            slot["tier_distribution"][tier] = slot["tier_distribution"].get(tier, 0) + int(n)
        latency = _safe_float(rec.get("latency_ms"))
        if latency is not None:
            slot["latency_ms_total"] += latency
    return by_endpoint


def _gate_summary(layer5: list[dict]) -> dict[str, Any]:
    triggers = 0
    soft_escapes = 0
    decisions: dict[str, int] = {}
    for rec in layer5:
        d = rec.get("decision", "unknown")
        decisions[d] = decisions.get(d, 0) + 1
        if d == "block":
            triggers += 1
        if d == "soft_escape":
            soft_escapes += 1
    return {
        "triggers": triggers,
        "soft_escapes": soft_escapes,
        "decisions": decisions,
    }


def _focus_set(layer1: list[dict]) -> tuple[set[str], set[str]]:
    """Last-write-wins: take the most recent layer1 record."""
    if not layer1:
        return set(), set()
    last = layer1[-1]
    files = {entry.get("file", "") for entry in (last.get("files") or [])}
    func_keys: set[str] = set()
    for entry in last.get("functions") or []:
        f = entry.get("file", "")
        n = entry.get("function", "")
        if f and n:
            func_keys.add(f"{f}::{n}")
    files.discard("")
    return files, func_keys


def _read_oh_trajectory(path: str) -> list[dict]:
    """OH writes one JSON event per line into output.jsonl. Best-effort."""
    return _read_jsonl(path)


def _aggregate_tokens_and_cost(llm_dir: str | None) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_hit_input_tokens": None,
        "cache_miss_input_tokens": None,
        "cost_usd": None,
        "_source": None,
    }
    if not llm_dir or not os.path.isdir(llm_dir):
        return fields
    in_tot = out_tot = hit_tot = miss_tot = 0
    cost_tot = 0.0
    saw_any = False
    for name in os.listdir(llm_dir):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(llm_dir, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        usage = data.get("usage") or {}
        in_tok = _safe_int(usage.get("prompt_tokens")) or 0
        out_tok = _safe_int(usage.get("completion_tokens")) or 0
        details = usage.get("prompt_tokens_details") or {}
        hit = _safe_int(details.get("cached_tokens")) or 0
        miss = max(0, in_tok - hit)
        cost = _safe_float(data.get("cost") or data.get("cost_usd")) or 0.0
        in_tot += in_tok
        out_tot += out_tok
        hit_tot += hit
        miss_tot += miss
        cost_tot += cost
        saw_any = True
    if saw_any:
        fields.update({
            "input_tokens": in_tot,
            "output_tokens": out_tot,
            "cache_hit_input_tokens": hit_tot,
            "cache_miss_input_tokens": miss_tot,
            "cost_usd": round(cost_tot, 6),
            "_source": "llm_completions",
        })
    return fields


def _resolve_outcome(results_json: str | None, instance_id: str) -> dict[str, Any]:
    out = {"completed": None, "patch_generated": None, "eval_resolved": None}
    if not results_json or not os.path.exists(results_json):
        return out
    try:
        with open(results_json, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return out
    # SWE-Bench Live results.json is a dict keyed by instance_id with bool /
    # status values; or a list of {instance_id, resolved}. Be tolerant.
    if isinstance(data, dict) and instance_id in data:
        v = data[instance_id]
        if isinstance(v, bool):
            out["eval_resolved"] = v
        elif isinstance(v, dict):
            out["eval_resolved"] = bool(v.get("resolved"))
            out["patch_generated"] = bool(v.get("patch") or v.get("model_patch"))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("instance_id") == instance_id:
                out["eval_resolved"] = bool(item.get("resolved"))
                out["patch_generated"] = bool(item.get("patch") or item.get("model_patch"))
                break
    if out["eval_resolved"] is not None:
        out["completed"] = True
    return out


def aggregate(args: argparse.Namespace) -> dict[str, Any]:
    tdir = args.telemetry_dir
    instance_id = os.path.basename(tdir.rstrip("/")).replace("gt_telemetry_", "") or "unknown"

    layer1 = _read_jsonl(os.path.join(tdir, "layer1_localization.jsonl"))
    layer2 = _read_jsonl(os.path.join(tdir, "layer2_brief.jsonl"))
    layer3 = _read_jsonl(os.path.join(tdir, "layer3_hook.jsonl"))
    layer4 = _read_jsonl(os.path.join(tdir, "layer4_endpoints.jsonl"))
    layer5 = _read_jsonl(os.path.join(tdir, "layer5_gate.jsonl"))

    focus_files, focus_funcs = _focus_set(layer1)
    endpoints = _endpoint_breakdown(layer4)
    gate = _gate_summary(layer5)

    tokens_cost = _aggregate_tokens_and_cost(args.llm_completions_dir)
    outcome = _resolve_outcome(args.results_json, instance_id)
    trajectory = _read_oh_trajectory(args.oh_output_jsonl) if args.oh_output_jsonl else []

    summary: dict[str, Any] = {
        "instance_id": instance_id,
        "arm": args.arm,
        "turns_used": len(trajectory) or None,
        **outcome,
        "tokens_and_cost": tokens_cost,
        "gt_layers": {
            "layer1_localization_records": _layer_counts(layer1),
            "layer2_brief_records": _layer_counts(layer2),
            "layer3_hook_fires": _layer_counts(layer3),
            "layer4_endpoint_calls": sum(s["calls"] for s in endpoints.values()),
            "layer4_by_endpoint": endpoints,
            "layer5_gate_records": _layer_counts(layer5),
            "layer5_summary": gate,
        },
        "focus_set": {
            "files": sorted(focus_files),
            "functions": sorted(focus_funcs),
        },
        "behavioral_metrics_note": (
            "focus_file_edit_rate / focus_function_edit_rate / hook_engagement_rate / "
            "endpoint_engagement_rate / gate_re_edit_rate require trajectory_full.jsonl "
            "cross-referenced with layer3/layer4. Compute downstream when trajectory is "
            "available."
        ),
    }
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--telemetry-dir", required=True, help="/tmp/gt_telemetry_<instance_id>/")
    p.add_argument("--output-dir", required=True, help="Where to write per_task_summary.json")
    p.add_argument("--arm", default="gt_v105")
    p.add_argument("--oh-output-jsonl", default=None)
    p.add_argument("--llm-completions-dir", default=None)
    p.add_argument("--results-json", default=None)
    args = p.parse_args()

    if not os.path.isdir(args.telemetry_dir):
        print(f"telemetry_dir not found: {args.telemetry_dir}", file=sys.stderr)
        return 1
    os.makedirs(args.output_dir, exist_ok=True)

    summary = aggregate(args)
    out_path = os.path.join(args.output_dir, "per_task_summary.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True, default=str)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
