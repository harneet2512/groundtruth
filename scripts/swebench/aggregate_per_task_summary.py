#!/usr/bin/env python3
"""Aggregate per-task summary across the 5 GT layers for a paired run.

Joins, per instance_id, per arm:
  - output.jsonl              -> patch present, error, model_name, history length
  - llm_completions/<id>/*.json -> per-turn tokens, cache hits, cost
  - /tmp/gt_hook_log.jsonl    -> hook fires, evidence families
  - mcp daemon text log       -> MCP call count + tool distribution (Arm A only)
  - v7_telemetry.jsonl (brief)-> brief sections + token counts (Arm A only)

Output: <run-root>/per_task_summary.json keyed by instance_id with one
record per arm. Designed to be run AFTER an arm completes; safe to re-run.

Usage:
    python scripts/swebench/aggregate_per_task_summary.py \
        --run-dir <paired_run_root>

Vertex DeepSeek V3.2 token rates (per million tokens):
  input: $0.27 (paid stable)
  output: $1.10
OpenRouter DeepSeek V3.2 token rates:
  input: $0.252
  output: $0.378
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

# Token pricing per million ($USD), keyed by model substring match.
PRICE_TABLE = {
    "vertex_ai/deepseek": {"in": 0.27, "out": 1.10},
    "openrouter/deepseek/deepseek-v3.2": {"in": 0.252, "out": 0.378},
    "deepseek-v3.2": {"in": 0.27, "out": 1.10},
    "vertex_ai/glm": {"in": 0.38, "out": 1.74},
    "vertex_ai/qwen": {"in": 0.27, "out": 1.10},
}

GT_HOOK_LOG = "/tmp/gt_hook_log.jsonl"
MCP_DAEMON_LOG_DEFAULT = "/tmp/gtfull_v2_mcp_server.log"


def model_to_rates(model: str) -> dict[str, float]:
    m = (model or "").lower()
    for key, rates in PRICE_TABLE.items():
        if key in m:
            return rates
    return {"in": 0.30, "out": 1.00}  # safe-ish fallback


def find_oh_run_dirs(arm_dir: Path) -> list[Path]:
    """Find OpenHands per-run dirs under an arm dir.
    Pattern: arm_dir/v1r_<arm>_<ts>/SWE-bench-Live/SWE-bench-Live/CodeActAgent/<combo>/
    """
    out = []
    for v1r in arm_dir.glob("v1r_*"):
        if not v1r.is_dir():
            continue
        for combo in v1r.glob("SWE-bench-Live/SWE-bench-Live/CodeActAgent/*"):
            if combo.is_dir():
                out.append(combo)
    return out


def load_output_jsonl(combo_dir: Path) -> dict[str, dict[str, Any]]:
    """Map instance_id -> output record."""
    f = combo_dir / "output.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        iid = rec.get("instance_id")
        if iid:
            out[iid] = rec
    return out


def iter_completions(combo_dir: Path, instance_id: str) -> Iterator[dict[str, Any]]:
    d = combo_dir / "llm_completions" / instance_id
    if not d.is_dir():
        return
    for f in sorted(d.glob("*.json")):
        try:
            yield json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue


def aggregate_completions(
    combo_dir: Path, instance_id: str, model: str
) -> dict[str, Any]:
    rates = model_to_rates(model)
    n_calls = 0
    in_tot = 0
    out_tot = 0
    cached_tot = 0
    cost_field_sum = 0.0
    cost_field_seen = False
    for d in iter_completions(combo_dir, instance_id):
        n_calls += 1
        usage = (d.get("response") or {}).get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        in_tot += pt
        out_tot += ct
        cached_tot += cached
        # OR exposes top-level cost; Vertex generally does not.
        c = d.get("cost")
        if c is not None:
            try:
                cost_field_sum += float(c)
                cost_field_seen = True
            except (TypeError, ValueError):
                pass
    in_uncached = max(0, in_tot - cached_tot)
    cost_token_math = (
        in_uncached * rates["in"] / 1_000_000
        + out_tot * rates["out"] / 1_000_000
    )
    return {
        "calls": n_calls,
        "input_tokens": in_tot,
        "output_tokens": out_tot,
        "cached_input_tokens": cached_tot,
        "uncached_input_tokens": in_uncached,
        "cache_hit_ratio": (cached_tot / in_tot) if in_tot else 0.0,
        "cost_field_usd": round(cost_field_sum, 6) if cost_field_seen else None,
        "cost_token_math_usd": round(cost_token_math, 6),
        "model": model,
        "rates_used": rates,
    }


def hook_summary_for_instance(
    instance_id: str, hook_log: Path = Path(GT_HOOK_LOG)
) -> dict[str, Any]:
    """Filter /tmp/gt_hook_log.jsonl for this task's events."""
    if not hook_log.exists():
        return {"hook_log_present": False, "fires": 0}
    fires = 0
    by_kind: dict[str, int] = {}
    families: dict[str, int] = {}
    for line in hook_log.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if instance_id and r.get("instance_id") not in (instance_id, None, ""):
            # gt_hook_log entries don't always carry instance_id; we accept None
            # and let the run-level filter (presence under task time-window) be
            # done elsewhere if needed.
            if r.get("instance_id"):
                continue
        fires += 1
        kind = r.get("hook") or r.get("kind") or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        ev = r.get("evidence") or {}
        for fam, payload in (ev.items() if isinstance(ev, dict) else []):
            if isinstance(payload, dict) and payload.get("ran"):
                families[fam] = families.get(fam, 0) + 1
    return {
        "hook_log_present": True,
        "fires": fires,
        "by_hook_kind": by_kind,
        "by_evidence_family": families,
    }


_RE_MCP_TOOL = re.compile(r"\btool[ =]([a-zA-Z_][a-zA-Z0-9_]*)\b|\btool_name=([a-zA-Z_][a-zA-Z0-9_]*)")
_RE_GT_TOOL = re.compile(r"\b(groundtruth_[a-zA-Z_]+|gt_[a-zA-Z_]+)\b")


def mcp_summary(daemon_log: Path) -> dict[str, Any]:
    """Best-effort parse of MCP daemon text log for tool dispatches.
    Counts substring hits for known GT tool names and any tool=NAME pattern.
    """
    if not daemon_log.exists():
        return {"daemon_log_present": False, "calls": 0}
    by_tool: dict[str, int] = {}
    text = daemon_log.read_text(encoding="utf-8", errors="replace")
    for m in _RE_GT_TOOL.finditer(text):
        t = m.group(1)
        # Skip GT_ env-var-style tokens
        if t.startswith("GT_"):
            continue
        by_tool[t] = by_tool.get(t, 0) + 1
    return {
        "daemon_log_present": True,
        "calls": sum(by_tool.values()),
        "by_tool": by_tool,
    }


def find_v7_telemetry(combo_dir: Path) -> Path | None:
    for c in combo_dir.rglob("v7_telemetry.jsonl"):
        return c
    return None


def brief_summary_for_instance(combo_dir: Path, instance_id: str) -> dict[str, Any]:
    f = find_v7_telemetry(combo_dir)
    if f is None or not f.exists():
        return {"telemetry_present": False}
    section_flags: dict[str, int] = {}
    layer_counts: dict[str, int] = {}
    chars = 0
    n_records = 0
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if instance_id and r.get("instance_id") not in (instance_id, None, ""):
            continue
        n_records += 1
        chars = max(chars, int(r.get("brief_chars") or 0))
        for k, v in (r.get("v7_sections") or {}).items():
            if v:
                section_flags[k] = section_flags.get(k, 0) + 1
        for k, v in (r.get("v7_layer_counts") or {}).items():
            try:
                layer_counts[k] = layer_counts.get(k, 0) + int(v)
            except (TypeError, ValueError):
                continue
    return {
        "telemetry_present": True,
        "n_records": n_records,
        "brief_chars_max": chars,
        "section_flags": section_flags,
        "layer_counts": layer_counts,
    }


def per_task_record(
    instance_id: str,
    arm: str,
    combo_dir: Path,
    output_rec: dict[str, Any] | None,
) -> dict[str, Any]:
    model = (((output_rec or {}).get("metadata") or {}).get("model_name")) \
        or (output_rec or {}).get("model_name") \
        or os.environ.get("V1R_LLM_MODEL", "qwen3-coder-480b-a35b-instruct-maas")
    completions = aggregate_completions(combo_dir, instance_id, model)
    hook = hook_summary_for_instance(instance_id)
    mcp_log = Path(os.environ.get("GT_MCP_DAEMON_LOG", MCP_DAEMON_LOG_DEFAULT))
    mcp = mcp_summary(mcp_log) if arm == "A" else {"daemon_log_present": False, "calls": 0}
    brief = brief_summary_for_instance(combo_dir, instance_id) if arm == "A" else {"telemetry_present": False}

    git_patch = ((output_rec or {}).get("test_result") or {}).get("git_patch") or ""
    err = (output_rec or {}).get("error")
    history = (output_rec or {}).get("history") or []
    n_actions = sum(1 for h in history if isinstance(h, dict) and h.get("action"))

    return {
        "instance_id": instance_id,
        "arm": arm,
        "model": model,
        "patch_present": bool(git_patch),
        "patch_chars": len(git_patch),
        "error": err,
        "n_history_events": len(history),
        "n_actions": n_actions,
        "completions": completions,
        "hooks": hook,
        "mcp": mcp,
        "brief": brief,
        "combo_dir": str(combo_dir),
    }


def aggregate_arm(arm_dir: Path, arm: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for combo in find_oh_run_dirs(arm_dir):
        outputs = load_output_jsonl(combo)
        for iid, rec in outputs.items():
            if iid in out:
                # Multiple combo dirs producing the same id: keep the larger patch.
                existing_chars = out[iid].get("patch_chars", 0)
                new_chars = len(((rec.get("test_result") or {}).get("git_patch") or ""))
                if new_chars <= existing_chars:
                    continue
            out[iid] = per_task_record(iid, arm, combo, rec)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Aggregate per-task summary for a paired run")
    p.add_argument("--run-dir", required=True, type=Path,
                   help="Paired run root (contains arm_A/ and/or arm_B/)")
    p.add_argument("--out", type=Path, default=None,
                   help="Output path (default: <run-dir>/per_task_summary.json)")
    args = p.parse_args(argv)

    run_dir: Path = args.run_dir
    if not run_dir.is_dir():
        print(f"ERROR: run-dir does not exist: {run_dir}", file=sys.stderr)
        return 2

    summary: dict[str, Any] = {
        "run_dir": str(run_dir.resolve()),
        "arms": {},
    }
    for arm in ("A", "B"):
        arm_dir = run_dir / f"arm_{arm}"
        if not arm_dir.is_dir():
            continue
        summary["arms"][arm] = aggregate_arm(arm_dir, arm)

    # Build paired view + roll-ups.
    ids = sorted(set().union(*(d.keys() for d in summary["arms"].values())))
    paired = []
    totals = {"A": {"cost_token_math": 0.0, "cost_field": 0.0, "calls": 0, "patches": 0},
              "B": {"cost_token_math": 0.0, "cost_field": 0.0, "calls": 0, "patches": 0}}
    for iid in ids:
        row: dict[str, Any] = {"instance_id": iid}
        for arm in ("A", "B"):
            r = summary["arms"].get(arm, {}).get(iid)
            if r is None:
                row[arm] = None
                continue
            row[arm] = {
                "patch_present": r["patch_present"],
                "patch_chars": r["patch_chars"],
                "n_actions": r["n_actions"],
                "cost_token_math_usd": r["completions"]["cost_token_math_usd"],
                "cost_field_usd": r["completions"]["cost_field_usd"],
                "calls": r["completions"]["calls"],
                "cache_hit_ratio": r["completions"]["cache_hit_ratio"],
                "input_tokens": r["completions"]["input_tokens"],
                "output_tokens": r["completions"]["output_tokens"],
                "mcp_calls": r["mcp"].get("calls", 0),
                "hook_fires": r["hooks"].get("fires", 0),
                "brief_chars": r["brief"].get("brief_chars_max", 0),
            }
            totals[arm]["cost_token_math"] += r["completions"]["cost_token_math_usd"] or 0.0
            if r["completions"]["cost_field_usd"] is not None:
                totals[arm]["cost_field"] += r["completions"]["cost_field_usd"]
            totals[arm]["calls"] += r["completions"]["calls"]
            totals[arm]["patches"] += int(r["patch_present"])
        paired.append(row)

    summary["paired"] = paired
    summary["totals"] = totals

    out_path = args.out or (run_dir / "per_task_summary.json")
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"# wrote {out_path}")

    # Print compact summary.
    print(f"# instances: {len(ids)}")
    for arm in ("A", "B"):
        t = totals[arm]
        print(f"  arm_{arm}: patches={t['patches']}/{len(ids)} "
              f"calls={t['calls']} "
              f"cost_token_math=${t['cost_token_math']:.4f} "
              f"cost_field=${t['cost_field']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
