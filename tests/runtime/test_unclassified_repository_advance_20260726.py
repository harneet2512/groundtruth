"""An untracked repository advance is a FACT, not corruption — record it, don't die on it.

WHAT THIS FIXES.  `reduce_event` enforced a symmetric repository-provenance invariant::

    if repository_changed and not repository_content_advanced:   raise   # (A)
    if not repository_changed and repository_content_advanced:   raise   # (B)

(A) WAS believed sound -- "GT recorded a mutation the repository does not corroborate, so
GT's own bookkeeping is lying". That was WRONG and is superseded (2026-07-27): a write of
identical bytes advances neither digest, the reducer cannot tell that from a hallucinated
mutation, and the raise is what killed the canonical observer on the first real observation
of run 30246661710. It is now the countable `no_op_mutation` rule. See
test_noop_mutation_does_not_kill_the_oracle_20260727.py.

(B) is not the same statement in reverse. `repository_content` is
`_canonical_repository_digest`: `git rev-parse HEAD` + `git status --porcelain` + `git diff
--binary` + the CONTENTS of untracked files, sampled live before and after every action. So
(B) fires whenever anything under the repo root moves without GT classifying it as a
mutation -- which is routine, not exceptional:

  * the agent runs the test suite and pytest writes an untracked cache or artifact
  * `pip install -e .` writes egg-info
  * a compiler, codegen step, or lockfile updater writes output
  * the agent mutates source through a shape GT does not classify as an edit
    (`sed -i`, `git apply`, a heredoc redirect, `python -c "open(...).write(...)"`)

Only the last of those is a GT blind spot, and none of them is GT lying about itself.

WHY THE OLD CONSEQUENCE WAS WRONG.  Raising here is not a safe conservative default; it is
the most destructive available response. `append_event` persists the event BEFORE reducing,
so the raise is caught as a `canonical_observer` fault, classified
`REDUCER_INVARIANT_VIOLATION` -- which is in `CORE_CORRUPTION_CODES` -- and replay recovery
re-reduces the same poisoned event and raises again, ending in `_quarantine(...)`:
`gt_emission_enabled=False` for the remainder of the attempt.

And a quarantined runtime does not degrade to the legacy path. `_augment_output` routes on
`attachment is None`, so once attached it never falls back. The attempt therefore ships ZERO
GT bytes -- strictly worse than the legacy route it replaced. One `pytest` invocation could
silently take an entire task's GT delivery to nothing.

THE FIX.  Keep the signal, drop the death: record `unclassified_repository_advance` in
`transition_rules` and adopt the advanced revision. Nothing is swallowed --

  * the rule is durable, replayable state, so an audit can count these directly;
  * `revision = event.revision_after` already ran, so freshness invalidation happens on its
    own and evidence keyed to the stale revision stops being fresh -- which is the correct
    handling of "the world moved under us", and is the SAME handling a classified mutation
    gets.

This does not weaken a bar. It makes the reducer's response proportionate to what it
observed, and it PRESERVES information the old path destroyed by killing the runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import groundtruth.runtime.reasoning_runtime as rr  # noqa: E402


BEFORE = rr.RevisionVector(
    repository_content="repo-1", graph="g-1", lsp="l-1", runtime_evidence="rt-1"
)
ADVANCED = rr.RevisionVector(
    repository_content="repo-2", graph="g-1", lsp="l-1", runtime_evidence="rt-1"
)


def _reduce(outcomes, before=BEFORE, after=ADVANCED):
    state = rr.WorkState.initial(attempt_id="a", revision=before)
    event = rr.CanonicalEvent(
        event_id="e-1",
        attempt_id="a",
        sequence=1,
        kind=rr.EventKind.OBSERVATION_COMMITTED,
        authority=rr.Authority.RESULT_DERIVED,
        outcomes=tuple(outcomes),
        revision_before=before,
        revision_after=after,
        previous_event_hash="",
        carrier="",
    )
    return rr.reduce_event(state, event)


def test_a_test_run_that_writes_an_untracked_file_does_not_kill_the_runtime():
    """The single most likely real trajectory: the agent runs the suite."""
    state = _reduce([rr.SemanticOutcome(kind=rr.SemanticKind.TEST_RESULT, subject="t.py")])
    assert "unclassified_repository_advance" in state.transition_rules
    assert state.revision.repository_content == "repo-2", (
        "the reducer must ADOPT the advanced revision so freshness invalidation runs; "
        "keeping the stale one would let evidence keyed to repo-1 stay 'fresh'"
    )


def test_an_unclassified_source_mutation_is_recorded_not_swallowed():
    """`sed -i` / `git apply` / heredoc: a real GT blind spot. It must remain COUNTABLE."""
    state = _reduce([])
    assert "unclassified_repository_advance" in state.transition_rules, (
        "the blind spot became invisible -- an audit can no longer count how often GT "
        "failed to classify a mutation the repository actually took"
    )


def test_the_rule_is_durable_replayable_state_not_a_log_line():
    """`transition_rules` is folded into WorkState and replayed, so the signal survives a
    recovery replay. A print or a fault would not."""
    state = _reduce([rr.SemanticOutcome(kind=rr.SemanticKind.TEST_RESULT, subject="t.py")])
    assert isinstance(state.transition_rules, tuple)
    assert state.transition_rules.count("unclassified_repository_advance") == 1, (
        "recorded zero times, or duplicated -- a replayed count must be exact"
    )


def test_the_rule_is_absent_when_the_repository_did_not_move():
    """NEAR-NEGATIVE. The rule must mean something; an always-on rule is noise."""
    state = _reduce(
        [rr.SemanticOutcome(kind=rr.SemanticKind.TEST_RESULT, subject="t.py")],
        after=BEFORE,
    )
    assert "unclassified_repository_advance" not in state.transition_rules


def test_the_rule_is_absent_when_the_advance_WAS_classified():
    """NEAR-NEGATIVE. A normal edit already explains the advance; flagging it would make
    every successful edit look like a blind spot."""
    state = _reduce(
        [
            rr.SemanticOutcome(
                kind=rr.SemanticKind.EDIT_EXECUTED, subject="a.py", changed=True
            )
        ]
    )
    assert "unclassified_repository_advance" not in state.transition_rules
    assert "repository_mutation" in state.transition_rules


def test_the_OTHER_direction_is_ALSO_recorded_not_fatal():
    """SUPERSEDED 2026-07-27 -- and the original assertion here was wrong.

    This test used to require the opposite direction (GT recorded a mutation the repository
    does not corroborate) to stay FATAL, on the argument that it was "GT's own bookkeeping
    contradicting observable truth". That argument did not survive a reproduction.

    `changed=True` means GT observed a WRITE, and `repository_content` digests HEAD + status
    + diff + untracked bytes -- so a write producing IDENTICAL BYTES advances neither. That
    is routine (a `sed -i` matching nothing, rewriting content already present, a retried
    editor action, a digest command failing to the same sentinel on both sides). The reducer
    cannot tell a hallucinated mutation from one that wrote identical bytes -- the two states
    are byte-identical -- so it cannot call one corruption.

    Driving a real AttemptReasoningRuntime through one edit cycle showed this raise is
    exactly what killed the canonical observer on run 30246661710: 45 oracle evaluations at
    iteration 0, then one `observe_failed:StateIntegrityError`, then `dark_fallback` forever.

    Both directions are now RECORDED, with distinct rules, and neither is fatal. The
    provenance signal is preserved and countable; only the death is removed. See
    test_noop_mutation_does_not_kill_the_oracle_20260727.py.
    """
    state = _reduce(
        [
            rr.SemanticOutcome(
                kind=rr.SemanticKind.EDIT_EXECUTED, subject="a.py", changed=True
            )
        ],
        after=BEFORE,
    )
    assert "no_op_mutation" in state.transition_rules
    assert "unclassified_repository_advance" not in state.transition_rules, (
        "the two directions must stay DISTINGUISHABLE -- collapsing them loses which one "
        "actually happened"
    )


def test_a_quarantine_from_this_cause_would_have_meant_zero_gt_bytes():
    """Documents the blast radius as an executable fact about the routing, so nobody
    re-introduces the raise thinking it degrades gracefully.

    `_augment_output` chooses the legacy path ONLY when the attachment is None. A runtime
    that attaches and is later quarantined is not None, so no fallback exists and delivery
    goes to zero for the rest of the attempt.
    """
    import inspect

    from artifact_deepswe import gt_mini_patch as seam

    src = inspect.getsource(seam._augment_output)
    assert "attachment is None" in src
    assert "_augment_output_legacy" in src
    body = src[src.index("attachment is None") :]
    assert "quarantin" not in body.lower(), (
        "if a quarantine-aware fallback now exists, update this test -- until then the "
        "cost of a mid-attempt quarantine is TOTAL delivery loss"
    )
