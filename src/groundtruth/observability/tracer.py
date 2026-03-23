"""Endpoint tracer — context manager for building trace records.

Usage:
    tracer = EndpointTracer(writer)

    with tracer.trace("groundtruth_impact", symbol="getUserById") as t:
        t.log_component("graph_callers", ComponentStatus.USED,
                        output_summary="5 direct callers", item_count=5)
        t.log_component("obligations", ComponentStatus.ABSTAINED,
                        reason="index stale for target file")
        t.log_component("freshness", ComponentStatus.USED,
                        output_summary="2 stale files", confidence=0.8)

        t.synthesize(
            included=["graph_callers", "freshness"],
            excluded=["obligations"],
            exclusion_reasons={"obligations": "index stale"},
            verdict="5 callers at risk, 2 stale files"
        )

        t.respond(
            response_type="impact_analysis",
            item_count=5,
            verdict="HIGH",
            output_summary="5 direct callers, 12 transitive"
        )
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Generator

from groundtruth.observability.schema import (
    ComponentStatus,
    ComponentTrace,
    EndpointTrace,
    RequestLayer,
    ResponseLayer,
    SynthesisLayer,
)

if TYPE_CHECKING:
    from groundtruth.observability.writer import TraceWriter


class TraceContext:
    """Builder for a single endpoint trace. Used inside `with` block."""

    def __init__(self, request: RequestLayer) -> None:
        self._request = request
        self._components: list[ComponentTrace] = []
        self._synthesis = SynthesisLayer()
        self._response = ResponseLayer()
        self._start = time.monotonic()

    def log_component(
        self,
        component: str,
        status: ComponentStatus,
        *,
        output_summary: str = "",
        confidence: float | None = None,
        trust_tier: str | None = None,
        reason: str = "",
        item_count: int = 0,
        duration_ms: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record one internal component's execution."""
        self._components.append(
            ComponentTrace(
                component=component,
                status=status,
                output_summary=output_summary,
                confidence=confidence,
                trust_tier=trust_tier,
                reason=reason,
                item_count=item_count,
                duration_ms=duration_ms or 0.0,
                extra=extra or {},
            )
        )

    def synthesize(
        self,
        *,
        included: list[str] | None = None,
        excluded: list[str] | None = None,
        exclusion_reasons: dict[str, str] | None = None,
        verdict: str = "",
        ranking_summary: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record the synthesis decision."""
        self._synthesis = SynthesisLayer(
            included=included or [],
            excluded=excluded or [],
            exclusion_reasons=exclusion_reasons or {},
            verdict=verdict,
            ranking_summary=ranking_summary,
            extra=extra or {},
        )

    def respond(
        self,
        *,
        response_type: str = "",
        item_count: int = 0,
        verdict: str = "",
        next_step: str = "",
        output_summary: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record the final response."""
        self._response = ResponseLayer(
            response_type=response_type,
            item_count=item_count,
            verdict=verdict,
            next_step=next_step,
            output_summary=output_summary,
            extra=extra or {},
        )

    def _finalize(self) -> EndpointTrace:
        """Build the final trace record."""
        elapsed = (time.monotonic() - self._start) * 1000
        self._response.total_duration_ms = elapsed
        return EndpointTrace(
            request=self._request,
            components=self._components,
            synthesis=self._synthesis,
            response=self._response,
        )


class EndpointTracer:
    """Creates traced endpoint calls. Writes to TraceWriter on exit."""

    def __init__(self, writer: TraceWriter | None = None) -> None:
        self._writer = writer

    @contextmanager
    def trace(
        self,
        endpoint: str,
        *,
        symbol: str | None = None,
        file_path: str | None = None,
        input_summary: str = "",
        patch_summary: str | None = None,
        task_context: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Generator[TraceContext, None, None]:
        """Context manager for tracing an endpoint call.

        Yields a TraceContext for logging components and synthesis.
        On exit, finalizes and writes the trace.
        """
        request = RequestLayer(
            endpoint=endpoint,
            input_summary=input_summary,
            symbol=symbol,
            file_path=file_path,
            patch_summary=patch_summary,
            task_context=task_context,
            extra=extra or {},
        )
        ctx = TraceContext(request)
        try:
            yield ctx
        finally:
            trace = ctx._finalize()
            if self._writer:
                self._writer.write(trace)
