"""Aggregate per-task and per-run metrics for the 20-task baseline calibration.

Inputs (per-shard):
  <outdir>/shard<ID>/trajectories/<iid>/<iid>.traj.json   or variants
  <outdir>/shard<ID>/preds.json                           (merged)
  <outdir>/shard<ID>/run.log                              (sweagent stdout/stderr)
  <outdir>/shard<ID>/eval_reports/<iid>.json              (swebench harness per-instance)
  <outdir>/shard<ID>/shard_reconcile.json                 (from cal_shard_reconcile.py)

Output:
  <outdir>/cal_metrics.jsonl   -- one JSON line per task
  <outdir>/cal_summary.md      -- rollups and 300-task projections

Gemini 3.1 Pro Preview pricing is read from <outdir>/preflight/pricing.json.
If that file does not exist, a fallback table is used AND a warning is printed.
The fallback matches the tentative list prices in the plan's section N; always
prefer the preflight-verified file before trusting any cost numbers.

Usage:
  python scripts/cal_metrics.py --shard A   --outdir /tmp/cal_gemini31pro_XXXX
  python scripts/cal_metrics.py --shard B   --outdir /tmp/cal_gemini31pro_XXXX
  python scripts/cal_metrics.py --shard ALL --outdir /tmp/cal_gemini31pro_XXXX
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics as stats
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_FALLBACK_PRICING = {
    "input_tier_threshold_tokens": 200_000,
    "input_per_1m_below": 2.00,
    "input_per_1m_above": 4.00,
    "output_tier_threshold_tokens": 200_000,
    "output_per_1m_below": 12.00,
    "output_per_1m_above": 18.00,
    "cached_input_per_1m_below": 0.50,
    "cached_input_per_1m_above": 1.00,
    "source": "FALLBACK (plan section N tentative list prices)",
}

_RATE_LIMIT_RE = re.compile(
    r"(?i)(RateLimit|429\b|RESOURCE_EXHAUSTED|quota exceeded|ResourceExhausted)"
)
_ERROR_403_RE = re.compile(
    r"(?i)(\b403\b|PermissionDenied|aiplatform\.endpoints\.predict denied)"
)
_TOOL_HISTORY_FM1_RE = re.compile(
    r"(?i)tool config, tools and system instruction should not be set "
    r"in the request when using cached content"
)
_TOOL_HISTORY_FM2_RE = re.compile(
    r"(?i)missing corresponding tool call for tool response message"
)
_FILE_READ_RE = re.compile(
    r"(?:^|[;&|(\s])(?:cat|head|tail|less|more)\s+([^\s;|&><]+)"
)
_SED_READ_RE = re.compile(r"sed\s+-n\s+['\"]?[\d,p]+['\"]?\s+([^\s;|&><]+)")


def _load_pricing(outdir: Path) -> dict:
    p = outdir / "preflight" / "pricing.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            sys.stderr.write(f"WARN: pricing.json exists but is not valid JSON ({e}); using fallback\n")
    sys.stderr.write(
        "WARN: preflight/pricing.json not found; using fallback Gemini 3.1 Pro Preview list prices.\n"
        "      Verify prices from Vertex pricing page and write preflight/pricing.json before trusting costs.\n"
    )
    return dict(_FALLBACK_PRICING)


def _tiered_cost(tokens: int, threshold: int, below_per_m: float, above_per_m: float) -> float:
    if tokens <= 0:
        return 0.0
    if tokens <= threshold:
        return tokens * below_per_m / 1_000_000.0
    return (
        threshold * below_per_m / 1_000_000.0
        + (tokens - threshold) * above_per_m / 1_000_000.0
    )


def _compute_cost(
    input_tokens: int, output_tokens: int, cached_input_tokens: int, pricing: dict
) -> float:
    # Uncached input is (input - cached_input). Cached input is priced at cache rate.
    uncached_in = max(0, input_tokens - cached_input_tokens)
    return (
        _tiered_cost(
            uncached_in,
            pricing["input_tier_threshold_tokens"],
            pricing["input_per_1m_below"],
            pricing["input_per_1m_above"],
        )
        + _tiered_cost(
            cached_input_tokens,
            pricing["input_tier_threshold_tokens"],
            pricing["cached_input_per_1m_below"],
            pricing["cached_input_per_1m_above"],
        )
        + _tiered_cost(
            output_tokens,
            pricing["output_tier_threshold_tokens"],
            pricing["output_per_1m_below"],
            pricing["output_per_1m_above"],
        )
    )


def _input_band(peak_input: int) -> str:
    if peak_input <= 200_000:
        return "<=200k"
    if peak_input <= 400_000:
        return "200k-400k"
    if peak_input <= 800_000:
        return "400k-800k"
    return ">800k"


def _iter_preds(shard_dir: Path) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for p in shard_dir.rglob("preds.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def _find_trajectory(shard_dir: Path, iid: str) -> Path | None:
    for ext in (f"{iid}.traj.json", f"{iid}.traj", f"{iid}.json"):
        for hit in shard_dir.rglob(ext):
            return hit
    return None


def _count_repeated_reads(commands: list[str], window: int = 10) -> int:
    reads_per_path: Counter[str] = Counter()
    repeated = 0
    recent: list[str] = []
    for cmd in commands:
        hits = _FILE_READ_RE.findall(cmd) + _SED_READ_RE.findall(cmd)
        for path in hits:
            reads_per_path[path] += 1
            if reads_per_path[path] > 1:
                repeated += 1
            recent.append(path)
            if len(recent) > window:
                evicted = recent.pop(0)
                reads_per_path[evicted] -= 1
                if reads_per_path[evicted] <= 0:
                    del reads_per_path[evicted]
    return repeated


def _extract_task_metrics(
    iid: str,
    traj_path: Path | None,
    pred_record: dict | None,
    eval_report: dict | None,
    run_log: str,
    pricing: dict,
) -> dict:
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    api_calls = 0
    steps = 0
    history_len_final = 0
    peak_input = 0
    commands: list[str] = []
    start_ts = None
    end_ts = None

    if traj_path is not None:
        try:
            traj = json.loads(traj_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            traj = {}

        info = traj.get("info") or {}
        model_stats = info.get("model_stats") or traj.get("model_stats") or {}
        input_tokens = int(model_stats.get("tokens_sent") or model_stats.get("input_tokens") or 0)
        output_tokens = int(
            model_stats.get("tokens_received") or model_stats.get("output_tokens") or 0
        )
        cached_input_tokens = int(
            model_stats.get("cache_read_input_tokens")
            or model_stats.get("cached_tokens")
            or 0
        )
        api_calls = int(model_stats.get("api_calls") or 0)

        history = traj.get("history") or []
        history_len_final = len(history)
        for msg in history:
            usage = (msg.get("usage") if isinstance(msg, dict) else None) or {}
            pin = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            peak_input = max(peak_input, pin)

        trajectory_steps = traj.get("trajectory") or traj.get("steps") or []
        steps = len(trajectory_steps)
        for step in trajectory_steps:
            if not isinstance(step, dict):
                continue
            action = step.get("action") or step.get("command") or ""
            if isinstance(action, str) and action:
                commands.append(action)

        start_ts = info.get("start_time") or traj.get("start_time")
        end_ts = info.get("end_time") or traj.get("end_time")

    wall_seconds = None
    if start_ts and end_ts:
        try:
            import datetime as _dt

            def _parse(t: Any) -> float:
                if isinstance(t, (int, float)):
                    return float(t)
                return _dt.datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()

            wall_seconds = max(0.0, _parse(end_ts) - _parse(start_ts))
        except Exception:  # noqa: BLE001
            wall_seconds = None

    retry_count = len(_RATE_LIMIT_RE.findall(run_log))
    error_403_count = len(_ERROR_403_RE.findall(run_log))
    fm1_hits = len(_TOOL_HISTORY_FM1_RE.findall(run_log))
    fm2_hits = len(_TOOL_HISTORY_FM2_RE.findall(run_log))
    tool_history_corruption_count = fm1_hits + fm2_hits
    repeated_reads = _count_repeated_reads(commands)

    patch = ""
    if pred_record is not None:
        patch = pred_record.get("model_patch") or pred_record.get("patch") or ""
    patch = patch or ""

    if eval_report is not None:
        resolved = bool(eval_report.get("resolved") or eval_report.get("is_resolved"))
        artifact_class = "resolved" if resolved else "not_resolved"
        eval_status = "resolved" if resolved else "not_resolved"
    elif pred_record is None:
        artifact_class = "no_prediction"
        eval_status = "no_prediction"
    elif not patch.strip():
        artifact_class = "empty_submission"
        eval_status = "eval_pending"
    elif "@@" not in patch or "--- " not in patch:
        artifact_class = "malformed_submission"
        eval_status = "eval_pending"
    else:
        artifact_class = "clean_submission"
        eval_status = "eval_pending"

    cost_estimate = _compute_cost(input_tokens, output_tokens, cached_input_tokens, pricing)

    return {
        "instance_id": iid,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "wall_seconds": wall_seconds,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "peak_input_tokens": peak_input or input_tokens,
        "api_calls": api_calls,
        "steps": steps,
        "history_len_final": history_len_final,
        "repeated_file_reads": repeated_reads,
        "retry_count": retry_count,
        "error_403_count": error_403_count,
        "tool_history_corruption_count": tool_history_corruption_count,
        "tool_history_fm1_hits": fm1_hits,
        "tool_history_fm2_hits": fm2_hits,
        "input_band": _input_band(peak_input or input_tokens),
        "cost_estimate": round(cost_estimate, 6),
        "artifact_class": artifact_class,
        "eval_status": eval_status,
    }


def _process_shard(outdir: Path, shard_id: str, pricing: dict) -> list[dict]:
    shard_dir = outdir / f"shard{shard_id}"
    if not shard_dir.exists():
        sys.stderr.write(f"WARN: shard dir missing: {shard_dir}\n")
        return []
    manifest_path = Path("benchmarks/swebench/cal20_live_lite.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    all_ids = manifest["selected"]
    if shard_id == "A":
        expected = all_ids[:10]
    elif shard_id == "B":
        expected = all_ids[10:20]
    else:
        expected = all_ids

    preds = _iter_preds(shard_dir)
    run_log_path = shard_dir / "run.log"
    run_log = run_log_path.read_text(encoding="utf-8", errors="replace") if run_log_path.exists() else ""

    rows: list[dict] = []
    for iid in expected:
        traj = _find_trajectory(shard_dir, iid)
        eval_path = shard_dir / "eval_reports" / f"{iid}.json"
        eval_report = None
        if eval_path.exists():
            try:
                eval_report = json.loads(eval_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                eval_report = None
        row = _extract_task_metrics(iid, traj, preds.get(iid), eval_report, run_log, pricing)
        row["shard_id"] = shard_id
        rows.append(row)
    return rows


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


def _safe_median(values: list[float]) -> float | None:
    return stats.median(values) if values else None


def _safe_mean(values: list[float]) -> float | None:
    return stats.fmean(values) if values else None


def _rollups(rows: list[dict]) -> dict:
    def col(key: str) -> list[float]:
        return [float(r[key]) for r in rows if isinstance(r.get(key), (int, float))]

    walls = col("wall_seconds")
    in_toks = col("input_tokens")
    out_toks = col("output_tokens")
    costs = col("cost_estimate")

    bands = Counter(r["input_band"] for r in rows)
    artifact_counts = Counter(r["artifact_class"] for r in rows)
    eval_counts = Counter(r["eval_status"] for r in rows)

    reread_explosion = [r for r in rows if r["repeated_file_reads"] >= 10]
    empty_or_malformed = [
        r for r in rows if r["artifact_class"] in ("empty_submission", "malformed_submission")
    ]
    crosses_200k = sum(1 for r in rows if r["input_band"] != "<=200k")
    crosses_400k = sum(1 for r in rows if r["input_band"] in ("400k-800k", ">800k"))
    crosses_800k = sum(1 for r in rows if r["input_band"] == ">800k")

    projections = {}
    if costs:
        projections["proj_300_cost_low_usd"] = round(
            (_safe_median(costs) or 0.0) * 300, 2
        )
        projections["proj_300_cost_high_usd"] = round((_pct(costs, 0.75) or 0.0) * 300, 2)
    if walls:
        # Default workers=4 for calibration. Runtime assumes tasks dispatched round-robin.
        p75 = _pct(walls, 0.75) or 0.0
        median = _safe_median(walls) or 0.0
        for w in (4, 6):
            projections[f"proj_300_runtime_w{w}_low_s"] = int(math.ceil(300 / w) * median)
            projections[f"proj_300_runtime_w{w}_high_s"] = int(math.ceil(300 / w) * p75)

    return {
        "tasks_total": len(rows),
        "wall_seconds": {
            "mean": _safe_mean(walls),
            "median": _safe_median(walls),
            "p95": _pct(walls, 0.95),
        },
        "input_tokens": {
            "mean": _safe_mean(in_toks),
            "median": _safe_median(in_toks),
            "p95": _pct(in_toks, 0.95),
        },
        "output_tokens": {
            "mean": _safe_mean(out_toks),
            "median": _safe_median(out_toks),
            "p95": _pct(out_toks, 0.95),
        },
        "cost_per_task_usd": {
            "mean": _safe_mean(costs),
            "median": _safe_median(costs),
            "p75": _pct(costs, 0.75),
            "p95": _pct(costs, 0.95),
            "total": round(sum(costs), 4) if costs else 0.0,
        },
        "input_band_distribution": dict(bands),
        "tasks_crossing_200k_input": crosses_200k,
        "tasks_crossing_400k_input": crosses_400k,
        "tasks_crossing_800k_input": crosses_800k,
        "rate_limit_event_total": sum(r["retry_count"] for r in rows),
        "error_403_total": sum(r.get("error_403_count", 0) for r in rows),
        "tool_history_corruption_total": sum(
            r.get("tool_history_corruption_count", 0) for r in rows
        ),
        "tool_history_fm1_total": sum(r.get("tool_history_fm1_hits", 0) for r in rows),
        "tool_history_fm2_total": sum(r.get("tool_history_fm2_hits", 0) for r in rows),
        "tasks_with_tool_history_corruption": sum(
            1 for r in rows if r.get("tool_history_corruption_count", 0) > 0
        ),
        "tasks_with_repeated_read_explosion": len(reread_explosion),
        "tasks_with_empty_or_malformed_artifact": len(empty_or_malformed),
        "artifact_class_distribution": dict(artifact_counts),
        "eval_status_distribution": dict(eval_counts),
        "projections": projections,
    }


def _format_summary(rollups: dict, rows: list[dict], pricing: dict) -> str:
    def _fmt(x: Any) -> str:
        if x is None:
            return "n/a"
        if isinstance(x, float):
            return f"{x:.2f}"
        return str(x)

    lines = ["# Calibration Summary", ""]
    lines.append(f"**Tasks total:** {rollups['tasks_total']}")
    lines.append("")
    lines.append("## Wall Time (seconds)")
    w = rollups["wall_seconds"]
    lines.append(f"- mean: {_fmt(w['mean'])}, median: {_fmt(w['median'])}, p95: {_fmt(w['p95'])}")
    lines.append("")
    lines.append("## Tokens")
    it = rollups["input_tokens"]
    ot = rollups["output_tokens"]
    lines.append(f"- input  mean: {_fmt(it['mean'])}, median: {_fmt(it['median'])}, p95: {_fmt(it['p95'])}")
    lines.append(f"- output mean: {_fmt(ot['mean'])}, median: {_fmt(ot['median'])}, p95: {_fmt(ot['p95'])}")
    lines.append("")
    lines.append("## Cost Per Task (USD)")
    c = rollups["cost_per_task_usd"]
    lines.append(
        f"- mean: {_fmt(c['mean'])}, median: {_fmt(c['median'])}, p75: {_fmt(c['p75'])}, p95: {_fmt(c['p95'])}, total: {_fmt(c['total'])}"
    )
    lines.append("")
    lines.append("## Input Band Distribution")
    for band, count in sorted(rollups["input_band_distribution"].items()):
        lines.append(f"- {band}: {count}")
    lines.append("")
    lines.append(f"- tasks crossing 200k: {rollups['tasks_crossing_200k_input']}")
    lines.append(f"- tasks crossing 400k: {rollups['tasks_crossing_400k_input']}")
    lines.append(f"- tasks crossing 800k: {rollups['tasks_crossing_800k_input']}")
    lines.append("")
    lines.append("## Health")
    lines.append(f"- rate-limit events total: {rollups['rate_limit_event_total']}")
    lines.append(
        f"- 403 / PermissionDenied events total: {rollups.get('error_403_total', 0)}"
    )
    lines.append(
        f"- tool_history corruption total (FM1+FM2): {rollups.get('tool_history_corruption_total', 0)}"
    )
    lines.append(
        f"  - FM1 (cache_control + tools collision): {rollups.get('tool_history_fm1_total', 0)}"
    )
    lines.append(
        f"  - FM2 (orphan tool_call on retry):       {rollups.get('tool_history_fm2_total', 0)}"
    )
    lines.append(
        f"- tasks with tool_history corruption: {rollups.get('tasks_with_tool_history_corruption', 0)}"
    )
    lines.append(
        f"- tasks with repeated-read explosion (>=10): {rollups['tasks_with_repeated_read_explosion']}"
    )
    lines.append(
        f"- tasks with empty/malformed artifact: {rollups['tasks_with_empty_or_malformed_artifact']}"
    )
    lines.append("")
    lines.append("## Artifact Class Distribution")
    for k, v in sorted(rollups["artifact_class_distribution"].items()):
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Eval Status Distribution")
    for k, v in sorted(rollups["eval_status_distribution"].items()):
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 300-Task Projections")
    proj = rollups["projections"]
    if "proj_300_cost_low_usd" in proj:
        lines.append(
            f"- cost range: ${proj['proj_300_cost_low_usd']:.2f} (median) .. ${proj['proj_300_cost_high_usd']:.2f} (p75)"
        )
    for w_ in (4, 6):
        low = proj.get(f"proj_300_runtime_w{w_}_low_s")
        high = proj.get(f"proj_300_runtime_w{w_}_high_s")
        if low is not None:
            lines.append(
                f"- runtime @ workers={w_}: ~{low // 60} min (median) .. {high // 60} min (p75)"
            )
    lines.append("")
    lines.append(f"## Pricing Source")
    lines.append(f"- {pricing.get('source', 'preflight/pricing.json')}")
    lines.append("")
    lines.append("## Per-Task Table")
    lines.append(
        "| instance_id | shard | wall_s | in_tok | out_tok | band | cost_usd | retries | rereads | artifact | eval |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            "| {instance_id} | {shard_id} | {wall} | {i} | {o} | {b} | {cost:.3f} | {rt} | {rr} | {ac} | {es} |".format(
                instance_id=r["instance_id"],
                shard_id=r["shard_id"],
                wall=f"{r['wall_seconds']:.0f}" if r["wall_seconds"] is not None else "n/a",
                i=r["input_tokens"],
                o=r["output_tokens"],
                b=r["input_band"],
                cost=r["cost_estimate"],
                rt=r["retry_count"],
                rr=r["repeated_file_reads"],
                ac=r["artifact_class"],
                es=r["eval_status"],
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--shard", required=True, choices=["A", "B", "ALL"])
    p.add_argument("--outdir", required=True)
    args = p.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    pricing = _load_pricing(outdir)

    if args.shard == "ALL":
        rows = _process_shard(outdir, "A", pricing) + _process_shard(outdir, "B", pricing)
    else:
        rows = _process_shard(outdir, args.shard, pricing)

    jsonl_path = outdir / "cal_metrics.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    rollups = _rollups(rows)
    summary = _format_summary(rollups, rows, pricing)

    summary_path = outdir / "cal_summary.md"
    summary_path.write_text(summary, encoding="utf-8")

    rollup_path = outdir / "cal_rollups.json"
    rollup_path.write_text(json.dumps(rollups, indent=2, default=str) + "\n", encoding="utf-8")

    print(f"wrote {jsonl_path}")
    print(f"wrote {summary_path}")
    print(f"wrote {rollup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
