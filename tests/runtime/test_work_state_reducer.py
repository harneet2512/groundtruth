"""RED contract for the immutable, semantic-outcome-driven work-state reducer."""
from __future__ import annotations

import pytest

from groundtruth.runtime.reasoning_runtime import (
    Authority,
    CanonicalEvent,
    EventKind,
    Phase,
    RevisionVector,
    SemanticKind,
    SemanticOutcome,
    StateIntegrityError,
    WorkState,
    reduce_event,
)


def _revision(repo: str) -> RevisionVector:
    return RevisionVector(
        repository_content=repo,
        graph="graph-1",
        lsp="lsp-1",
        runtime_evidence="runtime-1",
    )


def _event(
    sequence: int,
    *,
    before: RevisionVector,
    after: RevisionVector | None = None,
    outcomes: tuple[SemanticOutcome, ...] = (),
    carrier: str = "",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=f"ev-{sequence}",
        attempt_id="attempt-1",
        sequence=sequence,
        kind=EventKind.OBSERVATION_COMMITTED,
        authority=Authority.RESULT_DERIVED,
        outcomes=outcomes,
        revision_before=before,
        revision_after=after or before,
        previous_event_hash="",
        carrier=carrier,
    )


def test_reducer_returns_a_new_state_without_aliasing_or_mutating_input():
    rev1, rev2 = _revision("repo-1"), _revision("repo-2")
    original = WorkState.initial(attempt_id="attempt-1", revision=rev1)
    before_bytes = original.canonical_json()

    reduced = reduce_event(
        original,
        _event(
            1,
            before=rev1,
            after=rev2,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.EDIT_EXECUTED,
                    subject="src/auth.py",
                    changed=True,
                ),
            ),
        ),
    )

    assert reduced is not original
    assert original.canonical_json() == before_bytes
    assert original.edited_files == ()
    assert reduced.edited_files == ("src/auth.py",)
    assert isinstance(reduced.edited_files, tuple)


def test_phase_test_and_edit_state_come_from_semantic_outcomes_not_carrier_text():
    rev1, rev2 = _revision("repo-1"), _revision("repo-2")
    state = WorkState.initial(attempt_id="attempt-1", revision=rev1)

    state = reduce_event(
        state,
        _event(
            1,
            before=rev1,
            outcomes=(
                SemanticOutcome(kind=SemanticKind.SOURCE_VIEWED, subject="src/auth.py"),
            ),
            carrier="pytest -q && echo looks-like-a-test",
        ),
    )
    assert state.phase is Phase.UNDERSTANDING
    assert state.test_count == 0

    state = reduce_event(
        state,
        _event(
            2,
            before=rev1,
            after=rev2,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.EDIT_EXECUTED,
                    subject="src/auth.py",
                    changed=True,
                ),
            ),
        ),
    )
    assert state.phase is Phase.IMPLEMENTATION
    assert state.edited_files == ("src/auth.py",)

    state = reduce_event(
        state,
        _event(
            3,
            before=rev2,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.TEST_FAIL,
                    subject="tests/test_auth.py",
                    failure_fingerprint="red-1",
                ),
            ),
        ),
    )
    assert state.phase is Phase.RECOVERY
    assert state.test_count == 1
    assert state.current_failures == ("red-1",)
    assert state.transition_rules[-2:] == (
        "failure_after_edit",
        "validation_result",
    )


def test_first_failure_without_repository_edit_remains_validation():
    rev = _revision("repo-1")
    state = reduce_event(
        WorkState.initial(attempt_id="attempt-1", revision=rev),
        _event(
            1,
            before=rev,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.TEST_FAIL,
                    subject="tests/test_auth.py",
                    failure_fingerprint="red-1",
                ),
            ),
        ),
    )

    assert state.phase is Phase.VALIDATION
    assert "failure_after_edit" not in state.transition_rules
    assert "repeated_failure_after_edit" not in state.transition_rules


def test_repeated_same_failure_after_repository_edit_uses_distinct_recovery_rule():
    rev1, rev2 = _revision("repo-1"), _revision("repo-2")
    state = reduce_event(
        WorkState.initial(attempt_id="attempt-1", revision=rev1),
        _event(
            1,
            before=rev1,
            after=rev2,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.EDIT_EXECUTED,
                    subject="src/auth.py",
                    changed=True,
                ),
            ),
        ),
    )
    state = reduce_event(
        state,
        _event(
            2,
            before=rev2,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.TEST_FAIL,
                    failure_fingerprint="red-1",
                ),
            ),
        ),
    )
    assert state.transition_rules[-2:] == (
        "failure_after_edit",
        "validation_result",
    )
    state = reduce_event(
        state,
        _event(
            3,
            before=rev2,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.TEST_FAIL,
                    failure_fingerprint="red-1",
                ),
            ),
        ),
    )

    assert state.phase is Phase.RECOVERY
    assert state.transition_rules.count("failure_after_edit") == 1
    assert state.transition_rules.count("repeated_failure_after_edit") == 1
    assert state.transition_rules[-2:] == (
        "repeated_failure_after_edit",
        "validation_result",
    )


def test_environment_failure_behavior_is_unchanged_after_repository_edit():
    rev1, rev2 = _revision("repo-1"), _revision("repo-2")
    state = reduce_event(
        WorkState.initial(attempt_id="attempt-1", revision=rev1),
        _event(
            1,
            before=rev1,
            after=rev2,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.EDIT_EXECUTED,
                    subject="src/auth.py",
                    changed=True,
                ),
            ),
        ),
    )
    state = reduce_event(
        state,
        _event(
            2,
            before=rev2,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.TEST_ENV_FAIL,
                    failure_fingerprint="env-1",
                ),
            ),
        ),
    )
    assert state.phase is Phase.VALIDATION
    assert "failure_after_edit" not in state.transition_rules

    state = reduce_event(
        state,
        _event(
            3,
            before=rev2,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.TEST_ENV_FAIL,
                    failure_fingerprint="env-1",
                ),
            ),
        ),
    )
    assert state.phase is Phase.RECOVERY
    assert state.transition_rules[-2:] == (
        "repeated_failure_after_edit",
        "validation_result",
    )


def test_search_without_a_selected_symbol_moves_orientation_to_discovery():
    rev = _revision("repo-1")
    state = reduce_event(
        WorkState.initial(attempt_id="attempt-1", revision=rev),
        _event(
            1,
            before=rev,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.SEARCH_RESULT,
                    subject="refreshSession",
                    status="success",
                ),
            ),
        ),
    )
    assert state.phase is Phase.DISCOVERY


def test_noop_edit_does_not_create_edit_state_or_advance_revision():
    rev = _revision("repo-1")
    state = WorkState.initial(attempt_id="attempt-1", revision=rev)
    reduced = reduce_event(
        state,
        _event(
            1,
            before=rev,
            after=rev,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.EDIT_EXECUTED,
                    subject="src/auth.py",
                    changed=False,
                ),
            ),
        ),
    )
    assert reduced.edited_files == ()
    assert reduced.phase is Phase.ORIENTATION
    assert reduced.revision == rev


def test_revision_mismatch_is_rejected_instead_of_reducing_stale_truth():
    state = WorkState.initial(attempt_id="attempt-1", revision=_revision("repo-current"))
    stale_event = _event(1, before=_revision("repo-stale"))

    with pytest.raises(StateIntegrityError, match="revision"):
        reduce_event(state, stale_event)


def test_repository_mutation_without_content_change_is_recorded_not_fatal():
    """UPDATED 2026-07-27. This asserted a `StateIntegrityError`; it now asserts a rule.

    The old contract treated "an edit reported changed=True but repository_content did not
    advance" as corruption. A reproduction against a real AttemptReasoningRuntime showed
    that is exactly the raise that kills the canonical observer on the first real
    observation -- run 30246661710: 45 oracle evaluations at iteration 0, then one
    `observe_failed:StateIntegrityError`, then `dark_fallback` for the rest of the attempt.

    The premise was wrong. `changed=True` means GT observed a WRITE; `repository_content`
    digests HEAD + status + diff + untracked bytes, so a write of IDENTICAL bytes advances
    neither. That happens routinely (`sed -i` matching nothing, rewriting content already
    present, a retried editor action, a digest command failing to the same sentinel twice),
    and the reducer cannot distinguish it from a hallucinated mutation because the two
    produce byte-identical state.

    The signal is preserved as a countable `no_op_mutation` rule; only the death is removed.
    Genuine integrity invariants (attempt identity, event sequencing) still raise -- see
    test_noop_mutation_does_not_kill_the_oracle_20260727.py.
    """
    before = _revision("repo-1")
    graph_only = RevisionVector(
        repository_content="repo-1",
        graph="graph-2",
        lsp="lsp-1",
        runtime_evidence="runtime-1",
    )
    event = _event(
        1,
        before=before,
        after=graph_only,
        outcomes=(
            SemanticOutcome(
                kind=SemanticKind.EDIT_EXECUTED,
                subject="src/auth.py",
                changed=True,
            ),
        ),
    )

    state = reduce_event(
        WorkState.initial(attempt_id="attempt-1", revision=before), event
    )
    assert "no_op_mutation" in state.transition_rules
    assert "repository_mutation" in state.transition_rules, (
        "the mutation itself must still be recorded -- only the fatality is removed"
    )


def test_non_repository_revision_refresh_advances_revision_without_fake_edit():
    before = _revision("repo-1")
    graph_refresh = RevisionVector(
        repository_content="repo-1",
        graph="graph-2",
        lsp="lsp-2",
        runtime_evidence="runtime-1",
    )
    reduced = reduce_event(
        WorkState.initial(attempt_id="attempt-1", revision=before),
        _event(
            1,
            before=before,
            after=graph_refresh,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.SOURCE_VIEWED,
                    subject="src/auth.py",
                ),
            ),
        ),
    )

    assert reduced.revision == graph_refresh
    assert reduced.edited_files == ()


def test_unknown_edit_delta_is_not_promoted_to_a_repository_mutation():
    rev = _revision("repo-1")
    reduced = reduce_event(
        WorkState.initial(attempt_id="attempt-1", revision=rev),
        _event(
            1,
            before=rev,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.EDIT_EXECUTED,
                    subject="src/auth.py",
                    changed=None,
                ),
            ),
        ),
    )

    assert reduced.edited_files == ()
    assert reduced.phase is Phase.ORIENTATION


def test_one_compound_test_observation_counts_once_and_compile_is_separate():
    rev = _revision("repo-1")
    state = reduce_event(
        WorkState.initial(attempt_id="attempt-1", revision=rev),
        _event(
            1,
            before=rev,
            outcomes=(
                SemanticOutcome(kind=SemanticKind.TEST_RESULT),
                SemanticOutcome(
                    kind=SemanticKind.TEST_FAIL,
                    failure_fingerprint="red-1",
                ),
            ),
        ),
    )
    assert state.test_count == 1
    assert state.compile_count == 0

    state = reduce_event(
        state,
        _event(
            2,
            before=rev,
            outcomes=(SemanticOutcome(kind=SemanticKind.COMPILE_RESULT),),
        ),
    )
    assert state.test_count == 1
    assert state.compile_count == 1


def test_scoped_test_pass_clears_only_failures_for_the_same_validation_scope():
    rev = _revision("repo-1")
    state = WorkState.initial(attempt_id="attempt-1", revision=rev)
    state = reduce_event(
        state,
        _event(
            1,
            before=rev,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.TEST_FAIL,
                    subject="tests/test_auth.py",
                    failure_fingerprint="same-output",
                ),
                SemanticOutcome(
                    kind=SemanticKind.TEST_FAIL,
                    subject="tests/test_cache.py",
                    failure_fingerprint="same-output",
                ),
            ),
        ),
    )

    assert len(state.failure_scopes) == 2
    state = reduce_event(
        state,
        _event(
            2,
            before=rev,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.TEST_PASS,
                    subject="tests/test_auth.py",
                ),
            ),
        ),
    )

    assert state.current_failures == ("same-output",)
    assert len(state.failure_scopes) == 1
    assert state.failure_scopes[0][1] == "same-output"


def test_scoped_repeated_failure_still_enters_recovery_after_edit():
    rev1, rev2 = _revision("repo-1"), _revision("repo-2")
    state = reduce_event(
        WorkState.initial(attempt_id="attempt-1", revision=rev1),
        _event(
            1,
            before=rev1,
            after=rev2,
            outcomes=(
                SemanticOutcome(
                    kind=SemanticKind.EDIT_EXECUTED,
                    subject="src/auth.py",
                    changed=True,
                ),
            ),
        ),
    )
    for sequence in (2, 3):
        state = reduce_event(
            state,
            _event(
                sequence,
                before=rev2,
                outcomes=(
                    SemanticOutcome(
                        kind=SemanticKind.TEST_FAIL,
                        subject="tests/test_auth.py",
                        failure_fingerprint="red-1",
                    ),
                ),
            ),
        )

    assert state.phase is Phase.RECOVERY
    assert state.transition_rules.count("repeated_failure_after_edit") == 1


def test_decision_window_key_is_replay_stable_and_advances_at_commitment_boundary():
    rev = _revision("repo-1")
    initial = WorkState.initial(attempt_id="attempt-1", revision=rev)
    viewed = _event(
        1,
        before=rev,
        outcomes=(
            SemanticOutcome(
                kind=SemanticKind.SOURCE_VIEWED,
                subject="src/auth.py",
            ),
        ),
    )
    first = reduce_event(initial, viewed)
    replay = reduce_event(initial, viewed)
    assert first.decision_window_key == replay.decision_window_key == viewed.event_id

    proposed = _event(
        2,
        before=rev,
        outcomes=(
            SemanticOutcome(
                kind=SemanticKind.EDIT_PROPOSED,
                subject="src/auth.py",
            ),
        ),
    )
    second = reduce_event(first, proposed)
    assert second.decision_window_key == proposed.event_id
    assert second.decision_window_key != first.decision_window_key
