"""Multi-region Vertex preflight for the OpenHands + Qwen3-Coder-480B-A35B baseline calibration.

Purpose
-------
Decide which Vertex region (``us-south1`` vs ``global``) to use for the 20-task smoke,
on evidence, and write that decision to ``<outdir>/region_decision.json``. The plan
explicitly forbids hard-picking a region.

Per region we run:
  R1. Function-calling single-turn: model emits a valid ``tool_calls`` entry
  R2. Multi-turn tool-history stability (3 turns) --- reproduces the Vertex
      cache_control/tools collision class of failure if it applies to Qwen MaaS
  R3. Burst latency (5 sequential calls): median and p95
  R4. Concurrency (n=8, workers=4): 4xx count, 5xx count, rate-limit count
  R5. Dataset-adjacent model call: feed the first manifest instance's problem
      statement into a single completion (no tool-calling) to exercise realistic
      input sizes.

Decision rule (from plan section E):
  * Hard reject any region with an FC single-turn failure or multi-turn instability
  * Prefer the region with fewer 4xx/5xx aggregated across R1..R5
  * Tiebreaker: lower p95 latency from R3

Exit code
---------
0  on decision written successfully (even if only one region passes)
1  if BOTH regions fail --- do NOT silently fall through; stop the launch.

Usage::

    python scripts/vertex_qwen_preflight.py \
        --config configs/baseline_oh_qwen3coder_live_lite.toml \
        --manifest benchmarks/swebench/cal20_live_lite_oh.manifest.json \
        --regions us-south1,global \
        --outdir $OUTDIR/preflight \
        --project $VERTEXAI_PROJECT

Environment:
  ``GOOGLE_APPLICATION_CREDENTIALS`` must point at a service-account JSON.
"""
from __future__ import annotations

import argparse
import concurrent.futures as _cf
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

MODEL_DEFAULT = "vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas"

_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _litellm_completion(
    model: str,
    project: str,
    location: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    max_tokens: int = 256,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Thin wrapper around ``litellm.completion`` that classifies errors into 4xx/5xx."""
    try:
        import litellm  # type: ignore
    except ImportError as e:
        return {"ok": False, "reason": f"litellm not installed: {e}", "status_class": "client"}

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "vertex_project": project,
        "vertex_location": location,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "drop_params": True,
    }
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice

    t0 = time.time()
    try:
        resp = litellm.completion(**kwargs)
        latency = time.time() - t0
        choice = resp["choices"][0]["message"]  # type: ignore[index]
        content = choice.get("content") or ""
        tool_calls = choice.get("tool_calls")
        try:
            usage = getattr(resp, "usage", None) or resp.get("usage")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            usage = None
        return {
            "ok": True,
            "content": content[:200],
            "tool_calls": tool_calls,
            "latency_s": round(latency, 3),
            "usage": usage,
            "status_class": "2xx",
        }
    except Exception as e:  # noqa: BLE001
        latency = time.time() - t0
        msg = str(e)
        status_class = "unknown"
        lowered = msg.lower()
        if any(code in msg for code in ("400", "401", "403", "404", "429")):
            status_class = "4xx"
        if "429" in msg or "rate" in lowered or "resource_exhausted" in lowered:
            status_class = "429"
        if any(code in msg for code in ("500", "502", "503", "504")) or "internal" in lowered:
            status_class = "5xx"
        return {
            "ok": False,
            "reason": msg[:500],
            "latency_s": round(latency, 3),
            "status_class": status_class,
        }


def probe_r1_fc_single(model: str, project: str, region: str) -> dict:
    r = _litellm_completion(
        model,
        project,
        region,
        [{"role": "user", "content": "Use list_files to list /tmp. You MUST call the tool."}],
        tools=_TOOL_SCHEMA,
        tool_choice="auto",
    )
    ok = bool(r.get("ok")) and bool(r.get("tool_calls"))
    return {"name": "fc_single", "ok": ok, **r}


def probe_r2_fc_multiturn(model: str, project: str, region: str) -> dict:
    messages: list[dict] = [
        {
            "role": "system",
            "content": "You are a helpful assistant that interacts with tools.",
        }
    ]
    turns: list[dict] = []
    errors_4xx = 0
    errors_5xx = 0
    errors_429 = 0
    for turn in range(3):
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Turn {turn + 1}: list files under /tmp. You MUST call list_files "
                    'with path="/tmp".'
                ),
            }
        )
        r = _litellm_completion(
            model, project, region, messages, tools=_TOOL_SCHEMA, tool_choice="auto"
        )
        turns.append(
            {
                "turn": turn + 1,
                "ok": bool(r.get("ok")),
                "has_tool_calls": bool(r.get("tool_calls")),
                "latency_s": r.get("latency_s"),
                "status_class": r.get("status_class"),
                "reason": r.get("reason"),
            }
        )
        if r.get("status_class") == "4xx":
            errors_4xx += 1
        elif r.get("status_class") == "5xx":
            errors_5xx += 1
        elif r.get("status_class") == "429":
            errors_429 += 1

        if r.get("ok"):
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{turn}",
                            "type": "function",
                            "function": {
                                "name": "list_files",
                                "arguments": '{"path": "/tmp"}',
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{turn}",
                    "name": "list_files",
                    "content": "/tmp/a.log /tmp/b.log",
                }
            )
        else:
            break

    ok = all(t["ok"] and t["has_tool_calls"] for t in turns) and len(turns) == 3
    return {
        "name": "fc_multiturn",
        "ok": ok,
        "turns": turns,
        "errors_4xx": errors_4xx,
        "errors_5xx": errors_5xx,
        "errors_429": errors_429,
    }


def probe_r3_burst(model: str, project: str, region: str, n: int = 5) -> dict:
    latencies: list[float] = []
    results: list[dict] = []
    errors_4xx = errors_5xx = errors_429 = 0
    for _ in range(n):
        r = _litellm_completion(
            model, project, region, [{"role": "user", "content": "Reply with a single digit: 1"}]
        )
        results.append(r)
        if r.get("ok") and r.get("latency_s") is not None:
            latencies.append(float(r["latency_s"]))
        sc = r.get("status_class")
        if sc == "4xx":
            errors_4xx += 1
        elif sc == "5xx":
            errors_5xx += 1
        elif sc == "429":
            errors_429 += 1

    p50 = statistics.median(latencies) if latencies else None
    p95 = None
    if latencies:
        sorted_l = sorted(latencies)
        idx = max(0, int(0.95 * len(sorted_l)) - 1)
        p95 = sorted_l[idx]

    return {
        "name": "burst",
        "ok": errors_4xx == 0 and errors_5xx == 0 and all(r.get("ok") for r in results),
        "n": n,
        "latency_p50": p50,
        "latency_p95": p95,
        "errors_4xx": errors_4xx,
        "errors_5xx": errors_5xx,
        "errors_429": errors_429,
    }


def probe_r4_concurrency(
    model: str, project: str, region: str, n: int = 8, workers: int = 4
) -> dict:
    def _one(_: int) -> dict:
        return _litellm_completion(
            model,
            project,
            region,
            [{"role": "user", "content": "Reply with a single digit: 1"}],
        )

    t0 = time.time()
    with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_one, range(n)))
    wall = time.time() - t0

    errors_4xx = sum(1 for r in results if r.get("status_class") == "4xx")
    errors_5xx = sum(1 for r in results if r.get("status_class") == "5xx")
    errors_429 = sum(1 for r in results if r.get("status_class") == "429")
    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "name": "concurrency",
        "ok": errors_4xx == 0 and errors_5xx == 0 and errors_429 <= 1 and ok_count >= n - 1,
        "n": n,
        "workers": workers,
        "ok_count": ok_count,
        "errors_4xx": errors_4xx,
        "errors_5xx": errors_5xx,
        "errors_429": errors_429,
        "wall_seconds": round(wall, 2),
    }


def probe_r5_dataset_call(
    model: str, project: str, region: str, problem_statement: str
) -> dict:
    prompt = (
        "You are a software engineer. Read the following issue and in ONE sentence "
        "describe the single most important first step you would take. Do not "
        "produce a patch.\n\n<issue>\n"
        + problem_statement[:6000]
        + "\n</issue>"
    )
    r = _litellm_completion(
        model, project, region, [{"role": "user", "content": prompt}], max_tokens=256
    )
    return {
        "name": "dataset_call",
        "ok": bool(r.get("ok")) and bool(r.get("content")),
        **{k: v for k, v in r.items() if k != "content"},
        "content_head": (r.get("content") or "")[:200],
    }


def run_region(
    model: str, project: str, region: str, first_problem: str | None
) -> dict:
    print(f"[preflight] region={region} --- starting probes", flush=True)
    r1 = probe_r1_fc_single(model, project, region)
    print(f"  R1 fc_single ok={r1['ok']}", flush=True)
    r2 = probe_r2_fc_multiturn(model, project, region)
    print(f"  R2 fc_multiturn ok={r2['ok']}", flush=True)
    r3 = probe_r3_burst(model, project, region)
    print(f"  R3 burst ok={r3['ok']} p50={r3.get('latency_p50')} p95={r3.get('latency_p95')}", flush=True)
    r4 = probe_r4_concurrency(model, project, region)
    print(f"  R4 concurrency ok={r4['ok']} 4xx={r4['errors_4xx']} 5xx={r4['errors_5xx']}", flush=True)
    r5 = (
        probe_r5_dataset_call(model, project, region, first_problem)
        if first_problem
        else {"name": "dataset_call", "ok": True, "skipped": True}
    )
    print(f"  R5 dataset_call ok={r5['ok']}", flush=True)

    probes = [r1, r2, r3, r4, r5]
    errors_4xx = sum(int(p.get("errors_4xx", 0)) for p in probes)
    errors_5xx = sum(int(p.get("errors_5xx", 0)) for p in probes)
    fc_ok = bool(r1["ok"] and r2["ok"])
    return {
        "region": region,
        "fc_ok": fc_ok,
        "errors_4xx_total": errors_4xx,
        "errors_5xx_total": errors_5xx,
        "latency_p95": r3.get("latency_p95"),
        "latency_p50": r3.get("latency_p50"),
        "probes": probes,
        "region_hard_reject": not fc_ok,
    }


def pick_winner(region_results: list[dict]) -> tuple[str | None, str]:
    """Apply the section-E decision rule. Returns (winner_region, rationale)."""
    viable = [r for r in region_results if not r["region_hard_reject"]]
    if not viable:
        return None, "No region passed function-calling probes; plan says stop, do not switch providers."
    # Prefer fewer 4xx/5xx aggregate.
    viable_sorted = sorted(
        viable,
        key=lambda r: (
            r["errors_4xx_total"] + r["errors_5xx_total"],
            r.get("latency_p95") or 1e9,
        ),
    )
    top = viable_sorted[0]
    rationale_bits = [
        f"region={top['region']}",
        f"fc_ok={top['fc_ok']}",
        f"4xx={top['errors_4xx_total']}",
        f"5xx={top['errors_5xx_total']}",
        f"p95={top.get('latency_p95')}",
    ]
    if len(viable_sorted) > 1:
        runner = viable_sorted[1]
        rationale_bits.append(
            f"runner_up={runner['region']} (4xx={runner['errors_4xx_total']} "
            f"5xx={runner['errors_5xx_total']} p95={runner.get('latency_p95')})"
        )
    return top["region"], "; ".join(rationale_bits)


def load_first_problem_statement(manifest_path: Path) -> str | None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[preflight] could not read manifest: {e}", file=sys.stderr)
        return None
    selected = manifest.get("selected") or []
    if not selected:
        return None
    dataset = manifest.get("dataset", "SWE-bench-Live/SWE-bench-Live")
    split = manifest.get("split", "lite")
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        print("[preflight] datasets not installed; skipping R5 dataset call", file=sys.stderr)
        return None
    first_id = selected[0]
    try:
        ds = load_dataset(dataset, split=split)
        for row in ds:  # type: ignore[assignment]
            if row.get("instance_id") == first_id:  # type: ignore[union-attr]
                return row.get("problem_statement") or row.get("problem") or None  # type: ignore[union-attr]
    except Exception as e:  # noqa: BLE001
        print(f"[preflight] dataset load failed: {e}", file=sys.stderr)
        return None
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, help="Path to baseline_oh_qwen3coder_live_lite.toml")
    p.add_argument("--manifest", required=True, help="Path to cal20_live_lite_oh.manifest.json")
    p.add_argument("--regions", default="us-south1,global")
    p.add_argument("--outdir", required=True, help="Directory to write preflight artifacts")
    p.add_argument("--project", default=os.environ.get("VERTEXAI_PROJECT", ""))
    p.add_argument("--model", default=MODEL_DEFAULT)
    args = p.parse_args()

    if not args.project:
        print("ERROR: --project (or VERTEXAI_PROJECT env) is required", file=sys.stderr)
        return 2
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        print(
            "ERROR: GOOGLE_APPLICATION_CREDENTIALS is not set; preflight requires a service account",
            file=sys.stderr,
        )
        return 2

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    if not regions:
        print("ERROR: no regions supplied", file=sys.stderr)
        return 2

    first_problem = load_first_problem_statement(Path(args.manifest))

    region_results: list[dict] = []
    for region in regions:
        result = run_region(args.model, args.project, region, first_problem)
        _write(outdir / f"region_{region}.json", result)
        region_results.append(result)

    winner, rationale = pick_winner(region_results)
    decision = {
        "winner": winner,
        "rationale": rationale,
        "model": args.model,
        "regions_tested": regions,
        "regions": region_results,
    }
    _write(outdir / "region_decision.json", decision)

    if winner is None:
        print("FAIL: no region is viable. Stop; do not switch providers mid-calibration.", file=sys.stderr)
        return 1

    print(f"OK: winner={winner}. {rationale}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
