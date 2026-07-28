"""Handoff Wave 15C line 587: decision selection -> one coalition -> one staged capsule.

This is the item the whole deterministic runtime exists to satisfy, and the one that has
never happened: no capsule has ever compiled on the installed path.

WHY THE EXISTING MATRIX TEST DOES NOT PROVE IT.
``tests/runtime/test_all17_natural_attachment_matrix_wave12.py:556-597`` builds its decision
as ``ActiveDecision(required_roles=record.roles, useful_roles=(), ...)`` -- a decision tailored
to the evidence -- and hand-feeds **21** substrates. The live seam derives **2**. That proves
the compiler CAN compile given rich, agreeable input. It says nothing about whether the
installed path ever reaches that input, which is the actual question.

THIS FILE USES THE INSTALLED PATH'S OWN INPUTS:
  * the decision comes from ``CanonicalRuntimeAttachment._active_decision`` -- the same
    function the seam calls, derived from work state, never tailored to the evidence;
  * the substrate list comes from ``CanonicalRuntimeAttachment._available_substrates`` --
    the same function the seam calls, derived from the records that actually exist;
  * the work state comes from the real ``reduce_event`` reducer driven by a real canonical
    event.

Nothing is weakened to make a capsule appear. Both arms are asserted: with the lever off the
capsule must NOT stage for an out-of-context record, and with it on it must. If the only way
to get a capsule were to relax decision-completeness, that would be a failure, not a pass --
``test_capsule_still_refuses_without_the_required_role`` pins that.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path


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
        rr.TemporalPredicate.AUTHORIZED_BYTE_OWNER_LINEAGE_PRESENT,
    }
)


def _viewed_source_state() -> rr.WorkState:
    """Drive the REAL reducer with a real file-view observation."""
    state = rr.WorkState.initial(attempt_id="attempt-1", revision=REVISION)
    return rr.reduce_event(
        state,
        rr.CanonicalEvent(
            event_id="ev-1",
            attempt_id="attempt-1",
            sequence=1,
            kind=rr.EventKind.OBSERVATION_COMMITTED,
            authority=rr.Authority.RESULT_DERIVED,
            outcomes=(
                rr.SemanticOutcome(
                    kind=rr.SemanticKind.SOURCE_VIEWED, subject="src/api.py"
                ),
            ),
            revision_before=REVISION,
            revision_after=REVISION,
            previous_event_hash="",
            carrier="",
        ),
    )


def _caller_contract_record(decision: rr.ActiveDecision) -> rr.EvidenceRecord:
    """The evidence the Wave 15 repair made the view boundary actually produce."""
    contract = rr.feature_contract_for("caller_contract")
    return rr.EvidenceRecord(
        evidence_id="ev-caller-contract-1",
        feature_id="caller_contract",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="src/api.py",
        claim="src/api.py:handle is called by 2 production callers",
        actionable_consequence=(
            "preserve the handle(request, *, retries) signature for both callers"
        ),
        provenance=("src/caller.py:2",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        # EXACTLY what the installed producer emits. `gateway.py` (the producer that runs
        # at file_view -- the seam calls `gateway_produce_raw`) builds:
        #     (f"decision:{contract.decision_context.value}", f"fact:{...}", f"subject:{...}")
        # It does NOT include the active decision's id. An earlier version of this file put
        # `decision:{decision.decision_id}` here, which handed the relevance gate the exact
        # anchor it was looking for -- the same tailoring this file accuses the wave-12
        # matrix test of, moved from the decision side to the evidence side. An adversarial
        # audit caught it. Do not "fix" a failure here by reintroducing that anchor.
        causal_neighborhood=(
            f"decision:{contract.decision_context.value}",
            "fact:caller_contract",
            "subject:src/api.py",
        ),
        lifecycle=rr.EvidenceLifecycle.PENDING,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        revision_dependencies=contract.revision_dependencies,
        token_cost=140,
        failure_prevention=3,
        causal_value=3,
        contradiction_resolution=0,
        anchoring_risk=0,
        observed_substrates=("graph",),
    )


def _stage(tmp_path: Path, records, *, role_driven: bool):
    """Drive the REAL runtime with the seam's OWN decision and substrate derivation."""
    journal = rr.RuntimeJournal(tmp_path / "runtime.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-1",
        journal=journal,
        initial_revision=REVISION,
        role_driven_coalition=role_driven,
    )
    for record in records:
        runtime.ingest_evidence(record)

    work_state = _viewed_source_state()
    decision = seam.CanonicalRuntimeAttachment._active_decision(
        (), work_state, REVISION, ()
    )
    substrates = seam.CanonicalRuntimeAttachment._available_substrates(records)

    plan = runtime.prepare_next_inference(
        decisions=(decision,),
        satisfied_predicates=SATISFIED,
        commitment_window=rr.CommitmentWindowState.OPEN,
        available_substrates=substrates,
        native_observation="native observation",
        observation_id="obs-1",
        source_model_call_id="source-call",
        model_call_id="model-call-1",
    )
    return decision, substrates, plan


def test_the_decision_and_substrates_come_from_the_installed_path(tmp_path):
    """Guard against the matrix test's mistake: no tailoring, no hand-fed substrate list."""
    decision, substrates, _ = _stage(
        tmp_path,
        [_caller_contract_record(
            seam.CanonicalRuntimeAttachment._active_decision(
                (), _viewed_source_state(), REVISION, ()
            )
        )],
        role_driven=True,
    )
    assert decision.context is rr.DecisionContext.SOURCE_UNDERSTANDING
    # Derived, not dictated: the decision does NOT simply mirror the record's roles.
    assert decision.required_roles == (rr.EvidenceRole.BEHAVIORAL_CONTRACT,)
    assert decision.useful_roles
    # The live seam derives a handful of substrates, not the matrix test's 21.
    assert len(substrates) <= 4


def test_no_capsule_stages_by_default(tmp_path):
    """Byte-identical arm: under producer-identity partitioning nothing ships.

    caller_contract declares PATCH_CONSTRUCTION; a file view opens SOURCE_UNDERSTANDING.
    This is the state the runtime has always been in -- DECISION_INCOMPLETE, evidence HELD.
    """
    decision = seam.CanonicalRuntimeAttachment._active_decision(
        (), _viewed_source_state(), REVISION, ()
    )
    _, _, plan = _stage(
        tmp_path, [_caller_contract_record(decision)], role_driven=False
    )

    assert plan.compilation.state is not rr.CapsuleCompilationState.COMPILED
    assert not plan.delivery_attempt_id


def test_capsule_stages_on_the_installed_path(tmp_path):
    """HANDOFF LINE 587. One decision -> one coalition -> one staged capsule."""
    decision = seam.CanonicalRuntimeAttachment._active_decision(
        (), _viewed_source_state(), REVISION, ()
    )
    _, _, plan = _stage(
        tmp_path, [_caller_contract_record(decision)], role_driven=True
    )

    assert plan.delivery_attempt_id, (
        f"no capsule staged: {plan.compilation.state} / "
        f"{getattr(plan.compilation, 'failure_code', None)}"
    )
    assert plan.compilation.state is rr.CapsuleCompilationState.COMPILED


def test_a_second_inference_on_the_same_observation_yields_no_second_capsule(tmp_path):
    """dose<=1 survives: more eligible evidence must not mean more capsules.

    REWRITTEN after an adversarial audit proved the previous version fully vacuous. It
    asserted ``len([plan.compilation]) == 1`` -- a tautology over a one-element list literal
    -- and ``plan.compilation.decision_context is decision.context``, which is set on FAILED
    compilations too. Both passed with role_driven=False and NO capsule at all.

    This drives the real runtime twice on the SAME observation_id and asserts the second
    call produces no additional delivery attempt.
    """
    decision = seam.CanonicalRuntimeAttachment._active_decision(
        (), _viewed_source_state(), REVISION, ()
    )
    record = _caller_contract_record(decision)

    journal = rr.RuntimeJournal(tmp_path / "runtime.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-1",
        journal=journal,
        initial_revision=REVISION,
        role_driven_coalition=True,
    )
    runtime.ingest_evidence(record)
    substrates = seam.CanonicalRuntimeAttachment._available_substrates([record])

    def _prepare(model_call_id: str):
        return runtime.prepare_next_inference(
            decisions=(decision,),
            satisfied_predicates=SATISFIED,
            commitment_window=rr.CommitmentWindowState.OPEN,
            available_substrates=substrates,
            native_observation="native observation",
            observation_id="obs-1",
            source_model_call_id="source-call",
            model_call_id=model_call_id,
        )

    first = _prepare("model-call-1")
    second = _prepare("model-call-2")

    # The first genuinely delivered -- otherwise the second's silence proves nothing.
    assert first.delivery_attempt_id
    assert first.compilation.state is rr.CapsuleCompilationState.COMPILED

    assert not second.delivery_attempt_id, (
        "a second capsule was staged for the same observation; dose<=1 is broken"
    )
    # Assert the REASON, not just the absence. Dose is defended by two independent
    # mechanisms here -- the observation guard, and the evidence becoming already-visible
    # after the first delivery -- so "no delivery attempt" alone cannot tell them apart. A
    # mutation inverting the observation guard survived until this assertion existed,
    # because the visibility path silently covered for it.
    assert second.compilation.failure_code == "OBSERVATION_ALREADY_HAS_CAPSULE"


def test_capsule_still_refuses_without_the_required_role(tmp_path):
    """NO-WEAKENING PROOF at the capsule boundary.

    covering_red is role-eligible for this decision only via useful roles; it carries no
    BEHAVIORAL_CONTRACT, which SOURCE_UNDERSTANDING REQUIRES. A capsule must NOT stage.
    If this ever stages, decision-completeness was weakened to manufacture a delivery.
    """
    decision = seam.CanonicalRuntimeAttachment._active_decision(
        (), _viewed_source_state(), REVISION, ()
    )
    contract = rr.feature_contract_for("covering_red")
    enrichment_only = rr.EvidenceRecord(
        evidence_id="ev-covering-red-1",
        feature_id="covering_red",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="tests/test_api.py",
        claim="tests/test_api.py::test_handle covers the edit and ran RED",
        actionable_consequence="make tests/test_api.py::test_handle pass",
        provenance=("tests/test_api.py:11",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=(
            "subject:src/api.py",
            f"decision:{decision.decision_id}",
            "obligation:task",
        ),
        lifecycle=rr.EvidenceLifecycle.PENDING,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        revision_dependencies=contract.revision_dependencies,
        token_cost=140,
        failure_prevention=3,
        causal_value=3,
        contradiction_resolution=0,
        anchoring_risk=0,
    )
    assert rr.EvidenceRole.BEHAVIORAL_CONTRACT not in enrichment_only.roles

    _, _, plan = _stage(tmp_path, [enrichment_only], role_driven=True)

    assert not plan.delivery_attempt_id
    assert plan.compilation.state is not rr.CapsuleCompilationState.COMPILED


# ---------------------------------------------------------------------------------------
# Direct unit coverage of the MIXED_DECISION_CONTEXT guard.
#
# Added because mutations C3 (flip the guard's default to True) and C4 (delete the guard)
# BOTH SURVIVED the tests above. They survived for a real reason: under the default path the
# composer already drops out-of-context records as OTHER_DECISION, so a mixed coalition never
# reaches the compiler and the guard is unreachable from end-to-end tests. It is
# defence-in-depth against a coalition assembled some other way -- and defence-in-depth that
# nothing tests is indistinguishable from defence-in-depth that has been deleted.
#
# These call ``compile_observation_capsule`` directly with a deliberately mixed coalition.
# ---------------------------------------------------------------------------------------
def _ref(feature_id: str) -> rr.EvidenceRef:
    contract = rr.feature_contract_for(feature_id)
    return rr.EvidenceRef(
        evidence_id=f"ev-{feature_id}",
        feature_id=feature_id,
        decision_context=contract.decision_context,
        roles=contract.roles,
        claim=f"{feature_id} claim",
        actionable_consequence=f"act on {feature_id}",
        provenance=("src/api.py:1",),
        grade=rr.EvidenceGrade.VERIFIED,
        owner_feature_ids=(),
    )


def _mixed_oracle_decision() -> rr.OracleDecision:
    """A complete, releasable decision whose coalition spans two provenance contexts."""
    caller = _ref("caller_contract")        # PATCH_CONSTRUCTION
    covering = _ref("covering_red")         # FAILURE_RECOVERY
    assert caller.decision_context is not covering.decision_context
    return rr.OracleDecision(
        decision_id="PATCH_CONSTRUCTION:probe",
        decision_context=rr.DecisionContext.PATCH_CONSTRUCTION,
        primary_claim="Construct the patch while preserving required behavior.",
        coalition=(caller, covering),
        mandatory_items=(),
        suppressed=(),
        total_tokens=240,
        coverage=(rr.EvidenceRole.BEHAVIORAL_CONTRACT,),
        unresolved_roles=(),
        overall_grade=rr.EvidenceGrade.VERIFIED,
        decision_complete=True,
        release_allowed=True,
        over_budget=False,
    )


def _compile(decision: rr.OracleDecision, *, role_driven: bool):
    return rr.compile_observation_capsule(
        native_observation="native observation",
        decision=decision,
        observation_id="obs-1",
        source_model_call_id="source-call",
        model_call_id="model-call-1",
        enabled=True,
        role_driven=role_driven,
    )


def test_mixed_coalition_is_refused_by_default():
    """The guard must still bite when the lever is off. Kills mutations C3 and C4."""
    compilation = _compile(_mixed_oracle_decision(), role_driven=False)

    assert compilation.state is rr.CapsuleCompilationState.FAILED
    assert compilation.failure_code == "MIXED_DECISION_CONTEXT"


def test_mixed_coalition_is_permitted_under_role_driven_eligibility():
    """And must NOT bite when the lever is on -- mixed provenance is the point.

    Asserted as "not this failure code" rather than "compiles", because compilation has
    other legitimate reasons to fail on a synthetic decision; the claim under test is
    only that provenance mixing is no longer one of them.
    """
    compilation = _compile(_mixed_oracle_decision(), role_driven=True)

    assert compilation.failure_code != "MIXED_DECISION_CONTEXT"


def test_single_context_coalition_is_unaffected_either_way():
    """Control: the guard is about MIXING, not about context per se."""
    decision = dataclasses.replace(
        _mixed_oracle_decision(), coalition=(_ref("caller_contract"),)
    )
    for role_driven in (False, True):
        compilation = _compile(decision, role_driven=role_driven)
        assert compilation.failure_code != "MIXED_DECISION_CONTEXT"
