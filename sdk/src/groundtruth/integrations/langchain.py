"""LangChain tool wrappers."""

from __future__ import annotations

from typing import Any

from groundtruth.core import GroundTruth


def as_langchain_tools(gt: GroundTruth) -> list[Any]:
    """Return GroundTruth briefing/check/context wrappers as LangChain tools."""
    try:
        from langchain_core.tools import tool
    except ImportError as exc:  # pragma: no cover - exercised via importorskip smoke test
        raise ImportError("Install `[langchain]` extra: pip install groundtruth[langchain]") from exc

    @tool("gt_briefing", return_direct=False)
    def gt_briefing(symbol: str, family: str = "TARGET", max_results: int = 10) -> str:
        """Return a deterministic briefing for an indexed symbol."""
        return gt.briefing(symbol, family=family, max_results=max_results).evidence_text

    @tool("gt_check")
    def gt_check(path: str, diff: str = "") -> str:
        """Summarize deterministic file-level blast radius."""
        summary = gt.check(path, diff=diff or None).summary
        return summary

    @tool("gt_context")
    def gt_context(
        symbol: str,
        direction: str = "callers",
        scope: str = "",
        depth: int = 2,
    ) -> str:
        """Return deterministic local neighborhood context."""
        return gt.context(
            symbol,
            direction=direction,
            scope=scope or None,
            depth=int(depth),
        ).evidence

    return [gt_briefing, gt_check, gt_context]
