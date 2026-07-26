"""RED contract for the canonical event fabric.

The runtime receives already-normalized semantic truth.  These tests deliberately
avoid shell parsing: authority, causal identity, revisions, and compound outcomes
must be stable before reducers or producers consume an event.
"""
from __future__ import annotations

import json

from groundtruth.runtime.reasoning_runtime import (
    Authority,
    CanonicalEvent,
    CausalRef,
    CausalRefKind,
    EventKind,
    RevisionVector,
    SemanticKind,
    SemanticOutcome,
)


def _revision(repo: str = "repo-1") -> RevisionVector:
    return RevisionVector(
        repository_content=repo,
        graph="graph-1",
        lsp="lsp-1",
        runtime_evidence="runtime-1",
    )


def test_authority_precedence_is_structured_before_all_fallbacks():
    assert (
        Authority.STRUCTURED
        > Authority.RESULT_DERIVED
        > Authority.REPOSITORY_DELTA
        > Authority.RESULT_SHAPE
        > Authority.COMMAND_FALLBACK
        > Authority.LEGACY_INFERRED
    )


def test_observation_preserves_ordered_compound_semantic_outcomes():
    rev = _revision()
    event = CanonicalEvent(
        event_id="ev-1",
        attempt_id="attempt-1",
        sequence=1,
        kind=EventKind.OBSERVATION_COMMITTED,
        authority=Authority.RESULT_DERIVED,
        outcomes=(
            SemanticOutcome(
                kind=SemanticKind.EDIT_EXECUTED,
                subject="src/session.py",
                changed=True,
                authority=Authority.REPOSITORY_DELTA,
                provenance=("repository_diff",),
            ),
            SemanticOutcome(
                kind=SemanticKind.TEST_FAIL,
                subject="tests/test_session.py",
                failure_fingerprint="failure-7",
                authority=Authority.RESULT_DERIVED,
                provenance=("exit_status",),
            ),
        ),
        revision_before=rev,
        revision_after=_revision("repo-2"),
        previous_event_hash="",
        action_id="action-1",
        observation_id="obs-1",
        parents=(
            CausalRef(CausalRefKind.ACTION, "action-1"),
            CausalRef(CausalRefKind.MODEL_CALL, "model-1"),
        ),
    )

    encoded = json.loads(event.canonical_json())
    assert [row["kind"] for row in encoded["outcomes"]] == [
        "EDIT_EXECUTED",
        "TEST_FAIL",
    ]
    assert event.outcomes[0].subject == "src/session.py"
    assert event.outcomes[1].failure_fingerprint == "failure-7"
    assert event.outcomes[0].authority is Authority.REPOSITORY_DELTA
    assert event.outcomes[1].authority is Authority.RESULT_DERIVED
    assert [row["kind"] for row in encoded["parents"]] == ["ACTION", "MODEL_CALL"]


def test_canonical_serialization_and_hash_are_content_deterministic():
    kwargs = dict(
        event_id="ev-stable",
        attempt_id="attempt-1",
        sequence=3,
        kind=EventKind.OBSERVATION_COMMITTED,
        authority=Authority.STRUCTURED,
        outcomes=(
            SemanticOutcome(
                kind=SemanticKind.SEARCH_RESULT,
                subject="refreshSession",
                status="success",
            ),
        ),
        revision_before=_revision(),
        revision_after=_revision(),
        previous_event_hash="parent-hash",
        action_id="action-3",
        observation_id="obs-3",
        carrier='rg "refreshSession" src',
    )

    first = CanonicalEvent(**kwargs)
    second = CanonicalEvent(**kwargs)
    assert first.canonical_json().encode("utf-8") == second.canonical_json().encode("utf-8")
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64
    assert "timestamp" not in first.canonical_json().lower()

    changed = CanonicalEvent(**{**kwargs, "event_id": "ev-different"})
    assert changed.content_hash != first.content_hash


def test_nested_collections_are_frozen_copies_not_mutable_aliases():
    metadata = [["query", "refreshSession"]]
    outcome = SemanticOutcome(
        kind=SemanticKind.SEARCH_RESULT,
        subject="refreshSession",
        metadata=metadata,  # type: ignore[arg-type]
    )
    event = CanonicalEvent(
        event_id="ev-frozen",
        attempt_id="attempt-1",
        sequence=1,
        kind=EventKind.OBSERVATION_COMMITTED,
        authority=Authority.STRUCTURED,
        outcomes=[outcome],  # type: ignore[arg-type]
        revision_before=_revision(),
        revision_after=_revision(),
        previous_event_hash="",
    )
    before = event.content_hash

    metadata.append(["late", "mutation"])

    assert event.content_hash == before
    assert event.outcomes == (
        SemanticOutcome(
            kind=SemanticKind.SEARCH_RESULT,
            subject="refreshSession",
            metadata=(("query", "refreshSession"),),
        ),
    )
