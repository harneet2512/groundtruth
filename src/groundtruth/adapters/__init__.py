"""GT scaffold adapters.

Each adapter translates a specific scaffold's events into kernel-canonical
types. Adapters import the kernel; the kernel never imports adapters.
"""

from groundtruth.adapters.base import (
    Adapter,
    AppliedDecision,
    DegradeMap,
    ScaffoldArtifact,
)
from groundtruth.control.types import Capabilities

__all__ = [
    "Adapter",
    "AppliedDecision",
    "Capabilities",
    "DegradeMap",
    "ScaffoldArtifact",
]
