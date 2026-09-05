"""Do-no-harm safety governor — pure functions for delivery decisions.

GroundTruth is an assistive context layer, not a controller.
Default safe behavior is silence.
If GT lacks high-confidence, actionable, task-relevant evidence,
it must suppress agent-visible output and log why.

Research basis:
- R1 SWE-agent: interface design affects agent performance
- R2 Agentless: structured evidence, not controller
- R5 Lost-in-the-Middle: long/noisy context hurts
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DeliveryDecision:
    status: str
    reason: str

    @property
    def should_deliver(self) -> bool:
        return self.status == "DELIVERED_VISIBLE"


# Delivery statuses
DELIVERED_VISIBLE = "DELIVERED_VISIBLE"
SUPPRESSED_LOW_CONFIDENCE = "SUPPRESSED_LOW_CONFIDENCE"
SUPPRESSED_NO_EVIDENCE = "SUPPRESSED_NO_EVIDENCE"
SUPPRESSED_NOT_ACTIONABLE = "SUPPRESSED_NOT_ACTIONABLE"
SUPPRESSED_NOISE_RISK = "SUPPRESSED_NOISE_RISK"
SUPPRESSED_NO_EDITED_FUNCTION = "SUPPRESSED_NO_EDITED_FUNCTION"
FAILED_REASON = "FAILED_REASON"


def should_suppress_completeness(
    edited_functions: set[str] | None,
) -> DeliveryDecision:
    """Decide whether completeness evidence should be suppressed.

    - edited_functions=None → legacy all-pairs mode (backward compat)
    - edited_functions=set() → GT tried to extract but failed; suppress
    - edited_functions={"name"} → scoped, allow
    """
    if edited_functions is not None and not edited_functions:
        return DeliveryDecision(
            status=SUPPRESSED_NO_EDITED_FUNCTION,
            reason="Edited function identity unknown — suppressing class-wide noise",
        )
    return DeliveryDecision(
        status=DELIVERED_VISIBLE,
        reason="Completeness scoped to edited function" if edited_functions else "Legacy all-pairs mode",
    )


def should_suppress_l5(
    target_file: str,
    recent_l5_targets: list[str],
    lookback: int = 5,
) -> DeliveryDecision:
    """Decide whether L5 ignored-witness should be suppressed.

    Suppress if the same target file was already suggested within
    the last `lookback` L5 messages.
    """
    recent = recent_l5_targets[-lookback:] if recent_l5_targets else []
    if target_file in recent:
        return DeliveryDecision(
            status=SUPPRESSED_NOISE_RISK,
            reason=f"L5 already suggested {target_file} within last {lookback} messages",
        )
    return DeliveryDecision(
        status=DELIVERED_VISIBLE,
        reason="L5 suggestion for new target",
    )


def classify_finish_delivery(
    layer: str,
) -> DeliveryDecision:
    """Classify delivery in finish handler as dead write."""
    return DeliveryDecision(
        status=SUPPRESSED_NOT_ACTIONABLE,
        reason=f"{layer} evidence generated after AgentState.FINISHED — agent cannot act",
    )


def should_emit_l4a(
    symbol_name: str,
    symbol_callers: int,
    is_l1_candidate_file: bool,
    has_issue_keyword_match: bool,
    hub_threshold: int = 50,
) -> DeliveryDecision:
    """Decide whether L4a auto-query should show a symbol.

    Allow when:
    - symbol has issue-keyword relevance, OR
    - viewed file is a high-confidence L1 candidate, OR
    - symbol has reliable callers (not a generic hub)

    Suppress when:
    - symbol is a high-degree hub AND no issue/file relevance
    """
    if has_issue_keyword_match:
        return DeliveryDecision(DELIVERED_VISIBLE, "Issue keyword match")

    if is_l1_candidate_file:
        return DeliveryDecision(DELIVERED_VISIBLE, "File is L1 candidate")

    if symbol_callers > hub_threshold:
        return DeliveryDecision(
            SUPPRESSED_NOISE_RISK,
            f"Generic hub ({symbol_callers} callers) without issue/file relevance",
        )

    if symbol_callers > 0:
        return DeliveryDecision(DELIVERED_VISIBLE, f"Structural relevance ({symbol_callers} callers)")

    return DeliveryDecision(SUPPRESSED_NO_EVIDENCE, "No callers, no issue match, no file relevance")
