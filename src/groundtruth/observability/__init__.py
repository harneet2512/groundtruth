"""Lightweight observability for GroundTruth endpoint synthesis.

JSONL-based tracing that records which internal components ran,
what they returned, what was included/excluded in the final synthesis,
and why. Designed for debuggability and attribution, not dashboards.
"""

from groundtruth.observability.schema import (
    ComponentStatus,
    ComponentTrace,
    EndpointTrace,
    RequestLayer,
    ResponseLayer,
    SynthesisLayer,
)
from groundtruth.observability.tracer import EndpointTracer
from groundtruth.observability.writer import TraceWriter

__all__ = [
    "ComponentStatus",
    "ComponentTrace",
    "EndpointTrace",
    "EndpointTracer",
    "RequestLayer",
    "ResponseLayer",
    "SynthesisLayer",
    "TraceWriter",
]
