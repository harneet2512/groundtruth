from __future__ import annotations

from dataclasses import replace

import pytest

from groundtruth.runtime import evidence_envelope as ee
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.canonical_producers import (
    ProducerContext,
    SubmitEvidenceOwner,
    produce_covering_red,
    produce_recovery,
    produce_submit_refusal,
    produce_syntax_result,
)
from groundtruth.runtime.covering_runner import CoveringAttribution
from groundtruth.runtime.hypothesis_ledger import (
    Advisory,
    D_HYPOTHESIS_FALSIFIED,
    D_NOT_A_LOOP,
    T_EDIT_CONTRADICTED_CONTRACT,
    T_SAME_COMMAND_NEW_OUTPUT,
)
from groundtruth.runtime.submit_gate import GateVerdict, build_certificate


REVISION = rr.RevisionVector(
    repository_content="repo-content-7",
    graph="graph-7",
    lsp="lsp-7",
    runtime_evidence="runtime-7",
)


def _context(
    *,
    subject: str = "src/auth/session.py::refresh_session",
    path: str = "src/auth/session.py",
    line: int = 41,
) -> ProducerContext:
    return ProducerContext(
        subject=subject,
        provenance=((path, line),),
        revision=REVISION,
        decision_id="decision-7",
        causal_neighborhood=("obligation:session-rotation", "path:auth-session"),
    )


def _covering_attribution(*, attributed: bool = True) -> CoveringAttribution:
    return CoveringAttribution(
        attributed=attributed,
        method="unresolved_covering",
        current_verdict="fail",
        base_verdict="fail",
        implicated_edited_paths=("src/auth/session.py",),
        covering_files=("tests/auth/test_session.py",),
    )


def _covering_result(**overrides):
    result = {
        "executed": True,
        "verdict": "fail",
        "reason": "test_failure",
        "files": ["tests/auth/test_session.py"],
        "ran": ["tests/auth/test_session.py"],
        "command": ["pytest", "-q", "tests/auth/test_session.py"],
        "stdout_tail": "1 failed",
        "stderr_tail": "",
        "exit_code": 1,
        "failing_test_names": ["test_rotated_session_is_returned"],
    }
    result.update(overrides)
    return result


def _blocking_verdict() -> GateVerdict:
    return GateVerdict(
        allow=False,
        reason="covering_test_failed",
        detail="a covering test is failing",
        record={
            "block": "covering_test_failed",
            "covering_verdict": "fail",
            "covering_failing_names": ["test_rotated_session_is_returned"],
        },
    )


def test_covering_red_requires_executed_attributable_structured_failure() -> None:
    envelope = produce_covering_red(
        context=_context(),
        result=_covering_result(),
        attribution=_covering_attribution(),
    )

    assert envelope is not None
    assert envelope.producer == "covering_runner"
    assert envelope.evidence_type == "covering_red"
    assert envelope.tier == ee.VERIFIED
    assert envelope.provenance == (("src/auth/session.py", 41),)
    assert "test_rotated_session_is_returned" not in "\n".join(envelope.payload)
    assert "tests/auth/test_session.py" not in "\n".join(envelope.payload)
    assert ee.validate(envelope) == []

    record = rr.canonical_evidence_from_envelope(envelope)
    assert record is not None
    assert record.feature_id == "covering_red"
    assert record.owner_feature_ids == ()
    assert record.roles == (rr.EvidenceRole.BLOCKER, rr.EvidenceRole.VALIDATION)
    assert record.mandatory_reason is rr.MandatoryReason.BLOCKER
    assert record.revision == REVISION


@pytest.mark.parametrize(
    ("result", "attribution"),
    [
        (_covering_result(executed=False), _covering_attribution()),
        (_covering_result(verdict="pass", exit_code=0), _covering_attribution()),
        (_covering_result(), _covering_attribution(attributed=False)),
        (
            _covering_result(),
            replace(
                _covering_attribution(),
                implicated_edited_paths=("src/other.py",),
            ),
        ),
        (
            _covering_result(files=["tests/other.py"], ran=["tests/other.py"]),
            _covering_attribution(),
        ),
    ],
)
def test_covering_red_abstains_without_complete_attribution(result, attribution) -> None:
    assert (
        produce_covering_red(
            context=_context(),
            result=result,
            attribution=attribution,
        )
        is None
    )


def test_syntax_result_owns_only_gt_edit_check_and_preserves_native_diagnostic() -> None:
    result = {
        "verdict": "syntax_error",
        "diagnostic": (
            'File "src/auth/session.py", line 41\n'
            "    if token:\n"
            "             ^\n"
            "SyntaxError: expected expression"
        ),
        "language": ".py",
        "reason": "parse_error",
        "checker": ["ast.parse"],
    }

    envelope = produce_syntax_result(context=_context(), result=result)

    assert envelope is not None
    assert envelope.payload == (result["diagnostic"],)
    assert envelope.lineage is not None
    assert ee.validate(envelope) == []
    record = rr.canonical_evidence_from_envelope(envelope)
    assert record is not None
    assert record.feature_id == "syntax_result"
    assert record.owner_feature_ids == ("GT_EDIT_CHECK",)
    assert record.grade is rr.EvidenceGrade.VERIFIED
    assert record.authority is rr.Authority.RESULT_DERIVED
    assert record.revision_dependencies == ("edit_rev",)

    active = rr.ActiveDecision(
        decision_id="decision-7",
        context=rr.DecisionContext.PATCH_PROPAGATION,
        primary_claim="Repair the current edit without propagating invalid syntax.",
        required_roles=record.roles,
        causal_neighborhood=("path:auth-session",),
        token_budget=220,
        current_revision=REVISION,
    )
    contract = rr.feature_contract_for("syntax_result")
    assert contract is not None
    evaluation = rr.evaluate_feature_contract(
        contract,
        record,
        rr.TemporalRuntimeContext(
            active_decision=active,
            satisfied_predicates=frozenset(
                {
                    rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
                    rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
                }
            ),
            commitment_window=rr.CommitmentWindowState.OPEN,
            current_revision=REVISION,
            available_substrates=("parser_result",),
        ),
    )
    assert evaluation.relevant is True
    assert evaluation.release_allowed is True


@pytest.mark.parametrize(
    "result",
    [
        {
            "verdict": "unavailable",
            "diagnostic": "",
            "reason": "unsupported_language",
            "checker": [],
        },
        {
            "verdict": "syntax_error",
            "diagnostic": "",
            "reason": "parse_error",
            "checker": ["ast.parse"],
        },
        {
            "verdict": "syntax_error",
            "diagnostic": "SyntaxError: invalid syntax",
            "reason": "parse_error",
            "checker": [],
        },
    ],
)
def test_syntax_result_abstains_without_positive_parser_evidence(result) -> None:
    assert produce_syntax_result(context=_context(), result=result) is None


def test_recovery_converts_valid_typed_advisory_without_upgrading_confidence() -> None:
    advisory = Advisory(
        transition=T_EDIT_CONTRADICTED_CONTRACT,
        disposition=D_HYPOTHESIS_FALSIFIED,
        tier=ee.WARNING,
        blocking_eligibility=ee.ADVISORY,
        statement=(
            'the failure fingerprint "fp-7" recurred after source edit 4; '
            "the operational hypothesis is contradicted."
        ),
        evidence_ids=("fingerprint:fp-7", "edit:4"),
    )

    envelope = produce_recovery(context=_context(), advisory=advisory)

    assert envelope is not None
    assert envelope.tier == ee.WARNING
    assert envelope.blocking_eligibility == ee.ADVISORY
    record = rr.canonical_evidence_from_envelope(envelope)
    assert record is not None
    assert record.feature_id == "recovery"
    assert record.owner_feature_ids == ("GT_HYPOTHESIS",)
    assert record.grade is rr.EvidenceGrade.WARNING
    assert record.mandatory_reason is None
    assert record.roles == (
        rr.EvidenceRole.CONTRADICTION,
        rr.EvidenceRole.VALIDATION,
    )


def test_recovery_abstains_for_nonactionable_progress_or_invalid_advisory() -> None:
    progress = Advisory(
        transition=T_SAME_COMMAND_NEW_OUTPUT,
        disposition=D_NOT_A_LOOP,
        tier=ee.INFO,
        blocking_eligibility=ee.ADVISORY,
        statement="the probe produced new output; this is progress.",
        evidence_ids=("probe:x:1",),
    )
    invalid = replace(
        progress,
        transition=T_EDIT_CONTRADICTED_CONTRACT,
        disposition=D_HYPOTHESIS_FALSIFIED,
        tier=ee.VERIFIED,
    )

    assert produce_recovery(context=_context(), advisory=progress) is None
    assert produce_recovery(context=_context(), advisory=invalid) is None


@pytest.mark.parametrize(
    ("owner", "expected_owner"),
    [
        (SubmitEvidenceOwner.REFUSAL, "GT_SS_SUBMIT_RED"),
        (SubmitEvidenceOwner.CERTIFICATE, "GT_CERT_DELIVERY"),
    ],
)
def test_submit_refusal_assigns_only_the_physical_output_owner(
    owner: SubmitEvidenceOwner,
    expected_owner: str,
) -> None:
    verdict = _blocking_verdict()
    certificate = build_certificate(
        head=verdict,
        submit_revision=REVISION.repository_content,
        covering={
            "executed": True,
            "verdict": "fail",
            "reason": "test_failure",
            "patch_revision": REVISION.repository_content,
        },
    )

    envelope = produce_submit_refusal(
        context=_context(),
        verdict=verdict,
        certificate=certificate,
        output_owner=owner,
    )

    assert envelope is not None
    assert "test_rotated_session_is_returned" not in "\n".join(envelope.payload)
    record = rr.canonical_evidence_from_envelope(envelope)
    assert record is not None
    assert record.feature_id == "submit_refusal"
    assert record.owner_feature_ids == (expected_owner,)
    assert record.mandatory_reason is rr.MandatoryReason.BLOCKER
    assert record.grade is rr.EvidenceGrade.VERIFIED


def test_submit_refusal_abstains_on_allow_or_inconsistent_certificate() -> None:
    allowed = GateVerdict(True, "clean", "", {"allow": True})
    blocked = _blocking_verdict()
    allow_certificate = build_certificate(
        head=allowed,
        submit_revision=REVISION.repository_content,
    )

    assert (
        produce_submit_refusal(
            context=_context(),
            verdict=allowed,
            certificate=allow_certificate,
            output_owner=SubmitEvidenceOwner.CERTIFICATE,
        )
        is None
    )
    assert (
        produce_submit_refusal(
            context=_context(),
            verdict=blocked,
            certificate=allow_certificate,
            output_owner=SubmitEvidenceOwner.CERTIFICATE,
        )
        is None
    )


def test_submit_output_forms_share_one_physical_fact_identity() -> None:
    verdict = _blocking_verdict()
    certificate = build_certificate(
        head=verdict,
        submit_revision=REVISION.repository_content,
        covering={
            "executed": True,
            "verdict": "fail",
            "reason": "test_failure",
            "patch_revision": REVISION.repository_content,
        },
    )
    refusal = produce_submit_refusal(
        context=_context(),
        verdict=verdict,
        certificate=certificate,
        output_owner=SubmitEvidenceOwner.REFUSAL,
    )
    certified = produce_submit_refusal(
        context=_context(),
        verdict=verdict,
        certificate=certificate,
        output_owner=SubmitEvidenceOwner.CERTIFICATE,
    )

    assert refusal is not None and certified is not None
    assert refusal.dedup_key == certified.dedup_key
    records = rr.canonicalize_evidence_envelopes((refusal, certified))
    assert len(records) == 1
    assert records[0].owner_feature_ids == (
        "GT_CERT_DELIVERY",
        "GT_SS_SUBMIT_RED",
    )


def test_all_producers_abstain_on_test_only_provenance() -> None:
    context = _context(
        subject="tests/auth/test_session.py::test_rotation",
        path="tests/auth/test_session.py",
    )
    advisory = Advisory(
        transition=T_EDIT_CONTRADICTED_CONTRACT,
        disposition=D_HYPOTHESIS_FALSIFIED,
        tier=ee.WARNING,
        blocking_eligibility=ee.ADVISORY,
        statement="the failure recurred after an edit.",
        evidence_ids=("fingerprint:fp", "edit:1"),
    )

    assert produce_syntax_result(
        context=context,
        result={
            "verdict": "syntax_error",
            "diagnostic": "SyntaxError: invalid syntax",
            "reason": "parse_error",
            "checker": ["ast.parse"],
        },
    ) is None
    assert produce_recovery(context=context, advisory=advisory) is None
    assert produce_submit_refusal(
        context=context,
        verdict=_blocking_verdict(),
        output_owner=SubmitEvidenceOwner.REFUSAL,
    ) is None


def test_producers_abstain_on_crossed_source_provenance_or_unstable_input() -> None:
    crossed = _context(path="src/auth/other.py")
    poisoned = _covering_result(extra=object())

    assert produce_syntax_result(
        context=crossed,
        result={
            "verdict": "syntax_error",
            "diagnostic": "SyntaxError: invalid syntax",
            "reason": "parse_error",
            "checker": ["ast.parse"],
        },
    ) is None
    assert produce_covering_red(
        context=_context(),
        result=poisoned,
        attribution=_covering_attribution(),
    ) is None


def test_production_is_deterministic_and_never_renders_internal_gt_blocks() -> None:
    first = produce_syntax_result(
        context=_context(),
        result={
            "verdict": "name_error",
            "diagnostic": "src/auth/session.py:41: undefined name 'token_store'",
            "language": ".py",
            "reason": "undefined_name",
            "checker": ["ast.parse", "pyflakes"],
        },
    )
    second = produce_syntax_result(
        context=_context(),
        result={
            "checker": ["ast.parse", "pyflakes"],
            "reason": "undefined_name",
            "language": ".py",
            "diagnostic": "src/auth/session.py:41: undefined name 'token_store'",
            "verdict": "name_error",
        },
    )

    assert first is not None and second is not None
    assert first == second
    assert first.dedup_key == second.dedup_key
    assert "<gt-" not in "\n".join(first.payload).lower()
