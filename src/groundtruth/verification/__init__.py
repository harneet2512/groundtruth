"""Verification and Patch Reranker — scores candidate edits against contracts.

Checks patches against mined contracts, selects targeted tests, scores
maintainability, and returns ranked verdicts with reason codes.
"""

from groundtruth.verification.models import (
    PatchCandidate,
    VerificationResult,
    ViolationRecord,
)

__all__ = [
    "PatchCandidate",
    "VerificationResult",
    "ViolationRecord",
]
