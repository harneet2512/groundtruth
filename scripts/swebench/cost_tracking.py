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

_PRICING = {
    "input_cost_per_token": 0.14e-6,
    "output_cost_per_token": 0.28e-6,
    "litellm_provider": "openrouter",
    "mode": "chat",
}
litellm.register_model({
    "openrouter/deepseek/deepseek-v4-flash": _PRICING,
    "openai/deepseek-v4-flash": _PRICING,
    "deepseek/deepseek-v4-flash": _PRICING,
})


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

        record = {
            "ts": time.time(),
            "model": kwargs.get("model"),
            "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
            "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
            "cost_usd_litellm": cost,
            "openrouter_gen_id": getattr(completion_response, "id", None),
            "has_reasoning": has_reasoning,
        }
        with open(COST_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

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
