"""RED contract for the Mini-SWE -> canonical event boundary.

The adapter is deliberately tested from the current ``gateway.ToolEvent`` ABI.
It may translate structured seam facts, but it must not parse the command again
or let a carrier label override authoritative repository/test truth.
"""

from __future__ import annotations

from groundtruth.runtime.adapters.miniswe import (
    canonicalize_tool_event,
    normalize_event,
)
from groundtruth.runtime.gateway import ToolEvent
from groundtruth.runtime.reasoning_runtime import (
    Authority,
    CausalRef,
    CausalRefKind,
    EventKind,
    RevisionVector,
    SemanticKind,
)


def _revision(repository_content: str) -> RevisionVector:
    return RevisionVector(
        repository_content=repository_content,
        graph="graph-17",
        lsp="lsp-9",
        runtime_evidence="runtime-31",
    )


def _canonicalize(
    event: ToolEvent,
    *,
    event_id: str = "ev-0007",
    sequence: int = 7,
    revision_before: RevisionVector | None = None,
    revision_after: RevisionVector | None = None,
    previous_event_hash: str = "",
    parent_event_ids: tuple[str, ...] = (),
):
    before = revision_before or _revision("repo-before")
    after = revision_after or before
    return canonicalize_tool_event(
        event,
        event_id=event_id,
        attempt_id="attempt-3",
        sequence=sequence,
        action_id="action-7",
        model_turn_id="model-call-5",
        observation_id="observation-6",
        revision_before=before,
        revision_after=after,
        previous_event_hash=previous_event_hash,
        parent_event_ids=parent_event_ids,
    )


def test_authoritative_compound_edit_then_test_preserves_causal_outcome_order():
    """A carrier called ``other`` may still contain an edit followed by a test."""
    event = ToolEvent(
        kind="other",
        carrier_kind="other",
        command="python tools/apply_and_check.py",
        output="1 failed",
        exit_status=1,
        changed_files=("src/session.py",),
        semantic_events=("edit_result", "test_result"),
        primary_boundary="test_result",
        test_outcome="fail",
        test_protocol="pytest",
        state_revision="7",
        semantics_authoritative=True,
    )

    canonical = _canonicalize(
        event,
        revision_before=_revision("repo-before"),
        revision_after=_revision("repo-after"),
    )

    assert canonical.kind is EventKind.OBSERVATION_COMMITTED
    assert canonical.carrier == "other"
    assert [item.kind for item in canonical.outcomes] == [
        SemanticKind.EDIT_EXECUTED,
        SemanticKind.DIFF_CREATED,
        SemanticKind.TEST_RESULT,
        SemanticKind.TEST_FAIL,
    ]
    assert [item.subject for item in canonical.outcomes[:2]] == [
        "src/session.py",
        "src/session.py",
    ]
    assert canonical.revision_before.repository_content == "repo-before"
    assert canonical.revision_after.repository_content == "repo-after"


def test_each_outcome_keeps_the_authority_of_its_own_evidence_surface():
    """Structured event ownership must not flatten all claims to one grade."""
    event = ToolEvent(
        kind="other",
        carrier_kind="other",
        command="opaque-tool-call",
        output="FAILED",
        exit_status=1,
        changed_files=("src/session.py",),
        semantic_events=("edit_result", "test_result"),
        test_outcome="fail",
        test_protocol="structured",
        semantics_authoritative=True,
    )

    canonical = _canonicalize(
        event,
        revision_before=_revision("repo-before"),
        revision_after=_revision("repo-after"),
    )

    assert canonical.authority is Authority.STRUCTURED
    assert [item.authority for item in canonical.outcomes] == [
        Authority.REPOSITORY_DELTA,
        Authority.REPOSITORY_DELTA,
        Authority.RESULT_DERIVED,
        Authority.RESULT_DERIVED,
    ]
    assert canonical.outcomes[0].provenance == ("changed_files",)
    assert canonical.outcomes[2].provenance == (
        "test_outcome",
        "test_protocol",
        "exit_status",
    )


def test_authoritative_exact_noop_never_recovers_edit_or_test_from_carrier_text():
    """An empty authoritative semantic set is a fact, not missing parser input."""
    revision = _revision("repo-unchanged")
    event = ToolEvent(
        kind="edit",
        carrier_kind="edit",
        command="sed -i 's/x/x/' src/session.py && pytest -q",
        output="1 failed",
        exit_status=1,
        changed_files=(),
        semantic_events=(),
        primary_boundary="",
        test_outcome="",
        test_protocol="",
        state_revision="8",
        semantics_authoritative=True,
    )

    canonical = _canonicalize(
        event,
        revision_before=revision,
        revision_after=revision,
    )

    assert canonical.carrier == "edit"
    assert canonical.outcomes == ()
    assert canonical.revision_before == canonical.revision_after
    assert event.semantic_events == ()
    assert event.primary_boundary == ""


def test_ids_causal_refs_revision_vectors_and_hash_chain_are_stable():
    event = ToolEvent(
        kind="view",
        carrier_kind="view",
        command="sed -n '1,80p' src/session.py",
        output="def refresh_session(): ...",
        exit_status=0,
        semantic_events=("file_view",),
        primary_boundary="file_view",
        semantics_authoritative=True,
    )
    before = _revision("repo-a")
    first = _canonicalize(
        event,
        event_id="ev-0001",
        sequence=1,
        revision_before=before,
        revision_after=before,
        parent_event_ids=("ev-task",),
    )
    second = _canonicalize(
        event,
        event_id="ev-0002",
        sequence=2,
        revision_before=before,
        revision_after=before,
        previous_event_hash=first.content_hash,
        parent_event_ids=("ev-0001",),
    )
    recreated = _canonicalize(
        event,
        event_id="ev-0002",
        sequence=2,
        revision_before=before,
        revision_after=before,
        previous_event_hash=first.content_hash,
        parent_event_ids=("ev-0001",),
    )

    assert first.action_id == "action-7"
    assert first.model_turn_id == "model-call-5"
    assert first.observation_id == "observation-6"
    assert first.parents == (
        CausalRef(CausalRefKind.EVENT, "ev-task"),
        CausalRef(CausalRefKind.ACTION, "action-7"),
        CausalRef(CausalRefKind.MODEL_CALL, "model-call-5"),
        CausalRef(CausalRefKind.OBSERVATION, "observation-6"),
    )
    assert second.previous_event_hash == first.content_hash
    assert second.parents[0] == CausalRef(CausalRefKind.EVENT, "ev-0001")
    assert second.content_hash == recreated.content_hash
    assert second.canonical_json() == recreated.canonical_json()


def test_current_normalize_event_tool_event_is_accepted_without_mutation():
    """Compatibility is required while the single live seam migrates atomically."""
    legacy = normalize_event(
        "timeout 30 ./target/debug/deps/engine-abcd tests::job --nocapture",
        "running 1 test\ntest tests::job ... FAILED",
        101,
        12,
        semantic_events=("test_result",),
        test_outcome="fail",
        test_protocol="native",
        state_revision="12",
    )
    original = (
        legacy.kind,
        legacy.carrier_kind,
        legacy.semantic_events,
        legacy.primary_boundary,
        legacy.changed_files,
    )

    canonical = _canonicalize(legacy)

    assert canonical.carrier == "other"
    assert [item.kind for item in canonical.outcomes] == [
        SemanticKind.TEST_RESULT,
        SemanticKind.TEST_FAIL,
    ]
    assert canonical.outcomes[1].failure_fingerprint
    recreated = _canonicalize(legacy)
    assert (
        recreated.outcomes[1].failure_fingerprint
        == canonical.outcomes[1].failure_fingerprint
    )
    assert (
        legacy.kind,
        legacy.carrier_kind,
        legacy.semantic_events,
        legacy.primary_boundary,
        legacy.changed_files,
    ) == original
