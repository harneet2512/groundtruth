"""Substrate layer — typed interfaces for all semantic evidence logic.

All new semantic extraction, contract mining, verification, and procedure
logic routes through this boundary. Benchmark adapters and hooks must NOT
own semantic policy; they call into the substrate instead.
"""

from groundtruth.substrate.types import (
    ConfidenceTier,
    ContractRecord,
    EvidenceItem,
    LocalizationResult,
    LocalizationTarget,
)
from groundtruth.substrate.protocols import (
    ContractExtractor,
    EvidenceProducer,
    GraphReader,
)

__all__ = [
    "ConfidenceTier",
    "ContractExtractor",
    "ContractRecord",
    "EvidenceItem",
    "EvidenceProducer",
    "GraphReader",
    "LocalizationResult",
    "LocalizationTarget",
]
