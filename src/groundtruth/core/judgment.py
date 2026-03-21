"""Shared judgment interface -- the contract between product and eval harness.

Both src/groundtruth/ (product) and benchmarks/swebench/gt_tool.py (eval)
must produce equivalent output for the same inputs through this interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class JudgmentObligation:
    """Normalized obligation format for parity comparison."""

    kind: str  # constructor_symmetry | override_contract | caller_contract | shared_state
    source: str  # symbol that changed
    target: str  # symbol that must also change
    target_file: str
    confidence: float


class JudgmentEngine(Protocol):
    """Protocol that both product and eval engines must satisfy."""

    def infer_obligations(
        self, symbol: str, file_context: str | None = None
    ) -> list[JudgmentObligation]:
        """Given a symbol, return normalized obligations."""
        ...

    def infer_from_diff(self, diff_text: str) -> list[JudgmentObligation]:
        """Given a diff, return normalized obligations."""
        ...


def normalize_obligation(
    kind: str, source: str, target: str, target_file: str, confidence: float
) -> JudgmentObligation:
    """Create a normalized obligation for comparison."""
    # Normalize file paths (strip leading ./ and /, use forward slashes)
    target_file = target_file.replace("\\", "/").lstrip("./")
    return JudgmentObligation(
        kind=kind,
        source=source,
        target=target,
        target_file=target_file,
        confidence=round(confidence, 2),
    )


class ProductJudgmentAdapter:
    """Adapts src/groundtruth ObligationEngine to JudgmentEngine protocol."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def infer_obligations(
        self, symbol: str, file_context: str | None = None
    ) -> list[JudgmentObligation]:
        obligations = self._engine.infer(symbol, file_context=file_context)
        return [
            normalize_obligation(o.kind, o.source, o.target, o.target_file, o.confidence)
            for o in obligations
        ]

    def infer_from_diff(self, diff_text: str) -> list[JudgmentObligation]:
        obligations = self._engine.infer_from_patch(diff_text)
        return [
            normalize_obligation(o.kind, o.source, o.target, o.target_file, o.confidence)
            for o in obligations
        ]
