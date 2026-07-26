"""RED contract: coalition eligibility is role-driven, not partitioned by decision_context.

MEASURED DEFECT.  ``select_evidence_coalition`` drops any record whose ``decision_context``
differs from the open decision (``OTHER_DECISION``) BEFORE any role reasoning.  Since each
FACT carries exactly one context, that partitions the evidence pool by producer identity
rather than by what the open decision needs.  Consequence, computed from the live registry:
**12 of 25 required/useful role slots have no reachable carrier**, and
``PATCH_CONSTRUCTION`` can never contain more than ``{caller_contract, obligations}``.

The ``useful_roles`` tables are the architecture's own statement of what a decision wants --
``PATCH_CONSTRUCTION`` lists ``AFFECTED_CALLER, STATE_DEPENDENCY, VALIDATION,
MATERIAL_UNCERTAINTY`` -- and two thirds of that intent is unreachable.

THE FIX: eligibility = the record's roles serve the open decision's required|useful roles,
AND causal-neighborhood/graph connectivity, AND freshness.  ``decision_context`` becomes
PROVENANCE (which decision the evidence was produced for), not an eligibility gate.

THIS REMOVES NO PROTECTION.  Every other guard runs independently and unchanged:
role-fit (``NOT_ACTIONABLE_FOR_DECISION``), connectivity (``DISCONNECTED``), freshness
(``STALE``), ``ALREADY_VISIBLE``, ``SUPERSEDED``, duplicate-claim dedup, token budget, and --
critically -- decision-completeness.  ``test_role_driven_still_requires_the_anchor`` is the
no-weakening proof: without a record carrying the decision's REQUIRED role, the coalition
still refuses to complete.

PURITY (architecture item 8: "remain pure and replayable").  The lever is a PARAMETER, not an
environment read inside the composer.  Reading the environment here would make replay depend
on ambient process state.  The single production caller resolves it once.

Default OFF => byte-identical.
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


def _decision_in_phase(phase: rr.Phase) -> rr.ActiveDecision:
    work_state = dataclasses.replace(
        rr.WorkState.initial(attempt_id="attempt-1", revision=REVISION),
        phase=phase,
    )
    return seam.CanonicalRuntimeAttachment._active_decision(
        (), work_state, REVISION, ()
    )


def _record(feature_id: str, *, subject: str = "src/api.py") -> rr.EvidenceRecord:
    """A record for ``feature_id`` shaped exactly as its live contract requires."""
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
        causal_neighborhood=(f"subject:{subject}", "obligation:task"),
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


def _reasons(outcome) -> set[str]:
    return {record.reason.value for record in outcome.suppressed}


# ------------------------------------------------------------------ default is unchanged
def test_out_of_context_evidence_is_dropped_by_default():
    """Byte-identical guard: with the lever off, today's partition still applies."""
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    assert decision.context is rr.DecisionContext.PATCH_CONSTRUCTION

    outcome = rr.select_evidence_coalition(decision, [_record("covering_red")])

    assert outcome.coalition == ()
    assert _reasons(outcome) == {"OTHER_DECISION"}


@pytest.mark.parametrize(
    "feature_id",
    ["covering_red", "recovery", "syntax_result", "newfile_precedent", "localization"],
)
def test_default_drops_every_out_of_context_feature(feature_id):
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    outcome = rr.select_evidence_coalition(
        decision, [_record(feature_id)], role_driven=False
    )
    assert outcome.coalition == ()


# ------------------------------------------------------------------ the fix
def test_role_driven_admits_out_of_context_evidence_that_fits():
    """covering_red carries VALIDATION, which PATCH_CONSTRUCTION lists as USEFUL."""
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)

    outcome = rr.select_evidence_coalition(
        decision,
        [_record("caller_contract"), _record("covering_red")],
        role_driven=True,
    )

    features = {item.feature_id for item in outcome.coalition}
    assert features == {"caller_contract", "covering_red"}
    assert outcome.decision_complete is True
    assert outcome.release_allowed is True


def test_role_driven_still_requires_the_anchor():
    """NO-WEAKENING PROOF.

    covering_red fits PATCH_CONSTRUCTION's USEFUL roles but carries no BEHAVIORAL_CONTRACT,
    which is the REQUIRED role. Admitting it must NOT make the decision complete. If this
    ever passes as complete, decision-completeness has been weakened and the change is wrong.
    """
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)

    outcome = rr.select_evidence_coalition(
        decision, [_record("covering_red")], role_driven=True
    )

    assert outcome.decision_complete is False
    assert outcome.release_allowed is False
    assert rr.EvidenceRole.BEHAVIORAL_CONTRACT in outcome.unresolved_roles


def test_role_driven_still_rejects_roles_that_do_not_fit():
    """Eligibility moved to roles -- it did not become unconditional.

    submit_refusal carries BLOCKER/TERMINAL_ASSURANCE, neither of which appears in
    PATCH_CONSTRUCTION's required or useful sets, so it stays suppressed -- but now for the
    accurate reason rather than for its producer identity.
    """
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    unfitting = _record("submit_refusal")
    assert not set(unfitting.roles) & (
        set(decision.required_roles) | set(decision.useful_roles)
    )

    outcome = rr.select_evidence_coalition(decision, [unfitting], role_driven=True)

    assert outcome.coalition == ()
    assert _reasons(outcome) == {"NOT_ACTIONABLE_FOR_DECISION"}


def test_role_driven_still_enforces_freshness():
    """A stale out-of-context record is rejected on freshness, not admitted by role fit."""
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    stale = dataclasses.replace(_record("covering_red"), fresh=False)

    outcome = rr.select_evidence_coalition(
        decision, [_record("caller_contract"), stale], role_driven=True
    )

    assert {item.feature_id for item in outcome.coalition} == {"caller_contract"}
    assert "STALE" in _reasons(outcome)


def test_role_driven_still_enforces_already_visible():
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    seen = dataclasses.replace(_record("covering_red"), already_visible=True)

    outcome = rr.select_evidence_coalition(
        decision, [_record("caller_contract"), seen], role_driven=True
    )

    assert {item.feature_id for item in outcome.coalition} == {"caller_contract"}
    assert "ALREADY_VISIBLE" in _reasons(outcome)


def test_role_driven_buys_more_decision_coverage_for_the_same_budget():
    """The point of the change, stated as an assertion rather than a claim.

    Under the partition PATCH_CONSTRUCTION can hold at most {caller_contract, obligations},
    and those two overlap: ``obligations`` carries only BEHAVIORAL_CONTRACT, which the anchor
    already covers, so the second slot buys NO extra role.

    Role-driven eligibility lets the same budget reach a genuinely different role. The claim
    is therefore about COVERAGE, not coalition size -- the capsule budget (350 tokens here)
    caps membership either way, so a bigger coalition was never the goal. A strictly larger
    covered-role set for the same spend is.
    """
    decision = _decision_in_phase(rr.Phase.IMPLEMENTATION)
    offered = [
        _record("caller_contract"),
        _record("obligations", subject="issue"),
        _record("newfile_precedent", subject="src/new_mod.py"),
        _record("covering_red", subject="tests/test_api.py"),
    ]

    partitioned = rr.select_evidence_coalition(decision, offered, role_driven=False)
    role_driven = rr.select_evidence_coalition(decision, offered, role_driven=True)

    # The structural ceiling the partition imposes.
    assert {i.feature_id for i in partitioned.coalition} <= {
        "caller_contract",
        "obligations",
    }
    # Role-driven reaches outside that ceiling ...
    assert {i.feature_id for i in role_driven.coalition} - {
        "caller_contract",
        "obligations",
    }
    # ... and buys strictly more of what the decision declared it needs ...
    assert set(partitioned.coverage) < set(role_driven.coverage)
    # ... without spending more, and without relaxing completeness.
    assert role_driven.total_tokens <= partitioned.total_tokens
    assert partitioned.decision_complete is True
    assert role_driven.decision_complete is True
