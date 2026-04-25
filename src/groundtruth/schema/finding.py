"""Unified Finding model — single schema for all GroundTruth signals.

SARIF-inspired internal representation, text serialization for agents.
Research basis: Reflexion, Self-Refine validate text with structural
markers over pure JSON for agent feedback.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class FindingKind(str, Enum):
    """Closed enum of high-value deterministic signal kinds."""

    # Obligations
    CONSTRUCTOR_SYMMETRY = "constructor_symmetry"
    OVERRIDE_CONTRACT = "override_contract"
    CALLER_CONTRACT = "caller_contract"
    SHARED_STATE = "shared_state"
    # Contradictions
    OVERRIDE_VIOLATION = "override_violation"
    ARITY_MISMATCH = "arity_mismatch"
    IMPORT_PATH_MOVED = "import_path_moved"
    # Localization
    FILE_RELEVANCE = "file_relevance"
    IMPORT_PATH = "import_path"
    # Call-site signals
    CALL_SITE_VOTING = "call_site_voting"
    CALL_SITE_SWAP = "call_site_swap"
    ARG_AFFINITY = "arg_affinity"
    # Change detection
    GUARD_REMOVED = "guard_removed"
    EXCEPTION_SWALLOWED = "exception_swallowed"
    EXCEPTION_BROADENED = "exception_broadened"
    RETURN_SHAPE_CHANGED = "return_shape_changed"
    VALIDATION_REMOVED = "validation_removed"
    # Contract / test
    CALLER_EXPECTATION = "caller_expectation"
    TEST_ASSERTION = "test_assertion"
    # Guard
    GUARD_CONSISTENCY = "guard_consistency"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


class WhyNow(str, Enum):
    FILE_OPENED = "file_opened"
    FILE_CHANGED = "file_changed"
    PATCH_READY = "patch_ready"
    ALWAYS = "always"


class AgentAction(str, Enum):
    FIX_REQUIRED = "fix_required"
    VERIFY = "verify"
    READ = "read"
    ACKNOWLEDGE = "acknowledge"


class Location(BaseModel):
    """File + optional line + optional symbol reference."""

    file: str = Field(min_length=1)
    line: int | None = None
    symbol: str | None = None


class Finding(BaseModel):
    """Single structured finding emitted by any GT engine."""

    kind: FindingKind
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    location: Location
    message: str
    evidence_locations: list[Location] = Field(default_factory=list)
    why_now: WhyNow = WhyNow.ALWAYS
    agent_action: AgentAction = AgentAction.VERIFY
    novelty: bool = True
    source_code: str | None = None
    rule_id: str | None = None

    def to_text_line(self) -> str:
        """Agent-facing text with structural markers."""
        if self.confidence >= 0.85:
            tier = "VERIFIED"
        elif self.confidence >= 0.6:
            tier = "WARNING"
        else:
            tier = "INFO"
        loc = (
            f"{self.location.file}:{self.location.line}"
            if self.location.line
            else self.location.file
        )
        action = self.agent_action.value.upper().replace("_", " ")
        return f"[{tier}] [{self.kind.value}] {self.message} @ {loc} ({self.confidence:.2f}) — {action}"


def format_findings(
    findings: list[Finding],
    surface: str,
    *,
    include_binding: bool = False,
) -> str:
    """Format a list of findings as agent-facing text block.

    Returns empty string if no findings (silent when nothing to say).
    """
    if not findings:
        return ""
    lines = [f'<gt-evidence surface="{surface}">']
    for f in findings:
        lines.append(f.to_text_line())
    if include_binding:
        binding_count = sum(
            1
            for f in findings
            if f.agent_action == AgentAction.FIX_REQUIRED
        )
        if binding_count > 0:
            lines.append("---")
            lines.append(
                f"BINDING: {binding_count} finding(s) require explicit fix or ACK before submit."
            )
    lines.append("</gt-evidence>")
    return "\n".join(lines)
