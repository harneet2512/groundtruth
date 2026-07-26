"""RED contracts for decision-instance identity and causal connectivity.

``DecisionContext`` is a type (PATCH_CONSTRUCTION, FAILURE_RECOVERY, ...), not
the identity of one concrete open decision.  Likewise ``decision:<id>`` is an
eligibility/window anchor, not causal evidence connecting unrelated subjects.

These tests intentionally fail until the runtime:

* derives a deterministic decision *instance* ID from context + active subject
  + observable decision window;
* builds the active neighborhood from work state/reasoning truth, never from
  the candidate evidence pool; and
* excludes decision-only anchors when testing coalition connectivity.
"""

from __future__ import annotations

from types import SimpleNamespace

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-wave13",
    graph="graph-wave13",
    lsp="lsp-wave13",
    runtime_evidence="runtime-wave13",
)


def _record(
    evidence_id: str,
    *,
    subject: str,
    roles: tuple[rr.EvidenceRole, ...],
    decision_id: str,
) -> rr.EvidenceRecord:
    return rr.EvidenceRecord(
        evidence_id=evidence_id,
        # Deliberately outside the feature-contract registry: this pure
        # coalition test varies roles independently to isolate connectivity.
        feature_id=f"fixture-{evidence_id}",
        decision_context=rr.DecisionContext.PATCH_CONSTRUCTION,
        roles=roles,
        subject=subject,
        claim=f"{evidence_id} claim for {subject}",
        actionable_consequence=f"act on {subject}",
        provenance=(f"{subject}:1",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=(
            f"decision:{decision_id}",
            f"subject:{subject}",
        ),
        lifecycle=rr.EvidenceLifecycle.READY,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        token_cost=10,
        failure_prevention=5,
        causal_value=5,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=("nodes",),
        authority=rr.Authority.RESULT_DERIVED,
    )


def _work_state(
    subject: str,
    *,
    sequence: int,
    decision_window_key: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        phase=rr.Phase.IMPLEMENTATION,
        sequence=sequence,
        decision_window_key=(
            decision_window_key
            if decision_window_key is not None
            else f"window-{sequence}"
        ),
        focused_symbols=(),
        focused_files=(subject,),
    )


def _suppression(
    decision: rr.OracleDecision,
    evidence_id: str,
) -> rr.SuppressionRecord:
    return next(
        row
        for row in decision.suppressed
        if row.evidence_id == evidence_id
    )


def test_active_decision_neighborhood_is_not_seeded_from_candidates() -> None:
    active_subject = "src/auth/session.py"
    unrelated_subject = "src/cache/store.py"
    decision_id = "PATCH_CONSTRUCTION:fixture-window"
    active_record = _record(
        "active-contract",
        subject=active_subject,
        roles=(rr.EvidenceRole.BEHAVIORAL_CONTRACT,),
        decision_id=decision_id,
    )
    unrelated_record = _record(
        "unrelated-contract",
        subject=unrelated_subject,
        roles=(rr.EvidenceRole.AFFECTED_CALLER,),
        decision_id=decision_id,
    )

    active = seam.CanonicalRuntimeAttachment._active_decision(
        (active_record, unrelated_record),
        _work_state(active_subject, sequence=7),
        REVISION,
    )

    assert f"subject:{active_subject}" in active.causal_neighborhood
    assert f"subject:{unrelated_subject}" not in active.causal_neighborhood


def test_decision_anchor_alone_does_not_connect_unrelated_subjects() -> None:
    decision_id = "PATCH_CONSTRUCTION:session:window-7"
    active_subject = "src/auth/session.py"
    unrelated_subject = "src/cache/store.py"
    active = rr.ActiveDecision(
        decision_id=decision_id,
        context=rr.DecisionContext.PATCH_CONSTRUCTION,
        primary_claim="Repair the active session path.",
        required_roles=(rr.EvidenceRole.BEHAVIORAL_CONTRACT,),
        useful_roles=(rr.EvidenceRole.VALIDATION,),
        causal_neighborhood=(
            f"decision:{decision_id}",
            f"subject:{active_subject}",
        ),
        token_budget=100,
        current_revision=REVISION,
    )
    contract = _record(
        "session-contract",
        subject=active_subject,
        roles=(rr.EvidenceRole.BEHAVIORAL_CONTRACT,),
        decision_id=decision_id,
    )
    unrelated_validation = _record(
        "cache-validation",
        subject=unrelated_subject,
        roles=(rr.EvidenceRole.VALIDATION,),
        decision_id=decision_id,
    )

    oracle = rr.select_evidence_coalition(
        active,
        (contract, unrelated_validation),
    )

    assert tuple(
        item.evidence_id for item in oracle.coalition
    ) == ("session-contract",)
    assert (
        _suppression(oracle, "cache-validation").reason
        is rr.SuppressionReason.DISCONNECTED
    )


def test_decision_instance_id_is_deterministic_and_subject_specific() -> None:
    session_state = _work_state("src/auth/session.py", sequence=7)
    cache_state = _work_state("src/cache/store.py", sequence=7)

    session_first = seam.CanonicalRuntimeAttachment._active_decision(
        (),
        session_state,
        REVISION,
    )
    session_replay = seam.CanonicalRuntimeAttachment._active_decision(
        (),
        session_state,
        REVISION,
    )
    cache = seam.CanonicalRuntimeAttachment._active_decision(
        (),
        cache_state,
        REVISION,
    )

    assert session_first.context is cache.context
    assert session_first.decision_id == session_replay.decision_id
    assert session_first.decision_id != cache.decision_id
    assert session_first.decision_id.startswith(
        f"{rr.DecisionContext.PATCH_CONSTRUCTION.value}:"
    )


def test_decision_instance_id_changes_for_a_new_observable_window() -> None:
    first_window = seam.CanonicalRuntimeAttachment._active_decision(
        (),
        _work_state(
            "src/auth/session.py",
            sequence=7,
            decision_window_key="window-before-edit",
        ),
        REVISION,
    )
    next_window = seam.CanonicalRuntimeAttachment._active_decision(
        (),
        _work_state(
            "src/auth/session.py",
            sequence=7,
            decision_window_key="window-after-edit",
        ),
        REVISION,
    )

    assert first_window.context is next_window.context
    assert first_window.decision_id != next_window.decision_id
    assert first_window.decision_id.startswith(
        f"{rr.DecisionContext.PATCH_CONSTRUCTION.value}:"
    )
    assert next_window.decision_id.startswith(
        f"{rr.DecisionContext.PATCH_CONSTRUCTION.value}:"
    )
