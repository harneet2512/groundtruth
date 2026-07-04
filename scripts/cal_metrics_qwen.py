"""Per-task and per-run metrics for the OpenHands + Vertex Qwen3-Coder baseline calibration.

This is a Qwen/OpenHands-specific cousin of ``scripts/cal_metrics.py`` (which is
wired for SWE-agent + Gemini). The shape of this module mirrors the original so
downstream dashboards stay consistent, but the substantive changes are:

1. **Pricing model.** Qwen3-Coder-480B-A35B MaaS is flat-rate (no tiers).
   Two anchor sets are supported: ``anchor`` ($0.22/$1.80 per 1M tokens, Portkey
   + 2 aggregators) and ``worstcase`` ($1.00/$4.00, cloudprice.net). Both are
   reported so the pricing disagreement flagged in plan section M is visible in
   every cost column.
2. **Error patterns.** Qwen3-Coder lives on Vertex via LiteLLM. The interesting
   failure classes are: Vertex HTTP 400 (malformed tool-call payloads), 4xx auth
   stalls, 5xx provider blips, native tool-calling schema rejections, and
   multi-turn tool-history corruption (cache_control + tools collision class,
   kept as a defensive check since it's documented on Gemini).
3. **Artifact layout.** OpenHands writes ``output.jsonl`` (one record per
   instance) and a trajectory dir per instance. Shards live under
   ``<outdir>/shard_A/`` and ``<outdir>/shard_B/`` (not ``shardA/shardB``).

Usage::

    python scripts/cal_metrics_qwen.py --shard A   --outdir $OUTDIR
    python scripts/cal_metrics_qwen.py --shard B   --outdir $OUTDIR
    python scripts/cal_metrics_qwen.py --shard ALL --outdir $OUTDIR
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

_PRICING_ANCHORS: dict[str, dict[str, float]] = {
    "anchor": {
        "input_per_1m": 0.22,
        "output_per_1m": 1.80,
        "cached_input_per_1m": 0.02,
    },
    "worstcase": {
        "input_per_1m": 1.00,
        "output_per_1m": 4.00,
        "cached_input_per_1m": 0.11,
    },
}

_VERTEX_400_RE = re.compile(r"(?i)(?:status\s*code\s*[:=]?\s*400|HTTP/1\.1 400|\bBadRequest\b)")
_AUTH_4XX_RE = re.compile(r"(?i)(?:\b401\b|\b403\b|PermissionDenied|Unauthorized)")
_SERVER_5XX_RE = re.compile(r"(?i)(?:\b500\b|\b502\b|\b503\b|\b504\b|Internal server error)")
_RATE_LIMIT_RE = re.compile(r"(?i)(?:RateLimit|\b429\b|RESOURCE_EXHAUSTED|ResourceExhausted)")
_FC_SCHEMA_RE = re.compile(
    r"(?i)(?:tool_calls.*invalid|function.*schema|arguments.*could not be parsed)"
)
_CACHE_COLLISION_RE = re.compile(
    r"(?i)tool config, tools and system instruction should not be set "
    r"in the request when using cached content"
)
_ORPHAN_TOOL_CALL_RE = re.compile(
    r"(?i)missing corresponding tool call for tool response message"
)

# Trajectory action types OpenHands emits. We classify tool usage so the Qwen
# smoke can report whether behavior stayed inside the submitter's expected mix.
_BASH_ACTIONS = {"CmdRunAction", "run", "bash", "cmd_run"}
_EDITOR_ACTIONS = {"FileEditAction", "FileWriteAction", "str_replace_editor", "edit"}
_BROWSER_ACTIONS = {"BrowseURLAction", "BrowseInteractiveAction", "browser"}


def _load_pricing(path: Path | None) -> dict:
    """Load pricing table. If not present, emit both anchor and worstcase."""
    if path and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "anchor" not in data:
                data = {"anchor": data, "worstcase": _PRICING_ANCHORS["worstcase"]}
            return data
        except json.JSONDecodeError as e:
            sys.stderr.write(f"WARN: pricing file not valid JSON ({e}); using defaults\n")
    return dict(_PRICING_ANCHORS)


def _flat_cost(tokens: int, per_1m: float) -> float:
    return 0.0 if tokens <= 0 else tokens * per_1m / 1_000_000.0


def _compute_costs(
    input_tokens: int, output_tokens: int, cached_input_tokens: int, pricing: dict
) -> dict[str, float]:
    costs: dict[str, float] = {}
    for label, rates in pricing.items():
        uncached = max(0, input_tokens - cached_input_tokens)
        total = (
            _flat_cost(uncached, rates["input_per_1m"])
            + _flat_cost(cached_input_tokens, rates.get("cached_input_per_1m", rates["input_per_1m"]))
            + _flat_cost(output_tokens, rates["output_per_1m"])
        )
        costs[f"cost_{label}_usd"] = round(total, 6)
    return costs


def _extract_patch(record: dict) -> str:
    for key in ("model_patch", "git_patch", "test_patch", "patch"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    tr = record.get("test_result")
    if isinstance(tr, dict):
        for key in ("model_patch", "git_patch", "test_patch", "patch"):
            v = tr.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def _load_output_records(shard_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    jsonl = shard_dir / "output.jsonl"
    if not jsonl.exists():
        return out
    for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        iid = record.get("instance_id") or record.get("id")
        if isinstance(iid, str):
            out[iid] = record
    return out


def _find_trajectory(shard_dir: Path, iid: str) -> Path | None:
    for ext in (
        f"{iid}/trajectory.json",
        f"{iid}.traj.json",
        f"trajectories/{iid}.json",
        f"trajectories/{iid}/trajectory.json",
    ):
        hit = shard_dir / ext
        if hit.exists():
            return hit
    for hit in shard_dir.rglob(f"{iid}*.json"):
        name = hit.name.lower()
        if "traj" in name:
            return hit
    return None


def _classify_action(action: Any) -> str | None:
    if isinstance(action, str):
        name = action
    elif isinstance(action, dict):
        name = action.get("action") or action.get("tool") or action.get("type") or ""
    else:
        return None
    name = name or ""
    if name in _BASH_ACTIONS or "bash" in name.lower() or "cmd" in name.lower():
        return "bash"
    if name in _EDITOR_ACTIONS or "edit" in name.lower() or "file" in name.lower():
        return "editor"
    if name in _BROWSER_ACTIONS or "browse" in name.lower():
        return "browser"
    return None


def _extract_task_metrics(
    iid: str,
    traj_path: Path | None,
    oh_record: dict | None,
    eval_report: dict | None,
    run_log: str,
    pricing: dict,
) -> dict:
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    api_calls = 0
    steps = 0
    tool_call_count = 0
    bash_count = editor_count = browser_count = 0
    start_ts = end_ts = None

    # Prefer trajectory metrics; fall back to ``metrics`` on the output.jsonl record.
    metrics: dict[str, Any] = {}
    if traj_path is not None:
        try:
            traj = json.loads(traj_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            traj = {}
        metrics = traj.get("metrics") or traj.get("llm_metrics") or {}
        steps_data = traj.get("history") or traj.get("trajectory") or traj.get("events") or []
        if isinstance(steps_data, list):
            steps = len(steps_data)
            for entry in steps_data:
                cls = _classify_action(entry)
                if cls == "bash":
                    bash_count += 1
                elif cls == "editor":
                    editor_count += 1
                elif cls == "browser":
                    browser_count += 1
                if isinstance(entry, dict) and entry.get("tool_calls"):
                    tc = entry["tool_calls"]
                    tool_call_count += len(tc) if isinstance(tc, list) else 1
        start_ts = traj.get("start_time") or (traj.get("info") or {}).get("start_time")
        end_ts = traj.get("end_time") or (traj.get("info") or {}).get("end_time")

    if not metrics and oh_record:
        metrics = oh_record.get("metrics") or oh_record.get("llm_metrics") or {}

    def _int(key_names: tuple[str, ...]) -> int:
        for k in key_names:
            v = metrics.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    input_tokens = _int(("accumulated_prompt_tokens", "input_tokens", "tokens_sent", "prompt_tokens"))
    output_tokens = _int(
        ("accumulated_completion_tokens", "output_tokens", "tokens_received", "completion_tokens")
    )
    cached_input_tokens = _int(("cache_read_input_tokens", "cached_tokens", "cached_prompt_tokens"))
    api_calls = _int(("accumulated_api_calls", "api_calls", "num_llm_calls"))

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
    vertex_400 = len(_VERTEX_400_RE.findall(run_log))
    auth_4xx = len(_AUTH_4XX_RE.findall(run_log))
    server_5xx = len(_SERVER_5XX_RE.findall(run_log))
    fc_schema = len(_FC_SCHEMA_RE.findall(run_log))
    cache_collision = len(_CACHE_COLLISION_RE.findall(run_log))
    orphan_tool = len(_ORPHAN_TOOL_CALL_RE.findall(run_log))

    patch = _extract_patch(oh_record) if oh_record else ""

    if eval_report is not None:
        resolved = bool(eval_report.get("resolved") or eval_report.get("is_resolved"))
        patch_class = "resolved" if resolved else "not_resolved"
        eval_status = "resolved" if resolved else "not_resolved"
    elif oh_record is None:
        patch_class = "missing_pred"
        eval_status = "missing_pred"
    elif not patch.strip():
        patch_class = "empty_patch"
        eval_status = "eval_pending"
    elif "@@" not in patch or "--- " not in patch:
        patch_class = "malformed_patch"
        eval_status = "eval_pending"
    elif traj_path is None:
        patch_class = "missing_traj"
        eval_status = "eval_pending"
    else:
        patch_class = "clean"
        eval_status = "eval_pending"

    costs = _compute_costs(input_tokens, output_tokens, cached_input_tokens, pricing)

    return {
        "instance_id": iid,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "wall_seconds": wall_seconds,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "api_calls": api_calls,
        "steps": steps,
        "tool_call_count": tool_call_count,
        "bash_call_count": bash_count,
        "editor_call_count": editor_count,
        "browser_call_count": browser_count,
        "retry_count": retry_count,
        "error_403_count": auth_4xx,
        "vertex_400_count": vertex_400,
        "server_5xx_count": server_5xx,
        "fc_schema_reject_count": fc_schema,
        "cache_collision_count": cache_collision,
        "orphan_tool_call_count": orphan_tool,
        "patch_class": patch_class,
        "eval_status": eval_status,
        **costs,
    }


def _process_shard(
    outdir: Path, shard_id: str, manifest_ids: list[str], pricing: dict
) -> list[dict]:
    shard_dir = outdir / f"shard_{shard_id}"
    if not shard_dir.exists():
        sys.stderr.write(f"WARN: shard dir missing: {shard_dir}\n")
        return []
    records = _load_output_records(shard_dir)
    run_log_path = shard_dir / "run_infer.log"
    run_log = (
        run_log_path.read_text(encoding="utf-8", errors="replace")
        if run_log_path.exists()
        else ""
    )
    if shard_id == "A":
        expected = manifest_ids[:10]
    elif shard_id == "B":
        expected = manifest_ids[10:20]
    else:
        expected = manifest_ids

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
        row = _extract_task_metrics(iid, traj, records.get(iid), eval_report, run_log, pricing)
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
    return xs[lo] if lo == hi else xs[lo] * (hi - k) + xs[hi] * (k - lo)


def _median(values: list[float]) -> float | None:
    return stats.median(values) if values else None


def _mean(values: list[float]) -> float | None:
    return stats.fmean(values) if values else None


def _rollups(rows: list[dict], pricing: dict) -> dict:
    def col(key: str) -> list[float]:
        return [float(r[key]) for r in rows if isinstance(r.get(key), (int, float))]

    walls = col("wall_seconds")
    in_toks = col("input_tokens")
    out_toks = col("output_tokens")
    steps = col("steps")
    cost_anchor = col("cost_anchor_usd")
    cost_worst = col("cost_worstcase_usd")

    patch_counts = Counter(r["patch_class"] for r in rows)
    eval_counts = Counter(r["eval_status"] for r in rows)

    projections: dict[str, Any] = {}
    if cost_anchor:
        projections["proj_300_cost_anchor_median_usd"] = round((_median(cost_anchor) or 0.0) * 300, 2)
        projections["proj_300_cost_anchor_p75_usd"] = round((_pct(cost_anchor, 0.75) or 0.0) * 300, 2)
    if cost_worst:
        projections["proj_300_cost_worstcase_median_usd"] = round((_median(cost_worst) or 0.0) * 300, 2)
        projections["proj_300_cost_worstcase_p75_usd"] = round((_pct(cost_worst, 0.75) or 0.0) * 300, 2)
    if walls:
        median_wall = _median(walls) or 0.0
        p75_wall = _pct(walls, 0.75) or 0.0
        for w in (2, 4, 6):
            projections[f"proj_300_runtime_w{w}_low_s"] = int(math.ceil(300 / w) * median_wall)
            projections[f"proj_300_runtime_w{w}_high_s"] = int(math.ceil(300 / w) * p75_wall)

    return {
        "tasks_total": len(rows),
        "wall_seconds": {"mean": _mean(walls), "median": _median(walls), "p95": _pct(walls, 0.95)},
        "input_tokens": {"mean": _mean(in_toks), "median": _median(in_toks), "p95": _pct(in_toks, 0.95)},
        "output_tokens": {"mean": _mean(out_toks), "median": _median(out_toks), "p95": _pct(out_toks, 0.95)},
        "steps": {"mean": _mean(steps), "median": _median(steps), "p95": _pct(steps, 0.95)},
        "cost_per_task_anchor_usd": {
            "mean": _mean(cost_anchor),
            "median": _median(cost_anchor),
            "p75": _pct(cost_anchor, 0.75),
            "p95": _pct(cost_anchor, 0.95),
            "total": round(sum(cost_anchor), 4) if cost_anchor else 0.0,
        },
        "cost_per_task_worstcase_usd": {
            "mean": _mean(cost_worst),
            "median": _median(cost_worst),
            "p75": _pct(cost_worst, 0.75),
            "p95": _pct(cost_worst, 0.95),
            "total": round(sum(cost_worst), 4) if cost_worst else 0.0,
        },
        "rate_limit_event_total": sum(r["retry_count"] for r in rows),
        "vertex_400_total": sum(r["vertex_400_count"] for r in rows),
        "auth_4xx_total": sum(r["error_403_count"] for r in rows),
        "server_5xx_total": sum(r["server_5xx_count"] for r in rows),
        "fc_schema_reject_total": sum(r["fc_schema_reject_count"] for r in rows),
        "cache_collision_total": sum(r["cache_collision_count"] for r in rows),
        "orphan_tool_call_total": sum(r["orphan_tool_call_count"] for r in rows),
        "patch_class_distribution": dict(patch_counts),
        "eval_status_distribution": dict(eval_counts),
        "projections": projections,
        "pricing": pricing,
    }


def _format_summary(rollups: dict, rows: list[dict]) -> str:
    def _fmt(x: Any) -> str:
        if x is None:
            return "n/a"
        if isinstance(x, float):
            return f"{x:.2f}"
        return str(x)

    lines = [
        "# OpenHands + Qwen3-Coder Calibration Summary",
        "",
        f"**Tasks total:** {rollups['tasks_total']}",
        "",
        "## Wall Time (seconds)",
    ]
    w = rollups["wall_seconds"]
    lines.append(f"- mean: {_fmt(w['mean'])}, median: {_fmt(w['median'])}, p95: {_fmt(w['p95'])}")
    lines.extend(["", "## Tokens"])
    it, ot = rollups["input_tokens"], rollups["output_tokens"]
    lines.append(f"- input  mean: {_fmt(it['mean'])}, median: {_fmt(it['median'])}, p95: {_fmt(it['p95'])}")
    lines.append(f"- output mean: {_fmt(ot['mean'])}, median: {_fmt(ot['median'])}, p95: {_fmt(ot['p95'])}")
    s = rollups["steps"]
    lines.append(f"- steps  mean: {_fmt(s['mean'])}, median: {_fmt(s['median'])}, p95: {_fmt(s['p95'])}")
    lines.extend(["", "## Cost Per Task (USD) --- anchor = $0.22 / $1.80"])
    c = rollups["cost_per_task_anchor_usd"]
    lines.append(
        f"- mean: {_fmt(c['mean'])}, median: {_fmt(c['median'])}, p75: {_fmt(c['p75'])}, "
        f"p95: {_fmt(c['p95'])}, total: {_fmt(c['total'])}"
    )
    lines.extend(["", "## Cost Per Task (USD) --- worstcase = $1.00 / $4.00"])
    cw = rollups["cost_per_task_worstcase_usd"]
    lines.append(
        f"- mean: {_fmt(cw['mean'])}, median: {_fmt(cw['median'])}, p75: {_fmt(cw['p75'])}, "
        f"p95: {_fmt(cw['p95'])}, total: {_fmt(cw['total'])}"
    )
    lines.extend(["", "## Health Signals"])
    lines.append(f"- rate-limit events total: {rollups['rate_limit_event_total']}")
    lines.append(f"- Vertex HTTP 400 total:   {rollups['vertex_400_total']}")
    lines.append(f"- auth 4xx total:          {rollups['auth_4xx_total']}")
    lines.append(f"- server 5xx total:        {rollups['server_5xx_total']}")
    lines.append(f"- FC schema rejects:       {rollups['fc_schema_reject_total']}")
    lines.append(f"- cache_collision events:  {rollups['cache_collision_total']}")
    lines.append(f"- orphan_tool_call events: {rollups['orphan_tool_call_total']}")
    lines.extend(["", "## Patch Class Distribution"])
    for k, v in sorted(rollups["patch_class_distribution"].items()):
        lines.append(f"- {k}: {v}")
    lines.extend(["", "## Eval Status Distribution"])
    for k, v in sorted(rollups["eval_status_distribution"].items()):
        lines.append(f"- {k}: {v}")
    lines.extend(["", "## 300-Task Projections"])
    proj = rollups["projections"]
    if "proj_300_cost_anchor_median_usd" in proj:
        lines.append(
            f"- cost anchor:    ${proj['proj_300_cost_anchor_median_usd']:.2f} (median) "
            f".. ${proj['proj_300_cost_anchor_p75_usd']:.2f} (p75)"
        )
    if "proj_300_cost_worstcase_median_usd" in proj:
        lines.append(
            f"- cost worstcase: ${proj['proj_300_cost_worstcase_median_usd']:.2f} (median) "
            f".. ${proj['proj_300_cost_worstcase_p75_usd']:.2f} (p75)"
        )
    for w_ in (2, 4, 6):
        low = proj.get(f"proj_300_runtime_w{w_}_low_s")
        high = proj.get(f"proj_300_runtime_w{w_}_high_s")
        if low is not None:
            lines.append(
                f"- runtime @ workers={w_}: ~{low // 60} min (median) .. {high // 60} min (p75)"
            )
    lines.extend(["", "## Per-Task Table"])
    lines.append(
        "| instance_id | shard | wall_s | in_tok | out_tok | steps | "
        "cost_anchor | cost_worst | retries | v400 | patch | eval |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            "| {iid} | {sh} | {w} | {i} | {o} | {s} | {ca:.3f} | {cw:.3f} | "
            "{rt} | {v4} | {pc} | {es} |".format(
                iid=r["instance_id"],
                sh=r["shard_id"],
                w=f"{r['wall_seconds']:.0f}" if r["wall_seconds"] is not None else "n/a",
                i=r["input_tokens"],
                o=r["output_tokens"],
                s=r["steps"],
                ca=r.get("cost_anchor_usd", 0.0),
                cw=r.get("cost_worstcase_usd", 0.0),
                rt=r["retry_count"],
                v4=r["vertex_400_count"],
                pc=r["patch_class"],
                es=r["eval_status"],
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--shard", required=True, choices=["A", "B", "ALL"])
    p.add_argument("--outdir", required=True)
    p.add_argument(
        "--manifest",
        default="benchmarks/swebench/cal20_live_lite_oh.manifest.json",
        help="Manifest for the 20-task selection (to preserve instance order)",
    )
    p.add_argument(
        "--pricing",
        default=None,
        help="Optional pricing JSON with anchor/worstcase blocks; defaults to in-code anchors",
    )
    args = p.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    manifest_ids: list[str] = manifest["selected"]

    pricing = _load_pricing(Path(args.pricing) if args.pricing else None)

    if args.shard == "ALL":
        rows = _process_shard(outdir, "A", manifest_ids, pricing) + _process_shard(
            outdir, "B", manifest_ids, pricing
        )
    else:
        rows = _process_shard(outdir, args.shard, manifest_ids, pricing)

    jsonl_path = outdir / "cal_metrics.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    rollups = _rollups(rows, pricing)
    summary = _format_summary(rollups, rows)

    (outdir / "cal_summary.md").write_text(summary, encoding="utf-8")
    (outdir / "cal_rollups.json").write_text(
        json.dumps(rollups, indent=2, default=str) + "\n", encoding="utf-8"
    )

    print(f"wrote {jsonl_path}")
    print(f"wrote {outdir / 'cal_summary.md'}")
    print(f"wrote {outdir / 'cal_rollups.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
