"""CrewAI tool wrappers."""

from __future__ import annotations

from groundtruth.core import GroundTruth


def as_crewai_tools(gt: GroundTruth):
    """Return CrewAI-compatible tool objects."""
    try:
        from crewai.tools import BaseTool  # type: ignore[import-not-found]
        from pydantic import BaseModel, Field  # crewai installs pydantic
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Install `[crewai]` extra: pip install groundtruth[crewai]") from exc

    class BriefingArgs(BaseModel):
        symbol: str = Field(...)
        family: str = Field(default="TARGET")
        max_results: int = Field(default=10)

    class CheckArgs(BaseModel):
        path: str = Field(...)
        diff: str = Field(default="")

    class ContextArgs(BaseModel):
        symbol: str = Field(...)
        direction: str = Field(default="callers")
        scope: str = Field(default="")
        depth: int = Field(default=2)

    class BriefingTool(BaseTool):
        name: str = "gt_briefing"
        description: str = "Produce deterministic briefing for GroundTruth indexed symbol."
        args_schema: type[BriefingArgs] = BriefingArgs

        def _run(self, symbol: str, family: str = "TARGET", max_results: int = 10) -> str:  # type: ignore[override]
            return gt.briefing(symbol, family=family, max_results=max_results).evidence_text

    class CheckTool(BaseTool):
        name: str = "gt_check"
        description: str = "File-level deterministic impact rollup + optional verbatim diff."

        args_schema = CheckArgs

        def _run(self, path: str, diff: str = "") -> str:  # type: ignore[override]
            return gt.check(path, diff=diff or None).summary

    class ContextTool(BaseTool):
        name: str = "gt_context"
        description: str = "Deterministic subgraph context for a symbol."

        args_schema = ContextArgs

        def _run(self, symbol: str, direction: str = "callers", scope: str = "", depth: int = 2) -> str:  # type: ignore[override]
            return gt.context(
                symbol,
                direction=direction,  # type: ignore[arg-type]
                scope=scope or None,
                depth=int(depth),
            ).evidence

    return BriefingTool(), CheckTool(), ContextTool()
