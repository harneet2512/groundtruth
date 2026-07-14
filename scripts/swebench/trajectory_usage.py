"""Provider-neutral token usage extraction for mini-swe-agent trajectories."""
from __future__ import annotations

from typing import Any


def _number(mapping: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return int(value)
    return None


def _usage_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        extra = message.get("extra") if isinstance(message.get("extra"), dict) else {}
        response = extra.get("response") if isinstance(extra.get("response"), dict) else {}
        usage = response.get("usage")
    return usage if isinstance(usage, dict) else None


def _normalized_usage(usage: dict[str, Any]) -> dict[str, int | None]:
    prompt = _number(usage, "input_tokens", "prompt_tokens")
    completion = _number(usage, "output_tokens", "completion_tokens")
    details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    cache_hit = _number(details, "cached_tokens")
    if cache_hit is None:
        cache_hit = _number(usage, "prompt_cache_hit_tokens")
    cache_miss = _number(usage, "prompt_cache_miss_tokens")
    if cache_miss is None and prompt is not None and cache_hit is not None:
        cache_miss = max(prompt - cache_hit, 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
    }


def extract_trajectory_usage(trajectory: dict[str, Any]) -> dict[str, Any]:
    """Return truthful rollup-or-message token totals.

    A complete trajectory-level rollup wins. Otherwise per-model-turn usage is
    summed across both Chat Completions and Responses API message shapes. Missing
    fields remain ``None`` rather than becoming measured zeroes.
    """
    info = trajectory.get("info") if isinstance(trajectory.get("info"), dict) else {}
    stats = info.get("model_stats") if isinstance(info.get("model_stats"), dict) else {}
    rollup = _normalized_usage(stats)
    if rollup["prompt_tokens"] is not None and rollup["completion_tokens"] is not None:
        return {**rollup, "source": "model_stats", "usage_records": 1}

    records = [
        _normalized_usage(usage)
        for message in trajectory.get("messages", []) or []
        if isinstance(message, dict)
        for usage in [_usage_from_message(message)]
        if usage is not None
    ]
    if not records:
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "cache_hit_tokens": None,
            "cache_miss_tokens": None,
            "source": "unavailable",
            "usage_records": 0,
        }

    def _sum_if_complete(key: str) -> int | None:
        values = [record[key] for record in records]
        return sum(values) if all(value is not None for value in values) else None

    return {
        "prompt_tokens": _sum_if_complete("prompt_tokens"),
        "completion_tokens": _sum_if_complete("completion_tokens"),
        "cache_hit_tokens": _sum_if_complete("cache_hit_tokens"),
        "cache_miss_tokens": _sum_if_complete("cache_miss_tokens"),
        "source": "message_usage",
        "usage_records": len(records),
    }
