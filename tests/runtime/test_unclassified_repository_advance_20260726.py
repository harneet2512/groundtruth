"""An untracked repository advance is a FACT, not corruption — record it, don't die on it.

WHAT THIS FIXES.  `reduce_event` enforced a symmetric repository-provenance invariant::

    if repository_changed and not repository_content_advanced:   raise   # (A)
    if not repository_changed and repository_content_advanced:   raise   # (B)

(A) is sound: GT recorded a mutation the repository does not corroborate, so GT's own
bookkeeping is lying. Keep it, forever.

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

import pytest

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


def test_the_OTHER_direction_still_raises_because_that_one_is_gt_lying():
    """ANTI-WEAKENING, and the reason this file is not just 'delete a check'.

    GT recorded a mutation the repository does not corroborate. That is GT's own bookkeeping
    contradicting observable truth, and it must still be fatal. If a future edit relaxes
    this one too, the reducer stops enforcing provenance at all.
    """
    with pytest.raises(rr.StateIntegrityError, match="did not advance"):
        _reduce(
            [
                rr.SemanticOutcome(
                    kind=rr.SemanticKind.EDIT_EXECUTED, subject="a.py", changed=True
                )
            ],
            after=BEFORE,
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
