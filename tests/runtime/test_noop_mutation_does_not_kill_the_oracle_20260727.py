"""A no-op edit must not silence the oracle for the rest of the attempt.

THE BUG, REPRODUCED.  Driving a real ``AttemptReasoningRuntime`` through one edit cycle:

    edit changed=True,  repo digest ADVANCED   -> OK
    edit changed=True,  repo digest UNCHANGED  -> StateIntegrityError
                          "repository content revision did not advance for a mutation outcome"

That raise is what kills the canonical observer on the first real observation. Measured on
run 30246661710: 45 oracle evaluations at iteration 0, then ONE
``observe_failed:StateIntegrityError``, then ``dark_fallback`` on every iteration after --
the oracle never cycled again, and the attempt ran with no timing authority at all.

WHY THE RAISE IS WRONG.  When H2 relaxed the opposite direction (repo advanced with no
mutation outcome) this direction was KEPT on the argument that "GT recorded a mutation the
repository does not corroborate, so GT's own bookkeeping is lying". That argument does not
survive contact with the data.

``changed=True`` means GT OBSERVED A WRITE (``changed_files`` non-empty from the edit
bridge). ``repository_content`` is a digest of HEAD + status + diff + untracked bytes. A
write that produces IDENTICAL BYTES advances neither -- and that is routine, not exotic:

  * ``sed -i`` whose pattern matches nothing
  * rewriting a file with the content it already had
  * an editor action the agent retries after it already landed
  * any digest-command failure that returns the same ``unavailable:`` sentinel on both sides

In every one of those the write is real and the content is unchanged. The reducer cannot
distinguish "GT hallucinated a mutation" from "the mutation wrote identical bytes" -- the two
produce byte-identical state -- so it cannot justify treating one as corruption. And the
penalty is wildly disproportionate: `append_event` persists before reducing, the raise is
classified REDUCER_INVARIANT_VIOLATION (a CORE corruption code), replay re-reduces the same
poisoned event, and the attempt is quarantined.

THE FIX.  Record ``no_op_mutation`` in ``transition_rules`` and carry on. Nothing is
swallowed: the rule is durable replayable state an audit can count, and "the agent believes
it edited but the repository did not change" is itself a useful signal -- one worth
surfacing, not one worth dying on. Symmetric with ``unclassified_repository_advance``.

DO NOT "fix" a failure here by also relaxing the sequence, attempt-identity, or hash-chain
invariants. Those are genuine integrity checks with no benign trigger; this one is not.
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


def _reduce(outcomes, before=BEFORE, after=BEFORE):
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


def _edit(changed=True):
    return rr.SemanticOutcome(
        kind=rr.SemanticKind.EDIT_EXECUTED, subject="src/x.py", changed=changed
    )


def test_a_write_that_changed_no_bytes_does_not_raise():
    """THE FIX. The exact shape that killed the oracle on run 30246661710."""
    state = _reduce([_edit(changed=True)], after=BEFORE)
    assert "no_op_mutation" in state.transition_rules, (
        "a no-op edit is recorded, not fatal -- dying here silences the oracle for every "
        "remaining observation of the attempt"
    )


def test_the_no_op_is_countable_not_swallowed():
    """`transition_rules` is folded into WorkState and replayed, so an audit can count how
    often the agent believed it edited while the repository did not move."""
    state = _reduce([_edit(changed=True)], after=BEFORE)
    assert isinstance(state.transition_rules, tuple)
    assert state.transition_rules.count("no_op_mutation") == 1


def test_a_real_edit_is_untouched_and_never_flagged():
    """NEAR-NEGATIVE. A normal landed edit must not be marked a no-op, or the signal is
    noise and every successful edit looks suspicious."""
    state = _reduce([_edit(changed=True)], after=ADVANCED)
    assert "no_op_mutation" not in state.transition_rules
    assert "repository_mutation" in state.transition_rules


def test_no_flag_when_nothing_claimed_a_mutation():
    """NEAR-NEGATIVE. The rule must mean 'a claimed write moved no bytes', not fire on any
    quiet observation."""
    state = _reduce(
        [rr.SemanticOutcome(kind=rr.SemanticKind.TEST_RESULT, subject="t.py")],
        after=BEFORE,
    )
    assert "no_op_mutation" not in state.transition_rules


def test_the_other_direction_still_records_its_own_rule():
    """The H2 relaxation must survive this change: repo advanced with no mutation outcome
    stays recorded (not fatal, not silent)."""
    state = _reduce(
        [rr.SemanticOutcome(kind=rr.SemanticKind.TEST_RESULT, subject="t.py")],
        after=ADVANCED,
    )
    assert "unclassified_repository_advance" in state.transition_rules


@pytest.mark.parametrize(
    ("msg", "build"),
    [
        ("attempt identity mismatch", "attempt"),
        ("event sequence gap", "sequence"),
    ],
)
def test_genuine_integrity_invariants_are_untouched(msg, build):
    """ANTI-WEAKENING, and the reason this file is not 'delete another check'.

    Attempt identity and event sequencing have NO benign trigger -- a mismatch there means
    the event stream itself is corrupt. They must still raise.
    """
    state = rr.WorkState.initial(attempt_id="a", revision=BEFORE)
    bad_attempt = build == "attempt"
    event = rr.CanonicalEvent(
        event_id="e-1",
        attempt_id="OTHER" if bad_attempt else "a",
        sequence=1 if bad_attempt else 99,
        kind=rr.EventKind.OBSERVATION_COMMITTED,
        authority=rr.Authority.RESULT_DERIVED,
        outcomes=(),
        revision_before=BEFORE,
        revision_after=BEFORE,
        previous_event_hash="",
        carrier="",
    )
    with pytest.raises(rr.StateIntegrityError, match=msg):
        rr.reduce_event(state, event)
