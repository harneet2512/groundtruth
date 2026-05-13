"""Cost tracking + thinking-off guard for OpenHands smoke runs.

Imported at the TOP of oh_gt_full_wrapper.py BEFORE OpenHands starts so
litellm.register_model and litellm.success_callback take effect for every
LLM call in the agent loop.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import litellm

COST_LOG = os.getenv("GT_COST_LOG", "/tmp/litellm_costs.jsonl")
ABORT_FLAG = os.getenv("GT_ABORT_FLAG", "/tmp/gt_abort_reasoning.flag")

_PRICING_V4FLASH = {
    "input_cost_per_token": 0.14e-6,
    "output_cost_per_token": 0.28e-6,
    "litellm_provider": "openrouter",
    "mode": "chat",
}
_PRICING_QWEN3 = {
    "input_cost_per_token": 0.22e-6,
    "output_cost_per_token": 1.80e-6,
    "litellm_provider": "openrouter",
    "mode": "chat",
    "max_input_tokens": 262144,
    "max_output_tokens": 65536,
    "max_tokens": 262144,
}
for name in ("openrouter/deepseek/deepseek-v4-flash", "openai/deepseek-v4-flash", "deepseek/deepseek-v4-flash"):
    litellm.model_cost[name] = _PRICING_V4FLASH
_PRICING_QWEN3_OAI = {**_PRICING_QWEN3, "litellm_provider": "openai"}
litellm.model_cost["openrouter/qwen/qwen3-coder"] = _PRICING_QWEN3
litellm.model_cost["qwen/qwen3-coder"] = _PRICING_QWEN3
litellm.model_cost["qwen3-coder"] = _PRICING_QWEN3
litellm.model_cost["openai/qwen3-coder"] = _PRICING_QWEN3_OAI
_PRICING_VERTEX_QWEN3 = {
    "input_cost_per_token": 0.45e-6,
    "output_cost_per_token": 1.80e-6,
    "litellm_provider": "vertex_ai",
    "mode": "chat",
    "max_input_tokens": 262144,
    "max_output_tokens": 65536,
    "max_tokens": 262144,
}
litellm.model_cost["vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas"] = _PRICING_VERTEX_QWEN3
litellm.model_cost["openai/qwen3-coder-480b-a35b-instruct-maas"] = _PRICING_VERTEX_QWEN3
litellm.model_cost["openai/qwen/qwen3-coder-480b-a35b-instruct-maas"] = _PRICING_VERTEX_QWEN3

# Monkey-patch: inject sampling params for Vertex qwen3 (top_k, repetition_penalty).
# These match the v1.0.5 config that produced resolves on GCP.
_orig_completion = litellm.completion

def _vertex_params_completion(*args: Any, **kwargs: Any) -> Any:
    model = kwargs.get("model") or (args[0] if args else "")
    if isinstance(model, str) and "qwen3-coder" in model.lower() and "480b" in model.lower():
        eb = dict(kwargs.get("extra_body") or {})
        eb.setdefault("top_k", 20)
        eb.setdefault("repetition_penalty", 1.05)
        kwargs["extra_body"] = eb
    return _orig_completion(*args, **kwargs)

litellm.completion = _vertex_params_completion

_orig_acompletion = getattr(litellm, "acompletion", None)
if _orig_acompletion is not None:
    _saved_acompletion = _orig_acompletion

    async def _vertex_params_acompletion(*args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model") or (args[0] if args else "")
        if isinstance(model, str) and "qwen3-coder" in model.lower() and "480b" in model.lower():
            eb = dict(kwargs.get("extra_body") or {})
            eb.setdefault("top_k", 20)
            eb.setdefault("repetition_penalty", 1.05)
            kwargs["extra_body"] = eb
        return await _saved_acompletion(*args, **kwargs)

    litellm.acompletion = _vertex_params_acompletion


def _detect_reasoning(resp: Any) -> bool:
    try:
        msg = getattr(resp.choices[0], "message", None) if resp.choices else None
        if not msg:
            return False
        if getattr(msg, "reasoning_content", None):
            return True
        if getattr(msg, "reasoning_details", None):
            return True
        return "<think>" in (getattr(msg, "content", "") or "")
    except Exception:
        return False


def _cost_callback(kwargs, completion_response, start_time, end_time):
    try:
        cost = None
        try:
            cost = litellm.completion_cost(completion_response=completion_response)
        except Exception as e:
            print(f"[GT_COST] completion_cost failed: {e}", flush=True)

        usage = getattr(completion_response, "usage", None)
        has_reasoning = _detect_reasoning(completion_response)

        gen_id = getattr(completion_response, "id", None)
        or_cost = None
        or_cached = None
        if gen_id and os.environ.get("OPENROUTER_KEY"):
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"https://openrouter.ai/api/v1/generation?id={gen_id}",
                    headers={"Authorization": f"Bearer {os.environ['OPENROUTER_KEY']}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    gd = json.loads(resp.read()).get("data", {})
                    or_cost = gd.get("total_cost")
                    or_cached = gd.get("native_tokens_cached")
            except Exception:
                pass

        record = {
            "ts": time.time(),
            "model": kwargs.get("model"),
            "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
            "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
            "cost_usd_litellm": cost,
            "cost_usd_openrouter": or_cost,
            "cached_tokens": or_cached,
            "openrouter_gen_id": gen_id,
            "has_reasoning": has_reasoning,
        }
        with open(COST_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

        # Live cost visibility — prints to GHA log in real-time
        _call_num = getattr(_cost_callback, "_n", 0) + 1
        _cost_callback._n = _call_num
        _running = getattr(_cost_callback, "_total", 0.0) + (cost or 0)
        _cost_callback._total = _running
        in_t = getattr(usage, "prompt_tokens", 0) if usage else 0
        out_t = getattr(usage, "completion_tokens", 0) if usage else 0
        cached_t = record.get("cached_tokens") or 0
        print(f"[GT_COST] call={_call_num} in={in_t} out={out_t} cached={cached_t} cost=${cost or 0:.4f} total=${_running:.4f} reasoning={has_reasoning}", flush=True)

        if has_reasoning:
            print(
                "[GT_THINK_GUARD] REASONING DETECTED — abort flag written",
                flush=True,
                file=sys.stderr,
            )
            with open(ABORT_FLAG, "w") as f:
                f.write(json.dumps(record))
    except Exception as e:
        print(f"[GT_COST] callback exception: {e}", flush=True)


if _cost_callback not in litellm.success_callback:
    litellm.success_callback.append(_cost_callback)
