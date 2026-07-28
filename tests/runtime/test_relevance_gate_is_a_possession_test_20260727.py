"""C12 — the relevance gate is a POSSESSION test, not an OPENNESS test.

THE DEFECT. `evaluate_feature_contract` (`reasoning_runtime.py:4809-4813`) ends in:

    relevant = (serves_open_decision
                and (anchored_on_open_decision or role_driven)
                and bool(active_semantic_nodes.intersection(evidence_semantic_nodes)))

`active_semantic_nodes` is `{obligation:task} | {subject:<focused_file>}`. The SINGLE production
construction site for evidence neighbourhoods (`gateway.py:1919-1923`) emits exactly three nodes:

    f"decision:{contract.decision_context.value}", f"fact:{lineage.fact_class}", f"subject:{subject}"

`decision:` is stripped by both comprehensions and `fact:` can never match anything active. So the
intersection reduces to **`subject:` string equality against the files the agent has ALREADY
OPENED**.

WHY THAT IS BACKWARDS FOR THIS PRODUCT. `localization`'s subject is the ranked TOP FILE — a file the
agent has by definition NOT yet opened, because naming an unopened file is the entire point of the
feature (CLAUDE.md §3). The gate is therefore empty exactly when the feature has something to say,
and non-empty only once the evidence is redundant. **GT can only recommend a file the agent has
already read.**

WHY EXISTING TESTS DO NOT CATCH IT: every in-repo fixture builds records with `"obligation:task"` in
the neighbourhood (e.g. `test_role_driven_temporal_gate_20260726.py`), which intersects the active
set unconditionally and masks the subject comparison entirely. These tests use the PRODUCTION
three-node shape instead. That difference IS the bug's hiding place.

NO BAR IS WEAKENED HERE. This file only asserts the gate's behaviour; it does not change it. The
causal-connection guard must keep rejecting genuinely unrelated evidence — pinned below.
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

OPENED = "src/pkg/opened.py"      # the agent has viewed this
UNOPENED = "src/pkg/answer.py"    # the ranked top file -- the thing GT exists to name


def _decision_with_focus(*focused_files: str) -> rr.ActiveDecision:
    work_state = dataclasses.replace(
        rr.WorkState.initial(attempt_id="attempt-1", revision=REVISION),
        focused_files=tuple(focused_files),
    )
    return seam.CanonicalRuntimeAttachment._active_decision(
        (), work_state, REVISION, ()
    )


def _production_shaped_record(
    feature_id: str, decision: rr.ActiveDecision, subject: str
) -> rr.EvidenceRecord:
    """Built with the EXACT neighbourhood `gateway.py:1919-1923` emits -- three nodes, and
    deliberately NO `obligation:task`. Injecting that node is what makes every other fixture in
    this repo blind to the defect under test."""
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
            f"decision:{contract.decision_context.value}",
            # The fact node's VALUE is immaterial -- `fact:` can never intersect the active set,
            # which is exactly the property under test. Using the feature id keeps the shape
            # honest without asserting a lineage the contract does not carry.
            f"fact:{feature_id}",
            f"subject:{subject}",
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
    )


def _evaluate(record: rr.EvidenceRecord, decision: rr.ActiveDecision, *, role_driven=False):
    contract = rr.feature_contract_for(record.feature_id)
    return rr.evaluate_feature_contract(
        contract,
        record,
        rr.TemporalRuntimeContext(
            active_decision=decision,
            satisfied_predicates=SATISFIED,
            commitment_window=rr.CommitmentWindowState.OPEN,
            current_revision=REVISION,
            available_substrates=contract.fallback_policy.preferred_substrates,
        ),
        role_driven=role_driven,
    )


def test_positive_control_evidence_about_an_OPENED_file_is_relevant():
    """THE CONTROL. Without this, a `relevant=False` below is unreadable -- it could mean the
    fixture is malformed rather than the gate being a possession test. Same record, same shape,
    the ONLY difference being whether the subject is already in focus."""
    decision = _decision_with_focus(OPENED)
    rec = _production_shaped_record("localization", decision, OPENED)
    ev = _evaluate(rec, decision)
    assert ev.relevant is True, (
        "control failed: the harness cannot produce a relevant verdict at all, so the assertion "
        "below would be measuring the fixture, not the gate"
    )


def test_the_gate_is_a_possession_test_evidence_about_an_UNOPENED_file_is_held():
    """THE DEFECT, characterized. Identical record; subject is the ranked answer the agent has not
    opened. This is what `localization` looks like in production on every task."""
    decision = _decision_with_focus(OPENED)
    rec = _production_shaped_record("localization", decision, UNOPENED)
    ev = _evaluate(rec, decision)
    assert ev.relevant is False
    assert ev.next_lifecycle is rr.EvidenceLifecycle.HELD
    assert ev.release_allowed is False


def test_role_driven_does_not_bypass_the_possession_test():
    """The `role_driven` lever relaxes `serves_open_decision` and the `decision:` anchor -- it does
    NOT touch the intersection at `:4812`. Pins that the lever is not the escape hatch, so nobody
    'fixes' C12 by turning it on."""
    decision = _decision_with_focus(OPENED)
    rec = _production_shaped_record("localization", decision, UNOPENED)
    assert _evaluate(rec, decision, role_driven=True).relevant is False


@pytest.mark.xfail(
    strict=True,
    reason="C12 RESIDUAL, deliberately still xfail after the openness rule landed 2026-07-27. "
    "The gate DID learn an openness rule -- the graph-resolved def-home of a focused SYMBOL is "
    "now an active subject, closing the FILE-subject case for localization (proven in "
    "test_openness_gate_admits_the_unopened_def_home_20260727.py). This case is different and is "
    "NOT covered: focus holds an opened FILE and no symbol, so there is no inquiry to connect the "
    "evidence to. In that state nothing distinguishes the 'right' unopened file from "
    "vendor/unrelated/thing.py two tests below -- admitting one means admitting both, i.e. "
    "'admit everything'. Kept as a strict xfail rather than deleted because the day someone "
    "finds a principled connection relation for the no-inquiry state, this must fail loudly.",
)
def test_SPEC_evidence_naming_an_unopened_file_must_be_releasable():
    """THE ORIGINAL SPEC, retained verbatim. `strict=True` means it fails loudly if the gate ever
    starts admitting this case, which would signal the connection guard had collapsed.

    WHY THIS IS NOT AN UNFIXED BUG. In production, focus is not symbol-empty at a boundary where
    localization fires: `_viewed_symbols_for_action` loads the viewed file's definitions on every
    VIEW, and `_resolved_search_symbols` loads the graph-validated operand on every SEARCH. The
    symbol-empty state is essentially task_start -- where, under GT_LOC_RESLOT, the step-0 brief
    ships no localization narration at all. So the residual case is close to unreachable, and
    'fix' it by relaxing the intersection would trade a real guard for an unreachable gain.
    """
    decision = _decision_with_focus(OPENED)
    rec = _production_shaped_record("localization", decision, UNOPENED)
    assert _evaluate(rec, decision).relevant is True


def test_the_connection_guard_still_rejects_genuinely_unrelated_evidence():
    """NO BAR WEAKENED. Whatever replaces the possession test must still reject evidence with no
    connection to the work at all. Pinned now so the C12 fix cannot silently become 'admit
    everything' -- the failure mode that would turn GT into the context flood it exists to avoid."""
    decision = _decision_with_focus(OPENED)
    rec = _production_shaped_record("localization", decision, "vendor/unrelated/thing.py")
    assert _evaluate(rec, decision).relevant is False
