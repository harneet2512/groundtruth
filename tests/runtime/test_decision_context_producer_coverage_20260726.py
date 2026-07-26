"""Producer coverage for every Phase-reachable DecisionContext.

WHY THIS FILE EXISTS (LIPI / Logic + Integration).  The oracle compiles a capsule only
for the ONE decision the reducer says is open, and ``select_evidence_coalition`` drops
any record whose ``decision_context`` differs -- ``OTHER_DECISION``, at
``reasoning_runtime.py:5578``, BEFORE any role, freshness, or budget reasoning.  A
decision context with no producer is therefore a permanently dead decision: every turn
that lands on it yields ``DECISION_INCOMPLETE`` no matter how much evidence is ready.

``DecisionContext.SOURCE_UNDERSTANDING`` is exactly that state today.  ``Phase.UNDERSTANDING``
maps to it (``gt_mini_patch.py`` ``_active_decision``), ``Phase.UNDERSTANDING`` is what the
reducer enters on ``SOURCE_VIEWED``/``SYMBOL_VIEWED`` (``reasoning_runtime.py:503``, ``:507``)
-- i.e. on the single most common observation an agent produces -- and no entry in
``_FACT_DECISION_CONTRACTS`` carries that context.

The gap lands directly on the Wave 15 repair.  ``caller_contract`` now fires at its
contracted ``file_view`` boundary, but it is registered to ``PATCH_CONSTRUCTION``, so the
evidence it produces at view time is dropped as ``OTHER_DECISION`` and held forever.  Note
what ``SOURCE_UNDERSTANDING`` declares: required ``BEHAVIORAL_CONTRACT`` plus useful
``AFFECTED_CALLER`` -- precisely ``caller_contract``'s two roles.  The context was written
expecting caller-contract-shaped evidence; the producer is bound elsewhere.

Both facts below are derived from the LIVE registry and the LIVE reducer/composer.  Nothing
is hand-copied, so these tests keep biting if the tables are edited.

RESOLUTION IS A PRODUCT DECISION, deliberately not taken here: re-registering
``caller_contract`` to ``SOURCE_UNDERSTANDING``, adding a producer for that context, or
relaxing the single-anchor rule all change which decision an evidence class serves.  Do NOT
weaken decision-completeness to make a capsule ship -- correct-or-quiet is the bar.
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


def _producers_by_context() -> dict[rr.DecisionContext, tuple[str, ...]]:
    """Read producer coverage from the live contract registry."""
    coverage: dict[rr.DecisionContext, list[str]] = {
        context: [] for context in rr.DecisionContext
    }
    for feature in sorted(rr._FACT_DECISION_CONTRACTS):
        coverage[rr.feature_contract_for(feature).decision_context].append(feature)
    return {context: tuple(names) for context, names in coverage.items()}


def _phase_reachable_contexts() -> dict[rr.Phase, rr.DecisionContext]:
    """Drive the real ``_active_decision`` once per Phase to learn its context."""
    reachable: dict[rr.Phase, rr.DecisionContext] = {}
    for phase in rr.Phase:
        decision = seam.CanonicalRuntimeAttachment._active_decision(
            (), _work_state_in_phase(phase), REVISION, ()
        )
        reachable[phase] = decision.context
    return reachable


def _work_state_in_phase(phase: rr.Phase) -> rr.WorkState:
    """Build a real WorkState parked in ``phase`` via dataclass replace."""
    import dataclasses

    return dataclasses.replace(
        rr.WorkState.initial(attempt_id="attempt-1", revision=REVISION),
        phase=phase,
    )


def _caller_contract_record() -> rr.EvidenceRecord:
    """A caller_contract record shaped exactly as its live contract requires."""
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
        causal_neighborhood=("subject:src/api.py", "obligation:task"),
        lifecycle=rr.EvidenceLifecycle.READY,
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


def _decision_in_context(context: rr.DecisionContext) -> rr.ActiveDecision:
    """Build the decision the seam would build for ``context``."""
    work_state = _work_state_in_phase(
        next(
            phase
            for phase, reached in _phase_reachable_contexts().items()
            if reached is context
        )
    )
    return seam.CanonicalRuntimeAttachment._active_decision(
        (), work_state, REVISION, ()
    )


# --------------------------------------------------------------------------------------
# The general invariant.  STRICT xfail: when the gap is closed this XPASSes, which strict
# mode reports as a FAILURE -- forcing whoever fixes it to delete the marker rather than
# leaving a permanently-green test that no longer asserts anything.
# --------------------------------------------------------------------------------------
@pytest.mark.xfail(
    strict=True,
    reason=(
        "SOURCE_UNDERSTANDING has zero producers; Phase.UNDERSTANDING reaches it on "
        "every SOURCE_VIEWED. Remove this marker when a producer is registered."
    ),
)
def test_every_phase_reachable_decision_context_has_a_producer():
    coverage = _producers_by_context()
    reachable = set(_phase_reachable_contexts().values())
    starved = sorted(
        context.value for context in reachable if not coverage[context]
    )
    assert not starved, (
        f"decision contexts reachable from a Phase with zero producers: {starved}. "
        "Every turn landing on one is a guaranteed DECISION_INCOMPLETE."
    )


def test_source_understanding_is_the_only_starved_context():
    """Characterization: pins the CURRENT gap so a NEW one cannot appear unnoticed.

    If this fails with a longer list, a second context lost its producer -- diagnose that,
    do not widen the expected set.  If it fails with an empty list, the gap is closed and
    this test plus the strict xfail above should both be deleted.
    """
    coverage = _producers_by_context()
    reachable = set(_phase_reachable_contexts().values())
    starved = sorted(context.value for context in reachable if not coverage[context])
    assert starved == ["SOURCE_UNDERSTANDING"]


def test_viewing_source_parks_the_reducer_in_a_starved_decision():
    """The live reducer: one SOURCE_VIEWED puts the attempt on the dead context."""
    state = rr.WorkState.initial(attempt_id="attempt-1", revision=REVISION)
    event = rr.CanonicalEvent(
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
    )

    reduced = reduce_and_check(state, event)
    assert reduced.phase is rr.Phase.UNDERSTANDING

    decision = seam.CanonicalRuntimeAttachment._active_decision(
        (), reduced, REVISION, ()
    )
    assert decision.context is rr.DecisionContext.SOURCE_UNDERSTANDING
    assert _producers_by_context()[decision.context] == ()


def reduce_and_check(state: rr.WorkState, event: rr.CanonicalEvent) -> rr.WorkState:
    reduced = rr.reduce_event(state, event)
    assert reduced is not state, "reducer must return a new state"
    return reduced


def test_caller_contract_evidence_is_dropped_at_the_view_boundary():
    """The whole chain: real record -> real composer -> OTHER_DECISION, nothing ships.

    This is the concrete cost of the gap and it lands on the Wave 15 repair: the producer
    fires at ``file_view`` and the evidence is structurally undeliverable at that moment.
    """
    decision = _decision_in_context(rr.DecisionContext.SOURCE_UNDERSTANDING)
    record = _caller_contract_record()
    assert record.decision_context is not decision.context

    outcome = rr.select_evidence_coalition(decision, [record])

    assert outcome.coalition == ()
    assert outcome.decision_complete is False
    assert outcome.release_allowed is False
    assert [s.reason for s in outcome.suppressed] == [
        rr.SuppressionReason.OTHER_DECISION
    ]


def test_the_same_record_would_ship_against_its_own_decision_context():
    """Control: the record is well-formed and the composer works.

    Without this, the assertions above could pass because the record is malformed rather
    than because the context binding is wrong.
    """
    decision = _decision_in_context(rr.DecisionContext.PATCH_CONSTRUCTION)
    record = _caller_contract_record()
    assert record.decision_context is decision.context

    outcome = rr.select_evidence_coalition(decision, [record])

    assert len(outcome.coalition) == 1
    assert outcome.decision_complete is True
    assert outcome.release_allowed is True
    assert outcome.suppressed == ()


def test_source_understanding_useful_roles_match_caller_contract():
    """Evidence the gap is a registration mistake, not an intended silence.

    SOURCE_UNDERSTANDING requires BEHAVIORAL_CONTRACT and lists AFFECTED_CALLER as useful.
    Those are exactly caller_contract's two roles, and only caller_contract and
    signature_delta carry AFFECTED_CALLER at all -- the context was written anticipating
    this producer.
    """
    decision = _decision_in_context(rr.DecisionContext.SOURCE_UNDERSTANDING)
    contract = rr.feature_contract_for("caller_contract")

    assert set(decision.required_roles) <= set(contract.roles)
    assert set(contract.roles) <= set(decision.required_roles) | set(
        decision.useful_roles
    )

    carriers = sorted(
        feature
        for feature in rr._FACT_DECISION_CONTRACTS
        if rr.EvidenceRole.AFFECTED_CALLER
        in rr.feature_contract_for(feature).roles
    )
    assert carriers == ["caller_contract", "signature_delta"]
