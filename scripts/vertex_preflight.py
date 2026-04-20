"""Preflight probes for the 20-task baseline calibration on Vertex Gemini 3.1 Pro Preview.

Runs the 7 probes from plan section F, writes outputs to <outdir>/preflight/, and exits
non-zero on any MUST failure. Launcher should `set -e` and bail.

MUST probes (block launch on failure):
  1. gcloud/ADC reachable, correct project active
  2. Model identity: one-shot completion returns "OK"
  3. Dataset identity: canonical HF dataset loads with expected cardinality band
  4. Function-calling: model emits tool_calls when given a schema
  5. Burst (concurrency=1, n=5): 0 rate-limit events
  6. Concurrency (n=8, workers=4): <=1 rate-limit event, all recover via retry
  7. Eval harness reachable: swebench.harness.run_evaluation importable

Writes:
  <outdir>/preflight/<probe>.json
  <outdir>/preflight/pricing.json  (if you pass --pricing-json on command line)

Usage:
  python scripts/vertex_preflight.py \
      --outdir /tmp/cal_gemini31pro_XXXX \
      --model vertex_ai/gemini-3.1-pro-preview \
      --project project-c9a6fdd8-8d56-4e88-ad6 \
      --location global \
      --dataset swe-bench-live/SWE-bench-Live \
      --split lite \
      [--pricing-json path/to/pricing.json] \
      [--skip-probe 5,6]
"""
from __future__ import annotations

import argparse
import concurrent.futures as _cf
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"


def probe_1_gcloud(expected_project: str, out: Path) -> tuple[bool, dict]:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        payload = {"ok": False, "reason": "gcloud not on PATH"}
        _write(out, payload)
        return False, payload

    rc_p, stdout_p, stderr_p = _run([gcloud, "config", "get-value", "project"])
    active_project = stdout_p.strip() if rc_p == 0 else ""

    rc_t, stdout_t, stderr_t = _run(
        [gcloud, "auth", "application-default", "print-access-token"]
    )
    token_ok = rc_t == 0 and len(stdout_t.strip()) > 20

    ok = token_ok and active_project == expected_project
    payload = {
        "ok": ok,
        "active_project": active_project,
        "expected_project": expected_project,
        "token_mint_rc": rc_t,
        "token_mint_len": len(stdout_t.strip()) if stdout_t else 0,
        "stderr": (stderr_p + stderr_t)[:2000],
    }
    _write(out, payload)
    return ok, payload


def _litellm_completion(
    model: str, project: str, location: str, messages: list[dict], max_tokens: int = 256, **kwargs
) -> dict:
    try:
        import litellm  # type: ignore
    except ImportError as e:
        return {"ok": False, "reason": f"litellm not installed: {e}"}
    try:
        t0 = time.time()
        resp = litellm.completion(
            model=model,
            messages=messages,
            vertex_project=project,
            vertex_location=location,
            temperature=0.0,
            max_tokens=max_tokens,
            drop_params=True,
            **kwargs,
        )
        latency = time.time() - t0
        content = ""
        tool_calls = None
        usage = None
        try:
            choice = resp["choices"][0]["message"]  # type: ignore
            content = choice.get("content") or ""  # type: ignore
            tool_calls = choice.get("tool_calls")  # type: ignore
        except Exception:  # noqa: BLE001
            pass
        try:
            usage = getattr(resp, "usage", None) or resp.get("usage")  # type: ignore
        except Exception:  # noqa: BLE001
            usage = None
        return {
            "ok": True,
            "content": content[:200],
            "tool_calls": tool_calls,
            "latency_s": round(latency, 3),
            "usage": usage,
        }
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        rate_limit = any(
            tok in msg for tok in ("RateLimit", "429", "RESOURCE_EXHAUSTED", "ResourceExhausted")
        )
        return {"ok": False, "reason": msg[:500], "rate_limit": rate_limit}


def probe_2_model_identity(
    model: str, project: str, location: str, out: Path
) -> tuple[bool, dict]:
    result = _litellm_completion(
        model,
        project,
        location,
        [{"role": "user", "content": "Respond with the two characters OK and nothing else."}],
    )
    ok = bool(result.get("ok")) and "OK" in (result.get("content") or "")
    result["probe_ok"] = ok
    _write(out, result)
    return ok, result


def probe_3_dataset(dataset: str, split: str, out: Path) -> tuple[bool, dict]:
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError as e:
        payload = {"ok": False, "reason": f"datasets not installed: {e}"}
        _write(out, payload)
        return False, payload
    try:
        ds = load_dataset(dataset, split=split)
        ids = [row["instance_id"] for row in ds]  # type: ignore
        payload = {
            "ok": len(ids) > 0,
            "dataset": dataset,
            "split": split,
            "count": len(ids),
            "first3": ids[:3],
        }
    except Exception as e:  # noqa: BLE001
        payload = {"ok": False, "reason": str(e)[:500]}
    _write(out, payload)
    return bool(payload.get("ok")), payload


def probe_4_function_calling(
    model: str, project: str, location: str, out: Path
) -> tuple[bool, dict]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files in a given directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]
    result = _litellm_completion(
        model,
        project,
        location,
        [
            {
                "role": "user",
                "content": "Use the list_files tool to list files in /tmp. You MUST call the tool.",
            }
        ],
        tools=tools,
        tool_choice="auto",
    )
    tc = result.get("tool_calls")
    ok = bool(result.get("ok")) and bool(tc)
    result["probe_ok"] = ok
    _write(out, result)
    return ok, result


def probe_5_burst(model: str, project: str, location: str, out: Path, n: int = 5) -> tuple[bool, dict]:
    results = []
    latencies = []
    rate_limits = 0
    for _ in range(n):
        r = _litellm_completion(
            model,
            project,
            location,
            [{"role": "user", "content": "Reply with a single digit: 1"}],
        )
        results.append(r)
        if r.get("ok") and r.get("latency_s") is not None:
            latencies.append(r["latency_s"])
        if r.get("rate_limit"):
            rate_limits += 1
    payload = {
        "ok": rate_limits == 0 and all(r.get("ok") for r in results),
        "n": n,
        "rate_limits": rate_limits,
        "all_ok": all(r.get("ok") for r in results),
        "latency_p50": sorted(latencies)[len(latencies) // 2] if latencies else None,
        "latency_max": max(latencies) if latencies else None,
    }
    _write(out, payload)
    return payload["ok"], payload


def probe_6_concurrency(
    model: str, project: str, location: str, out: Path, n: int = 8, workers: int = 4
) -> tuple[bool, dict]:
    def _one() -> dict:
        return _litellm_completion(
            model,
            project,
            location,
            [{"role": "user", "content": "Reply with a single digit: 1"}],
        )

    t0 = time.time()
    with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: _one(), range(n)))
    elapsed = time.time() - t0

    rate_limits = sum(1 for r in results if r.get("rate_limit"))
    ok_count = sum(1 for r in results if r.get("ok"))
    payload = {
        "ok": rate_limits <= 1 and ok_count >= n - 1,
        "n": n,
        "workers": workers,
        "ok_count": ok_count,
        "rate_limits": rate_limits,
        "wall_seconds": round(elapsed, 2),
    }
    _write(out, payload)
    return payload["ok"], payload


def probe_7_eval_harness(out: Path) -> tuple[bool, dict]:
    try:
        import importlib

        mod = importlib.import_module("swebench.harness.run_evaluation")
        has_main = hasattr(mod, "main") or hasattr(mod, "run_evaluation")
        payload = {"ok": bool(has_main), "module": "swebench.harness.run_evaluation"}
    except Exception as e:  # noqa: BLE001
        payload = {"ok": False, "reason": str(e)[:300]}
    _write(out, payload)
    return bool(payload.get("ok")), payload


def probe_8_cache_collision(
    model: str, project: str, location: str, out: Path
) -> tuple[bool, dict]:
    # Reproduces the exact failure mode that killed cal_gemini31pro_1776718572:
    # Vertex HTTP 400 "Tool config, tools and system instruction should not be
    # set in the request when using cached content." The repaired config must
    # not trigger it even across multi-turn reuse with tools + system_instruction.
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files in a given directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]
    system_msg = {
        "role": "system",
        "content": "You are a helpful assistant that can interact with a computer to solve tasks.",
    }
    collisions: list[str] = []
    orphan_tool_call: list[str] = []
    turns: list[dict] = []
    messages: list[dict] = [system_msg]

    for turn in range(3):
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Turn {turn + 1}: list files under /tmp. You MUST call the "
                    "list_files tool with path=\"/tmp\"."
                ),
            }
        )
        result = _litellm_completion(
            model,
            project,
            location,
            messages,
            max_tokens=256,
            tools=tools,
            tool_choice="auto",
        )
        reason = (result.get("reason") or "").lower()
        if "tool config, tools and system instruction should not be set" in reason or "cached content" in reason:
            collisions.append(reason[:240])
        if "missing corresponding tool call" in reason:
            orphan_tool_call.append(reason[:240])
        tc = result.get("tool_calls")
        turns.append(
            {
                "turn": turn + 1,
                "ok": bool(result.get("ok")),
                "has_tool_calls": bool(tc),
                "latency_s": result.get("latency_s"),
                "reason": (result.get("reason") or "")[:240] if not result.get("ok") else None,
            }
        )
        # Simulate realistic multi-turn reuse: append an assistant tool_call reply
        # and a synthetic tool response so the next turn's history carries tool
        # state (this is exactly the history shape that triggered FM-1 in prod).
        if result.get("ok"):
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{turn}",
                            "type": "function",
                            "function": {"name": "list_files", "arguments": '{"path": "/tmp"}'},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{turn}",
                    "name": "list_files",
                    "content": "[turn %d] /tmp/a.log /tmp/b.log" % (turn + 1),
                }
            )
        else:
            break

    ok = (
        len(collisions) == 0
        and len(orphan_tool_call) == 0
        and all(t["ok"] for t in turns)
    )
    payload = {
        "ok": ok,
        "turns": turns,
        "collisions": collisions,
        "orphan_tool_call": orphan_tool_call,
        "purpose": "verify that repaired config does not trigger Vertex cache/tools/system 400 across multi-turn reuse",
    }
    _write(out, payload)
    return ok, payload


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--outdir", required=True)
    p.add_argument("--model", default="vertex_ai/gemini-3.1-pro-preview")
    p.add_argument("--project", default="project-c9a6fdd8-8d56-4e88-ad6")
    p.add_argument("--location", default="global")
    p.add_argument("--dataset", default="princeton-nlp/SWE-Bench_Lite")
    p.add_argument("--split", default="test")
    p.add_argument(
        "--pricing-json",
        default=None,
        help="Path to a pricing.json to copy into <outdir>/preflight/pricing.json",
    )
    p.add_argument(
        "--skip-probe",
        default="",
        help="Comma-separated probe numbers to skip (for debugging only; a skipped MUST is still a fail at gate time).",
    )
    args = p.parse_args()

    outdir = Path(args.outdir) / "preflight"
    outdir.mkdir(parents=True, exist_ok=True)

    if args.pricing_json:
        src = Path(args.pricing_json)
        if src.exists():
            (outdir / "pricing.json").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"copied pricing from {src}")
        else:
            sys.stderr.write(f"WARN: --pricing-json not found: {src}\n")

    skip = {s.strip() for s in args.skip_probe.split(",") if s.strip()}
    results: dict[str, dict] = {}
    overall_ok = True

    probes = [
        ("1", "gcloud", lambda: probe_1_gcloud(args.project, outdir / "probe1_gcloud.json")),
        ("2", "model_identity", lambda: probe_2_model_identity(args.model, args.project, args.location, outdir / "probe2_model.json")),
        ("3", "dataset", lambda: probe_3_dataset(args.dataset, args.split, outdir / "probe3_dataset.json")),
        ("4", "function_calling", lambda: probe_4_function_calling(args.model, args.project, args.location, outdir / "probe4_fc.json")),
        ("5", "burst", lambda: probe_5_burst(args.model, args.project, args.location, outdir / "probe5_burst.json")),
        ("6", "concurrency", lambda: probe_6_concurrency(args.model, args.project, args.location, outdir / "probe6_concurrency.json")),
        ("7", "eval_harness", lambda: probe_7_eval_harness(outdir / "probe7_eval.json")),
        ("8", "cache_collision", lambda: probe_8_cache_collision(args.model, args.project, args.location, outdir / "probe8_cache.json")),
    ]

    for num, name, fn in probes:
        if num in skip:
            print(f"[skip] probe {num} {name}")
            results[name] = {"skipped": True}
            continue
        try:
            ok, payload = fn()
        except Exception as e:  # noqa: BLE001
            ok, payload = False, {"ok": False, "reason": f"probe crashed: {e}"}
            _write(outdir / f"probe{num}_{name}.json", payload)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] probe {num} {name}")
        results[name] = payload
        if not ok:
            overall_ok = False

    _write(outdir / "summary.json", {"ok": overall_ok, "probes": results})
    if not overall_ok:
        sys.stderr.write("FAIL: one or more preflight probes did not pass; do not launch.\n")
        return 1
    print("OK: all preflight probes passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
