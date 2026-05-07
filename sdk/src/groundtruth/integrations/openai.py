"""OpenAI function-calling schema helpers."""

from __future__ import annotations

from typing import Any, cast

from groundtruth.core import GroundTruth
from groundtruth.models import Direction


def as_openai_tools(gt: GroundTruth) -> dict[str, Any]:
    """Expose JSON schemas plus dispatcher for OpenAI-compatible clients."""
    try:
        from pydantic import BaseModel, Field  # OpenAI SDK pulls pydantic v2 by default
    except ImportError as exc:  # pragma: no cover - exercised via importorskip smoke test
        raise ImportError("Install `[openai]` extra (includes pydantic): pip install groundtruth[openai]") from exc

    class BriefingArgs(BaseModel):
        symbol: str = Field(description="Indexed symbol identifier")
        family: str = Field(default="TARGET")
        max_results: int = Field(default=10)

    class CheckArgs(BaseModel):
        path: str = Field(description="Exact nodes.file_path value inside graph.db")
        diff: str = Field(default="", description="Unified diff verbatim")

    class ContextArgs(BaseModel):
        symbol: str = Field(description="Symbol/query string matching nodes rows")
        direction: str = Field(default="callers")
        scope: str = Field(default="", description="Optional path prefix filter")
        depth: int = Field(default=2)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "gt_briefing",
                "parameters": BriefingArgs.model_json_schema(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "gt_check",
                "parameters": CheckArgs.model_json_schema(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "gt_context",
                "parameters": ContextArgs.model_json_schema(),
            },
        },
    ]

    def dispatch(name: str, raw_args: dict[str, Any]) -> str:
        lowered = name.lower().strip()
        args = dict(raw_args or {})
        if lowered == "gt_briefing":
            b_payload = BriefingArgs(**args)
            return gt.briefing(
                b_payload.symbol,
                family=b_payload.family,
                max_results=int(b_payload.max_results),
            ).evidence_text
        if lowered == "gt_check":
            c_payload = CheckArgs(**args)
            return gt.check(c_payload.path, diff=c_payload.diff or None).summary
        if lowered == "gt_context":
            cx_payload = ContextArgs(**args)
            direction = cast(Direction, cx_payload.direction)
            return gt.context(
                cx_payload.symbol,
                direction=direction,
                scope=cx_payload.scope or None,
                depth=int(cx_payload.depth),
            ).evidence
        raise ValueError(f"Unsupported tool name: {name}")

    return {"tools": tools, "dispatch": dispatch}
