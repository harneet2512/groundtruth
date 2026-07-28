"""RED contract: the temporal gate must honour role-driven eligibility too.

THE TRAP THIS FILE EXISTS TO CATCH.  ``AttemptReasoningRuntime`` runs
``evaluate_feature_contract`` BEFORE ``select_evidence_coalition``.  Evidence judged
``relevant=False`` there is downgraded to ``HELD``, and the composer then rejects any
non-READY item as ``NOT_READY`` -- before its roles are ever examined.

So threading ``role_driven`` into the composer ALONE changes nothing in production: the
upstream gate has already dropped the evidence.  The change would look wired, pass its own
unit tests, and deliver nothing.  That is the same failure shape as the dead ``viewed_files``
path (Wave 15) and the untested ``:4462`` edit (reverted after a mutation survived).

Both stages must agree on what eligibility means. These tests drive the REAL
``evaluate_feature_contract`` and then the REAL ``select_evidence_coalition`` on its output,
exactly as the runtime does.

NO BAR IS WEAKENED. Under role-driven eligibility a record must still fit the decision's
declared roles, be fresh, be connected, be unseen, and the coalition must still carry the
REQUIRED role to complete. Default OFF => byte-identical.
"""

from __future__ import annotations

import dataclasses

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)

SATISFIED = frozenset(
    {
        rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
        rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
        rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
        rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
        rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
        rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
    }
)


def _decision_in_phase(phase: rr.Phase) -> rr.ActiveDecision:
    work_state = dataclasses.replace(
        rr.WorkState.initial(attempt_id="attempt-1", revision=REVISION),
        phase=phase,
    )
    return seam.CanonicalRuntimeAttachment._active_decision(
        (), work_state, REVISION, ()
    )


def _record(feature_id: str, decision: rr.ActiveDecision, subject: str) -> rr.EvidenceRecord:
    contract = rr.feature_contract_for(feature_id)
    return rr.EvidenceRecord(
        evidence_id=f"ev-{feature_id}",
        feature_id=feature_id,
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject=subject,
        claim=f"{feature_id} claim about {subject}",
        actionable_consequence=f"act on the {feature_id} finding for {subject}",
        provenance=(f"{subject}:7",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=(
            f"subject:{subject}",
            f"decision:{decision.decision_id}",
            "obligation:task",
        ),
        lifecycle=rr.EvidenceLifecycle.READY,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        revision_dependencies=contract.revision_dependencies,
        token_cost=120,
        failure_prevention=3,
        causal_value=3,
        contradiction_resolution=0,
        anchoring_risk=0,
        observed_substrates=tuple(
            sorted(contract.fallback_policy.preferred_substrates)
        ),
    )


def _gate(record, decision, *, role_driven):
    return rr.evaluate_feature_contract(
        rr.feature_contract_for(record.feature_id),
        record,
        rr.TemporalRuntimeContext(
            active_decision=decision,
            satisfied_predicates=SATISFIED,
            commitment_window=rr.CommitmentWindowState.OPEN,
            current_revision=REVISION,
            available_substrates=rr.feature_contract_for(
                record.feature_id
            ).fallback_policy.preferred_substrates,
        ),
        role_driven=role_driven,
    )


def _through_runtime(records, decision, *, role_driven):
    """Reproduce the runtime's two-stage pipeline: temporal gate, then composer."""
    scheduled = []
    for record in records:
        evaluation = _gate(record, decision, role_driven=role_driven)
        scheduled.append(
            dataclasses.replace(
                record,
                lifecycle=(
                    rr.EvidenceLifecycle.READY
                    if evaluation.release_allowed
                    else rr.EvidenceLifecycle.HELD
                ),
            )
        )
    return rr.select_evidence_coalition(decision, scheduled, role_driven=role_driven)


# ------------------------------------------------------------------ the trap itself
def test_temporal_gate_holds_out_of_context_evidence_by_default():
    """Byte-identical guard."""
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    record = _record("covering_red", decision, "tests/test_api.py")

    evaluation = _gate(record, decision, role_driven=False)

    assert evaluation.relevant is False
    assert evaluation.release_allowed is False


def test_temporal_gate_releases_out_of_context_evidence_that_fits_the_roles():
    """covering_red carries VALIDATION, which PATCH_CONSTRUCTION declares USEFUL."""
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    record = _record("covering_red", decision, "tests/test_api.py")

    evaluation = _gate(record, decision, role_driven=True)

    assert evaluation.relevant is True
    assert evaluation.release_allowed is True


def test_gate_and_composer_agree_end_to_end():
    """THE regression this file exists for.

    If the gate and the composer disagree, the evidence is HELD upstream and the composer's
    role logic never runs -- the change would be invisible in production.
    """
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    records = [
        _record("caller_contract", decision, "src/api.py"),
        _record("covering_red", decision, "tests/test_api.py"),
    ]

    partitioned = _through_runtime(records, decision, role_driven=False)
    role_driven = _through_runtime(records, decision, role_driven=True)

    assert {i.feature_id for i in partitioned.coalition} == {"caller_contract"}
    assert {i.feature_id for i in role_driven.coalition} == {
        "caller_contract",
        "covering_red",
    }
    assert set(partitioned.coverage) < set(role_driven.coverage)


def test_gate_still_rejects_roles_that_do_not_fit():
    """Eligibility moved to roles; it did not become unconditional.

    submit_refusal carries BLOCKER/TERMINAL_ASSURANCE -- neither is required or useful for
    PATCH_CONSTRUCTION -- so role fit genuinely fails rather than being waved through.
    """
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    record = _record("submit_refusal", decision, "src/api.py")
    assert not set(record.roles) & (
        set(decision.required_roles) | set(decision.useful_roles)
    )

    assert _gate(record, decision, role_driven=True).relevant is False


def test_gate_still_requires_causal_connection():
    """Role fit alone is not relevance -- the evidence must be about this decision.

    Same roles, unrelated subject and no shared decision node: still irrelevant.
    """
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    contract = rr.feature_contract_for("covering_red")
    stranger = dataclasses.replace(
        _record("covering_red", decision, "tests/test_api.py"),
        causal_neighborhood=("subject:totally/unrelated.py",),
    )
    assert stranger.roles == contract.roles

    assert _gate(stranger, decision, role_driven=True).relevant is False


def test_anchor_requirement_survives_the_full_pipeline():
    """NO-WEAKENING PROOF at the pipeline level, not just the composer."""
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    outcome = _through_runtime(
        [_record("covering_red", decision, "tests/test_api.py")],
        decision,
        role_driven=True,
    )

    assert outcome.decision_complete is False
    assert outcome.release_allowed is False
    assert rr.EvidenceRole.BEHAVIORAL_CONTRACT in outcome.unresolved_roles


@pytest.mark.parametrize(
    "feature_id,phase",
    [
        ("caller_contract", rr.Phase.UNDERSTANDING),
        ("signature_delta", rr.Phase.IMPLEMENTATION),
        ("syntax_result", rr.Phase.IMPLEMENTATION),
        ("covering_red", rr.Phase.VALIDATION),
    ],
)
def test_held_out_features_become_gate_eligible_at_their_own_boundary(feature_id, phase):
    """Generality: not a caller_contract special case.

    Each of these fires at a boundary whose open decision differs from its declared context,
    and each carries roles that decision declares it needs.
    """
    decision = _decision_in_phase(phase)
    record = _record(feature_id, decision, "src/api.py")
    assert record.decision_context is not decision.context

    assert _gate(record, decision, role_driven=False).relevant is False
    assert _gate(record, decision, role_driven=True).relevant is True


def test_decision_anchor_is_still_required_when_the_lever_is_off():
    """Byte-identical guard for the `decision:` anchor relaxation.

    Role-driven mode waives the requirement that evidence name the OPEN decision in its
    causal neighborhood, because the installed producer stamps its own provenance context
    there and never the open decision's id. That waiver must NOT apply by default.

    Added because a mutation replacing ``(anchored_on_open_decision or role_driven)`` with
    a bare ``True`` SURVIVED -- nothing asserted the anchor still bites with the lever off.
    """
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    # In-context evidence (so provenance is NOT the reason it fails) whose neighborhood
    # names no decision at all.
    unanchored = dataclasses.replace(
        _record("caller_contract", decision, "src/api.py"),
        causal_neighborhood=("subject:src/api.py", "obligation:task"),
    )
    assert unanchored.decision_context is decision.context

    assert _gate(unanchored, decision, role_driven=False).relevant is False
    # ...and the lever is what waives it.
    assert _gate(unanchored, decision, role_driven=True).relevant is True
