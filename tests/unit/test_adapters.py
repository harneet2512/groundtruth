"""Tests for adapter functions — existing dataclasses to Finding[]."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from groundtruth.schema.adapters import (
    caller_expectation_to_finding,
    change_evidence_to_finding,
    contradiction_to_finding,
    evidence_node_to_finding,
    obligation_to_finding,
    pattern_evidence_to_finding,
    semantic_evidence_to_finding,
    structural_evidence_to_finding,
    assertion_expectation_to_finding,
)
from groundtruth.schema.finding import (
    AgentAction,
    FindingKind,
    Severity,
    WhyNow,
)


# ── Stub dataclasses matching existing shapes ────────────────────────────────
# Using stubs so tests don't depend on validator/evidence module internals.


@dataclass
class _Obligation:
    kind: str
    source: str
    target: str
    target_file: str
    target_line: int | None
    reason: str
    confidence: float


@dataclass(frozen=True)
class _Contradiction:
    kind: str
    file_path: str
    line: int | None
    message: str
    evidence: str
    confidence: float


@dataclass
class _ChangeEvidence:
    kind: str
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "change"


@dataclass
class _CallerExpectation:
    file_path: str
    line: int
    usage_type: str
    detail: str
    confidence: float
    family: str = "contract"


@dataclass
class _TestExpectation:
    test_file: str
    test_func: str
    line: int
    assertion_type: str
    expected: str
    confidence: float
    family: str = "contract"


@dataclass
class _SemanticEvidence:
    kind: str
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "semantic"


@dataclass
class _PatternEvidence:
    kind: str
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "pattern"


@dataclass
class _StructuralEvidence:
    kind: str
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "structural"


@dataclass
class _EvidenceNode:
    family: str
    score: int
    name: str
    file: str
    line: int
    source_code: str
    summary: str
    resolution_method: str | None = None


# ── Obligation ───────────────────────────────────────────────────────────────


class TestObligationAdapter:
    def test_constructor_symmetry(self) -> None:
        ob = _Obligation(
            kind="constructor_symmetry",
            source="__init__",
            target="__eq__",
            target_file="src/model.py",
            target_line=42,
            reason="__eq__ must mirror __init__ fields",
            confidence=0.9,
        )
        f = obligation_to_finding(ob)
        assert f.kind == FindingKind.CONSTRUCTOR_SYMMETRY
        assert f.severity == Severity.ERROR
        assert f.confidence == 0.9
        assert f.location.file == "src/model.py"
        assert f.location.line == 42
        assert f.location.symbol == "__eq__"
        assert f.why_now == WhyNow.PATCH_READY
        assert f.agent_action == AgentAction.FIX_REQUIRED
        assert f.rule_id == "GT-OBL-CONSTRUCTOR_SYMMETRY"

    def test_low_confidence_is_warning(self) -> None:
        ob = _Obligation(
            kind="shared_state",
            source="a",
            target="b",
            target_file="x.py",
            target_line=10,
            reason="coupled via self.x",
            confidence=0.6,
        )
        f = obligation_to_finding(ob)
        assert f.severity == Severity.WARNING
        assert f.agent_action == AgentAction.VERIFY

    def test_unknown_kind_defaults(self) -> None:
        ob = _Obligation(
            kind="unknown_future_kind",
            source="a",
            target="b",
            target_file="x.py",
            target_line=None,
            reason="reason",
            confidence=0.5,
        )
        f = obligation_to_finding(ob)
        assert f.kind == FindingKind.CALLER_CONTRACT

    def test_none_line(self) -> None:
        ob = _Obligation(
            kind="caller_contract",
            source="a",
            target="b",
            target_file="x.py",
            target_line=None,
            reason="reason",
            confidence=0.9,
        )
        f = obligation_to_finding(ob)
        assert f.location.line is None


# ── Contradiction ────────────────────────────────────────────────────────────


class TestContradictionAdapter:
    def test_override_violation(self) -> None:
        c = _Contradiction(
            kind="override_violation",
            file_path="handlers.py",
            line=15,
            message="SubHandler.process has 3 params vs base's 2",
            evidence="Base.process(self, request)",
            confidence=0.95,
        )
        f = contradiction_to_finding(c)
        assert f.kind == FindingKind.OVERRIDE_VIOLATION
        assert f.severity == Severity.ERROR
        assert f.agent_action == AgentAction.FIX_REQUIRED
        assert f.why_now == WhyNow.PATCH_READY

    def test_arity_mismatch(self) -> None:
        c = _Contradiction(
            kind="arity_mismatch",
            file_path="views.py",
            line=30,
            message="func called with 3 args, defined with 2",
            evidence="def func(a, b)",
            confidence=0.88,
        )
        f = contradiction_to_finding(c)
        assert f.kind == FindingKind.ARITY_MISMATCH


# ── ChangeEvidence ───────────────────────────────────────────────────────────


class TestChangeEvidenceAdapter:
    def test_guard_removed(self) -> None:
        ce = _ChangeEvidence(
            kind="guard_removed",
            file_path="src/model.py",
            line=42,
            message="safety check removed",
            confidence=0.8,
        )
        f = change_evidence_to_finding(ce)
        assert f.kind == FindingKind.GUARD_REMOVED
        assert f.why_now == WhyNow.FILE_CHANGED
        assert f.severity == Severity.WARNING

    def test_high_confidence_is_error(self) -> None:
        ce = _ChangeEvidence(
            kind="exception_swallowed",
            file_path="x.py",
            line=10,
            message="except clause now bare",
            confidence=0.9,
        )
        f = change_evidence_to_finding(ce)
        assert f.severity == Severity.ERROR
        assert f.agent_action == AgentAction.FIX_REQUIRED

    def test_all_change_kinds(self) -> None:
        for kind in [
            "guard_removed",
            "exception_swallowed",
            "exception_broadened",
            "return_shape_changed",
            "validation_removed",
        ]:
            ce = _ChangeEvidence(kind=kind, file_path="x.py", line=1, message="m", confidence=0.7)
            f = change_evidence_to_finding(ce)
            assert f.kind.value == kind


# ── CallerExpectation ────────────────────────────────────────────────────────


class TestCallerExpectationAdapter:
    def test_basic(self) -> None:
        ce = _CallerExpectation(
            file_path="handlers.py",
            line=87,
            usage_type="destructure_tuple",
            detail="a, b = func()",
            confidence=0.75,
        )
        f = caller_expectation_to_finding(ce)
        assert f.kind == FindingKind.CALLER_EXPECTATION
        assert "destructure_tuple" in f.message
        assert f.why_now == WhyNow.FILE_CHANGED


# ── TestExpectation ──────────────────────────────────────────────────────────


class TestTestExpectationAdapter:
    def test_basic(self) -> None:
        te = _TestExpectation(
            test_file="tests/test_model.py",
            test_func="test_encrypt_padding",
            line=23,
            assertion_type="assertEqual",
            expected="16",
            confidence=0.85,
        )
        f = assertion_expectation_to_finding(te)
        assert f.kind == FindingKind.TEST_ASSERTION
        assert f.location.file == "tests/test_model.py"
        assert f.location.symbol == "test_encrypt_padding"
        assert "assertEqual" in f.message
        assert f.why_now == WhyNow.FILE_OPENED


# ── SemanticEvidence ─────────────────────────────────────────────────────────


class TestSemanticEvidenceAdapter:
    def test_call_site_voting(self) -> None:
        se = _SemanticEvidence(
            kind="call_site_voting",
            file_path="views.py",
            line=42,
            message="8/10 call sites pass user_id at pos 1",
            confidence=0.72,
        )
        f = semantic_evidence_to_finding(se)
        assert f is not None
        assert f.kind == FindingKind.CALL_SITE_VOTING
        assert f.why_now == WhyNow.PATCH_READY

    def test_argument_swap(self) -> None:
        se = _SemanticEvidence(
            kind="argument_swap",
            file_path="x.py",
            line=10,
            message="swap detected",
            confidence=0.8,
        )
        f = semantic_evidence_to_finding(se)
        assert f is not None
        assert f.kind == FindingKind.CALL_SITE_SWAP

    def test_unknown_kind_returns_none(self) -> None:
        se = _SemanticEvidence(
            kind="unknown_signal",
            file_path="x.py",
            line=1,
            message="m",
            confidence=0.5,
        )
        assert semantic_evidence_to_finding(se) is None


# ── PatternEvidence ──────────────────────────────────────────────────────────


class TestPatternEvidenceAdapter:
    def test_missing_guard(self) -> None:
        pe = _PatternEvidence(
            kind="missing_guard",
            file_path="x.py",
            line=5,
            message="siblings guard but this doesn't",
            confidence=0.7,
        )
        f = pattern_evidence_to_finding(pe)
        assert f is not None
        assert f.kind == FindingKind.GUARD_REMOVED

    def test_pruned_kind_returns_none(self) -> None:
        pe = _PatternEvidence(
            kind="missing_docstring",
            file_path="x.py",
            line=5,
            message="no docstring",
            confidence=0.8,
        )
        assert pattern_evidence_to_finding(pe) is None


# ── StructuralEvidence ───────────────────────────────────────────────────────


class TestStructuralEvidenceAdapter:
    def test_obligation(self) -> None:
        se = _StructuralEvidence(
            kind="obligation",
            file_path="x.py",
            line=5,
            message="must update __eq__",
            confidence=0.9,
        )
        f = structural_evidence_to_finding(se)
        assert f is not None
        assert f.kind == FindingKind.CALLER_CONTRACT

    def test_convention_pruned(self) -> None:
        se = _StructuralEvidence(
            kind="convention",
            file_path="x.py",
            line=5,
            message="naming convention",
            confidence=0.9,
        )
        assert structural_evidence_to_finding(se) is None


# ── EvidenceNode ─────────────────────────────────────────────────────────────


class TestEvidenceNodeAdapter:
    def test_import_family(self) -> None:
        en = _EvidenceNode(
            family="IMPORT",
            score=2,
            name="pad_block",
            file="crypto/utils.py",
            line=10,
            source_code="from crypto.utils import pad_block",
            summary="USE: from crypto.utils import pad_block",
            resolution_method="import",
        )
        f = evidence_node_to_finding(en)
        assert f is not None
        assert f.kind == FindingKind.IMPORT_PATH
        assert f.why_now == WhyNow.FILE_OPENED

    def test_caller_family(self) -> None:
        en = _EvidenceNode(
            family="CALLER",
            score=3,
            name="decrypt",
            file="handlers.py",
            line=87,
            source_code="a, b = encrypt(data)",
            summary="decrypt destructures return",
            resolution_method="same_file",
        )
        f = evidence_node_to_finding(en)
        assert f is not None
        assert f.kind == FindingKind.CALLER_EXPECTATION

    def test_unknown_family_returns_none(self) -> None:
        en = _EvidenceNode(
            family="UNKNOWN",
            score=1,
            name="x",
            file="x.py",
            line=1,
            source_code="",
            summary="",
        )
        assert evidence_node_to_finding(en) is None

    def test_explicit_confidence(self) -> None:
        en = _EvidenceNode(
            family="TEST",
            score=2,
            name="test_enc",
            file="tests/t.py",
            line=5,
            source_code="assert x == 16",
            summary="assertEqual: 16",
        )
        f = evidence_node_to_finding(en, confidence=0.99)
        assert f is not None
        assert f.confidence == 0.99
