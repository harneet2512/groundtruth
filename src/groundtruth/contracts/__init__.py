"""Contract Engine — deterministic behavioral contract extraction.

Extracts reusable behavioral constraints from repositories so the agent
knows what must remain true. Contracts are confidence-gated, deterministic,
and machine-checkable where possible.
"""

from groundtruth.contracts.engine import ContractEngine
from groundtruth.contracts.types import (
    ExceptionContract,
    OutputContract,
    RoundtripContract,
)

__all__ = [
    "ContractEngine",
    "ExceptionContract",
    "OutputContract",
    "RoundtripContract",
]
