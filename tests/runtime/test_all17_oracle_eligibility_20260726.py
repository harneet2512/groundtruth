"""All 17 DIRECT features must be oracle-eligible at the boundary they actually fire at.

THE SESSION GOAL, as an executable assertion: the 17 stop injecting independently and instead
emit typed evidence that gt_oracle releases at the right decision. That requires, minimally,
that each feature's evidence be ELIGIBLE for whatever decision is open at its own contracted
boundary. It was not: 7 of 17 fired where their declared ``decision_context`` was not the one
open, and the oracle discarded them on producer identity before looking at their roles.

This drives live code end to end per feature:

    contract.commitment_boundary  ->  a real canonical event  ->  reduce_event
      ->  work-state phase  ->  CanonicalRuntimeAttachment._active_decision  ->  open decision
      ->  role fit against that decision's required|useful roles

The only hand-authored mapping is boundary -> SemanticKind, i.e. "what kind of observation
IS a file view". That is observable truth about the harness, not a copy of the policy under
test, so it cannot make the assertion vacuous: the phase, the decision and the role tables are
all read from live code.

NOTHING HERE WEAKENS A BAR. Eligibility is not delivery and not completeness -- ``anchor``
below records that only 14 of 17 can COMPLETE a decision alone, and that stays true. A feature
that is eligible still has to pass freshness, connectivity, dedup, budget, and the coalition
still has to carry the decision's REQUIRED role.
"""

from __future__ import annotations

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)

ALL_17 = sorted(rr._FACT_DECISION_CONTRACTS) + sorted(rr._CAP_FACT_BINDING)

# What kind of observation each registered delivery boundary IS. Harness truth, not policy.
BOUNDARY_OUTCOME = {
    "task_start": None,  # no observation yet -- the step-0 brief
    "search_result": (rr.SemanticKind.SEARCH_RESULT, "src/api.py", None),
    "failed_search": (rr.SemanticKind.SEARCH_EMPTY, "src/missing.py", None),
    "file_view": (rr.SemanticKind.SOURCE_VIEWED, "src/api.py", None),
    "first_view_edit": (rr.SemanticKind.SOURCE_VIEWED, "src/api.py", None),
    "edit_result": (rr.SemanticKind.EDIT_EXECUTED, "src/api.py", True),
    "test_result": (rr.SemanticKind.TEST_FAIL, "tests/test_api.py", None),
    "failure_obs": (rr.SemanticKind.TEST_FAIL, "tests/test_api.py", None),
    "submit": (rr.SemanticKind.SUBMIT_PROPOSED, "submit", None),
}

# A repository mutation MUST advance the revision -- reduce_event raises
# StateIntegrityError otherwise, which is correct and is not something to work around.
MUTATED_REVISION = rr.RevisionVector(
    repository_content="repo-2",
    graph="graph-2",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)


def _open_decision_at(boundary: str) -> rr.ActiveDecision:
    """Drive the REAL reducer for this boundary, then the REAL decision builder."""
    state = rr.WorkState.initial(attempt_id="attempt-1", revision=REVISION)
    outcome_spec = BOUNDARY_OUTCOME[boundary]
    if outcome_spec is not None:
        kind, subject, changed = outcome_spec
        outcome = (
            rr.SemanticOutcome(kind=kind, subject=subject, changed=changed)
            if changed is not None
            else rr.SemanticOutcome(kind=kind, subject=subject)
        )
        after = MUTATED_REVISION if changed else REVISION
        state = rr.reduce_event(
            state,
            rr.CanonicalEvent(
                event_id="ev-1",
                attempt_id="attempt-1",
                sequence=1,
                kind=rr.EventKind.OBSERVATION_COMMITTED,
                authority=rr.Authority.RESULT_DERIVED,
                outcomes=(outcome,),
                revision_before=REVISION,
                revision_after=after,
                previous_event_hash="",
                carrier="",
            ),
        )
    return seam.CanonicalRuntimeAttachment._active_decision((), state, REVISION, ())


def _fits(contract: rr.FeatureContract, decision: rr.ActiveDecision) -> bool:
    return bool(
        set(contract.roles)
        & (set(decision.required_roles) | set(decision.useful_roles))
    )


def test_the_seventeen_are_exactly_the_direct_features():
    """Guard the denominator: 10 FACT + 7 CAP byte owners."""
    assert len(rr._FACT_DECISION_CONTRACTS) == 10
    assert len(rr._CAP_FACT_BINDING) == 7
    assert len(ALL_17) == 17


def test_every_boundary_in_use_is_covered_by_this_test():
    """If a feature moves to an unmodelled boundary this fails loudly instead of skipping."""
    boundaries = {
        rr.feature_contract_for(feature).commitment_boundary for feature in ALL_17
    }
    assert boundaries <= set(BOUNDARY_OUTCOME), (
        f"unmodelled delivery boundaries: {sorted(boundaries - set(BOUNDARY_OUTCOME))}"
    )


@pytest.mark.parametrize("feature_id", ALL_17)
def test_feature_is_role_eligible_at_its_own_boundary(feature_id):
    """THE GOAL. Every one of the 17 can be considered by the oracle where it fires."""
    contract = rr.feature_contract_for(feature_id)
    decision = _open_decision_at(contract.commitment_boundary)

    assert _fits(contract, decision), (
        f"{feature_id} fires at {contract.commitment_boundary} where "
        f"{decision.context.value} is open, but none of its roles "
        f"{[r.value for r in contract.roles]} is required or useful there"
    )


def _mismatched() -> set[str]:
    return {
        feature_id
        for feature_id in ALL_17
        if rr.feature_contract_for(feature_id).decision_context
        is not _open_decision_at(
            rr.feature_contract_for(feature_id).commitment_boundary
        ).context
    }


def test_features_fire_outside_their_declared_context():
    """WHY the partition had to go.

    These fire at a boundary whose open decision is not the one they declare, so under
    producer-identity partitioning their evidence was discarded before its roles were read.

    Asserted as a SUBSET, not an exact list, on purpose. ``failure_obs`` is the stuck/pivot
    boundary and this fixture approximates it with a single TEST_FAIL, which the reducer
    reads as VALIDATION rather than RECOVERY -- so ``recovery``/``GT_HYPOTHESIS`` may appear
    here as a fixture artifact rather than a product fact. The seven below do not depend on
    that approximation: their boundaries (file_view, test_result, task_start, edit_result)
    are modelled exactly.
    """
    assert {
        "GT_EDIT_CHECK",
        "GT_PATCH_DELTA",
        "caller_contract",
        "covering_red",
        "obligations",
        "signature_delta",
        "syntax_result",
    } <= _mismatched()


def test_partitioned_eligibility_is_strictly_worse_than_role_driven():
    """The measured baseline the change improves on. A record, not a target.

    Stated as a relationship rather than a hard count so the fixture approximation above
    cannot make it a false red: whatever the exact figure, producer-identity partitioning
    admits strictly fewer of the 17 than role fit does, and role fit admits all of them.
    """
    partitioned = len(ALL_17) - len(_mismatched())
    role_eligible = sum(
        _fits(
            rr.feature_contract_for(feature_id),
            _open_decision_at(
                rr.feature_contract_for(feature_id).commitment_boundary
            ),
        )
        for feature_id in ALL_17
    )
    assert role_eligible == 17
    assert partitioned < role_eligible
    # Recorded live value at the time of writing; a change here is a real registration move.
    assert partitioned == 8


def test_only_fourteen_can_anchor_and_that_is_unchanged():
    """NO-WEAKENING RECORD.

    Eligibility is not completeness. ``obligations``, ``syntax_result`` and
    ``GT_EDIT_CHECK`` still cannot COMPLETE the decision open at their boundary -- they
    enrich. Making all 17 eligible must not quietly turn them into anchors.
    """
    anchors = sorted(
        feature_id
        for feature_id in ALL_17
        if set(
            _open_decision_at(
                rr.feature_contract_for(feature_id).commitment_boundary
            ).required_roles
        )
        <= set(rr.feature_contract_for(feature_id).roles)
    )
    assert len(anchors) == 14
    assert {"obligations", "syntax_result", "GT_EDIT_CHECK"}.isdisjoint(anchors)
