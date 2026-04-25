"""Adapter functions — convert existing dataclasses to Finding[].

Each adapter is a standalone function. Existing dataclasses are NOT
modified. This preserves backward compatibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from groundtruth.schema.finding import (
    AgentAction,
    Finding,
    FindingKind,
    Location,
    Severity,
    WhyNow,
)

if TYPE_CHECKING:
    from groundtruth.evidence.change import ChangeEvidence
    from groundtruth.evidence.contract import CallerExpectation, TestExpectation
    from groundtruth.evidence.pattern import PatternEvidence
    from groundtruth.evidence.semantic.call_site_voting import SemanticEvidence
    from groundtruth.evidence.structural import StructuralEvidence
    from groundtruth.validators.contradictions import Contradiction
    from groundtruth.validators.obligations import Obligation


# ── Obligation ───────────────────────────────────────────────────────────────

_OBLIGATION_KIND_MAP: dict[str, FindingKind] = {
    "constructor_symmetry": FindingKind.CONSTRUCTOR_SYMMETRY,
    "override_contract": FindingKind.OVERRIDE_CONTRACT,
    "caller_contract": FindingKind.CALLER_CONTRACT,
    "shared_state": FindingKind.SHARED_STATE,
}


def obligation_to_finding(ob: Obligation) -> Finding:
    kind = _OBLIGATION_KIND_MAP.get(ob.kind, FindingKind.CALLER_CONTRACT)
    return Finding(
        kind=kind,
        severity=Severity.ERROR if ob.confidence >= 0.85 else Severity.WARNING,
        confidence=ob.confidence,
        location=Location(
            file=ob.target_file,
            line=ob.target_line,
            symbol=ob.target,
        ),
        message=ob.reason,
        evidence_locations=[Location(file=ob.target_file, symbol=ob.source)],
        why_now=WhyNow.PATCH_READY,
        agent_action=(
            AgentAction.FIX_REQUIRED
            if ob.confidence >= 0.85
            else AgentAction.VERIFY
        ),
        rule_id=f"GT-OBL-{ob.kind.upper()}",
    )


# ── Contradiction ────────────────────────────────────────────────────────────

_CONTRADICTION_KIND_MAP: dict[str, FindingKind] = {
    "override_violation": FindingKind.OVERRIDE_VIOLATION,
    "arity_mismatch": FindingKind.ARITY_MISMATCH,
    "import_path_moved": FindingKind.IMPORT_PATH_MOVED,
}


def contradiction_to_finding(c: Contradiction) -> Finding:
    kind = _CONTRADICTION_KIND_MAP.get(c.kind, FindingKind.ARITY_MISMATCH)
    return Finding(
        kind=kind,
        severity=Severity.ERROR,
        confidence=c.confidence,
        location=Location(file=c.file_path, line=c.line),
        message=c.message,
        why_now=WhyNow.PATCH_READY,
        agent_action=AgentAction.FIX_REQUIRED,
        rule_id=f"GT-CTR-{c.kind.upper()}",
    )


# ── ChangeEvidence ───────────────────────────────────────────────────────────

_CHANGE_KIND_MAP: dict[str, FindingKind] = {
    "guard_removed": FindingKind.GUARD_REMOVED,
    "exception_swallowed": FindingKind.EXCEPTION_SWALLOWED,
    "exception_broadened": FindingKind.EXCEPTION_BROADENED,
    "return_shape_changed": FindingKind.RETURN_SHAPE_CHANGED,
    "validation_removed": FindingKind.VALIDATION_REMOVED,
}


def change_evidence_to_finding(ce: ChangeEvidence) -> Finding:
    kind = _CHANGE_KIND_MAP.get(ce.kind, FindingKind.GUARD_REMOVED)
    return Finding(
        kind=kind,
        severity=Severity.ERROR if ce.confidence >= 0.85 else Severity.WARNING,
        confidence=ce.confidence,
        location=Location(file=ce.file_path, line=ce.line),
        message=ce.message,
        why_now=WhyNow.FILE_CHANGED,
        agent_action=(
            AgentAction.FIX_REQUIRED
            if ce.confidence >= 0.85
            else AgentAction.VERIFY
        ),
        rule_id=f"GT-CHG-{ce.kind.upper()}",
    )


# ── CallerExpectation ────────────────────────────────────────────────────────


def caller_expectation_to_finding(ce: CallerExpectation) -> Finding:
    return Finding(
        kind=FindingKind.CALLER_EXPECTATION,
        severity=Severity.WARNING if ce.confidence < 0.85 else Severity.ERROR,
        confidence=ce.confidence,
        location=Location(file=ce.file_path, line=ce.line),
        message=f"caller {ce.usage_type}: {ce.detail}",
        why_now=WhyNow.FILE_CHANGED,
        agent_action=AgentAction.VERIFY,
        rule_id="GT-CALLER-EXPECT",
    )


# ── TestExpectation ──────────────────────────────────────────────────────────


def assertion_expectation_to_finding(te: TestExpectation) -> Finding:
    return Finding(
        kind=FindingKind.TEST_ASSERTION,
        severity=Severity.WARNING,
        confidence=te.confidence,
        location=Location(
            file=te.test_file,
            line=te.line,
            symbol=te.test_func,
        ),
        message=f"{te.assertion_type}: expected {te.expected}",
        why_now=WhyNow.FILE_OPENED,
        agent_action=AgentAction.VERIFY,
        rule_id="GT-TEST-ASSERT",
    )


# ── SemanticEvidence ─────────────────────────────────────────────────────────

_SEMANTIC_KIND_MAP: dict[str, FindingKind] = {
    "call_site_voting": FindingKind.CALL_SITE_VOTING,
    "call_site_swap": FindingKind.CALL_SITE_SWAP,
    "arg_affinity": FindingKind.ARG_AFFINITY,
    "argument_swap": FindingKind.CALL_SITE_SWAP,
    "guard_consistency": FindingKind.GUARD_CONSISTENCY,
}


def semantic_evidence_to_finding(se: SemanticEvidence) -> Finding | None:
    kind = _SEMANTIC_KIND_MAP.get(se.kind)
    if kind is None:
        return None
    return Finding(
        kind=kind,
        severity=Severity.WARNING if se.confidence < 0.85 else Severity.ERROR,
        confidence=se.confidence,
        location=Location(file=se.file_path, line=se.line),
        message=se.message,
        why_now=WhyNow.PATCH_READY,
        agent_action=AgentAction.VERIFY,
        rule_id=f"GT-SEM-{se.kind.upper()}",
    )


# ── PatternEvidence ──────────────────────────────────────────────────────────

_PATTERN_KIND_MAP: dict[str, FindingKind] = {
    "missing_guard": FindingKind.GUARD_REMOVED,
    "return_shape_outlier": FindingKind.RETURN_SHAPE_CHANGED,
}


def pattern_evidence_to_finding(pe: PatternEvidence) -> Finding | None:
    """Convert PatternEvidence to Finding. Returns None for pruned kinds."""
    kind = _PATTERN_KIND_MAP.get(pe.kind)
    if kind is None:
        return None
    return Finding(
        kind=kind,
        severity=Severity.WARNING,
        confidence=pe.confidence,
        location=Location(file=pe.file_path, line=pe.line),
        message=pe.message,
        why_now=WhyNow.FILE_CHANGED,
        agent_action=AgentAction.VERIFY,
        rule_id=f"GT-PAT-{pe.kind.upper()}",
    )


# ── StructuralEvidence ───────────────────────────────────────────────────────

_STRUCTURAL_KIND_MAP: dict[str, FindingKind] = {
    "obligation": FindingKind.CALLER_CONTRACT,
    "contradiction": FindingKind.ARITY_MISMATCH,
}


def structural_evidence_to_finding(se: StructuralEvidence) -> Finding | None:
    """Convert StructuralEvidence to Finding. Returns None for pruned kinds."""
    kind = _STRUCTURAL_KIND_MAP.get(se.kind)
    if kind is None:
        return None
    return Finding(
        kind=kind,
        severity=Severity.WARNING if se.confidence < 0.85 else Severity.ERROR,
        confidence=se.confidence,
        location=Location(file=se.file_path, line=se.line),
        message=se.message,
        why_now=WhyNow.PATCH_READY,
        agent_action=AgentAction.VERIFY,
        rule_id=f"GT-STR-{se.kind.upper()}",
    )


# ── EvidenceNode (gt_intel.py) ───────────────────────────────────────────────

_EVIDENCE_FAMILY_MAP: dict[str, FindingKind] = {
    "IMPORT": FindingKind.IMPORT_PATH,
    "CALLER": FindingKind.CALLER_EXPECTATION,
    "SIBLING": FindingKind.CALLER_CONTRACT,
    "TEST": FindingKind.TEST_ASSERTION,
    "IMPACT": FindingKind.CALLER_CONTRACT,
    "TYPE": FindingKind.CALLER_EXPECTATION,
    "PRECEDENT": FindingKind.FILE_RELEVANCE,
}


def evidence_node_to_finding(
    en: object,
    *,
    confidence: float | None = None,
) -> Finding | None:
    """Convert gt_intel.py EvidenceNode to Finding.

    Accepts 'object' to avoid importing from benchmarks/ at module level.
    Expects attrs: family, score, name, file, line, source_code, summary,
    resolution_method.
    """
    family = getattr(en, "family", "")
    kind = _EVIDENCE_FAMILY_MAP.get(family)
    if kind is None:
        return None

    score = getattr(en, "score", 0)
    resolution = getattr(en, "resolution_method", None)

    if confidence is None:
        if resolution in ("same_file", "import"):
            confidence = min(1.0, 0.5 + score * 0.15)
        elif resolution == "name_match":
            confidence = min(1.0, 0.3 + score * 0.15)
        else:
            confidence = min(1.0, 0.4 + score * 0.15)

    why_now = WhyNow.FILE_OPENED if family in ("IMPORT", "TEST", "PRECEDENT") else WhyNow.FILE_CHANGED

    return Finding(
        kind=kind,
        severity=Severity.WARNING if confidence < 0.85 else Severity.ERROR,
        confidence=confidence,
        location=Location(
            file=getattr(en, "file", ""),
            line=getattr(en, "line", None),
            symbol=getattr(en, "name", None),
        ),
        message=getattr(en, "summary", ""),
        source_code=getattr(en, "source_code", None) or None,
        why_now=why_now,
        agent_action=AgentAction.VERIFY,
        rule_id=f"GT-EV-{family}",
    )
