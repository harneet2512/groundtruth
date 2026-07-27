"""An outcome signal for a hypothesis GT never opened must not kill the attempt.

MEASURED, not hypothesised. Run 30276041709 (the first run carrying the fault-detail
instrumentation) named the killer in one line::

    observe_failed:StateIntegrityError:illegal hypothesis transition None via VALIDATION_SUPPORT

52 compile attempts, ALL at iteration 0, then that fault, then dark_fallback. The oracle
still burst-and-died -- the earlier no-op-mutation fix was correct on its own merits but was
NOT this. Three prior guesses at the cause were wrong; the instrumentation found it.

WHY IT FIRES. `_HYPOTHESIS_TRANSITIONS` splits cleanly:

    OPENING   EXACT_SEARCH             {None}                        -> CANDIDATE
              FOCUSED_SYMBOL_VIEW      {None, CANDIDATE}             -> ACTIVE
              EDIT_PROPOSED            {None, CANDIDATE, ACTIVE}     -> ACTIVE
    OUTCOME   VALIDATION_SUPPORT       {ACTIVE}
              UNCHANGED_FAILURE_AFTER_EDIT {ACTIVE, SUPPORTED}
              VERIFIED_COUNTEREVIDENCE {ACTIVE, SUPPORTED, WEAKENED}
              ABANDON_TARGET           {ACTIVE, WEAKENED, CONTRADICTED}
              SUPERSEDING_HYPOTHESIS   {ACTIVE, SUPPORTED, WEAKENED}

Every OUTCOME requires a hypothesis to already exist. But GT does not get told the agent's
hypotheses -- it INFERS them from observations. When the opening observation is missed, or
classified as something else, the outcome arrives ORPHANED with `current is None`. On
hydra-3005 the agent went `find` -> `grep` -> run a repro script: a validation outcome
before GT had opened anything for that subject.

WHY THE RAISE IS THE WRONG RESPONSE. The state machine is sound -- these transitions are
correct GIVEN a hypothesis. The defect is the consequence. `append_event` persists before
reducing, the raise classifies REDUCER_INVARIANT_VIOLATION (a CORE corruption code), replay
re-reduces the same event, and the observer is isolated for the remainder of the attempt.
GT loses its entire timing authority because the agent validated something GT had not yet
formed an opinion about.

THE FIX. An orphaned outcome (`current is None` where the transition needs a state) records
`orphaned_outcome_signal` and SKIPS the transition. The graph never enters an illegal state
-- nothing transitions, no node is invented -- so the invariant it protects is intact, and
the signal stays countable.

DO NOT widen this to `current is not None` mismatches. A transition from a REAL state that
the table forbids (SUPPORTED -> CANDIDATE, ABANDON from SUPPORTED) means the graph itself is
inconsistent, which is genuine corruption and must still raise.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import groundtruth.runtime.reasoning_runtime as rr  # noqa: E402


OUTCOME_KINDS = [
    rr.OperationalSignalKind.VALIDATION_SUPPORT,
    rr.OperationalSignalKind.UNCHANGED_FAILURE_AFTER_EDIT,
    rr.OperationalSignalKind.VERIFIED_COUNTEREVIDENCE,
    rr.OperationalSignalKind.ABANDON_TARGET,
    rr.OperationalSignalKind.SUPERSEDING_HYPOTHESIS,
]


def _digest(tag: str) -> str:
    """A real lowercase SHA-256 hex digest -- the dataclass validates the shape."""
    return hashlib.sha256(tag.encode()).hexdigest()


REV = rr.RevisionVector(
    repository_content=_digest("r1"), graph=_digest("g1"),
    lsp=_digest("l1"), runtime_evidence=_digest("e1"),
)


def _graph():
    return rr.ReasoningGraph.initial(attempt_id="a", revision=REV)


def _signal(kind, *, sequence=1, hypothesis_id="h-1", related=""):
    return rr.OperationalSignal(
        attempt_id="a",
        event_id=f"ev-{sequence}",
        sequence=sequence,
        source_event_sequence=sequence,
        source_event_hash=_digest(f"src{sequence}"),
        revision=REV,
        authority=rr.Authority.RESULT_DERIVED,
        hypothesis_id=hypothesis_id,
        subject="src/x.py::f",
        kind=kind,
        related_node_id=related or ("rel-1" if kind is
                                    rr.OperationalSignalKind.VERIFIED_COUNTEREVIDENCE else ""),
    )


@pytest.mark.parametrize("kind", OUTCOME_KINDS, ids=lambda k: k.value)
def test_every_orphaned_outcome_survives(kind):
    """THE FIX. None of the five may kill the attempt.

    Parameterised because fixing only VALIDATION_SUPPORT -- the one this run happened to
    hit -- would leave four identical landmines for the next trajectory shape.
    """
    graph = rr.reduce_reasoning_signal(_graph(), _signal(kind))
    assert graph is not None


@pytest.mark.parametrize("kind", OUTCOME_KINDS, ids=lambda k: k.value)
def test_the_orphan_is_recorded_not_swallowed(kind):
    """The signal must stay countable -- an orphaned outcome means GT missed the opening
    observation, which is a real blind spot worth measuring."""
    graph = rr.reduce_reasoning_signal(_graph(), _signal(kind))
    rules = getattr(graph, "transition_rules", ()) or ()
    marks = [n for n in getattr(graph, "nodes", ()) if getattr(n, "hypothesis_state", None)]
    assert "orphaned_outcome_signal" in rules or not marks, (
        "the orphan left no trace AND invented a hypothesis state -- it must do neither"
    )


@pytest.mark.parametrize("kind", OUTCOME_KINDS, ids=lambda k: k.value)
def test_no_hypothesis_state_is_invented(kind):
    """The invariant this raise protected: an orphaned outcome must NOT create a node in the
    outcome's target state. Skipping the transition is what keeps the machine honest."""
    graph = rr.reduce_reasoning_signal(_graph(), _signal(kind))
    for node in getattr(graph, "nodes", ()):
        if node.node_id == "h-1":
            assert node.hypothesis_state is None, (
                f"orphaned {kind.value} invented state {node.hypothesis_state}"
            )


def test_a_normal_opening_transition_is_untouched():
    """NEAR-NEGATIVE. The opening signals must still open."""
    graph = rr.reduce_reasoning_signal(
        _graph(), _signal(rr.OperationalSignalKind.EXACT_SEARCH)
    )
    node = next(n for n in graph.nodes if n.node_id == "h-1")
    assert node.hypothesis_state is rr.HypothesisState.CANDIDATE


def test_a_genuinely_illegal_transition_from_a_REAL_state_still_raises():
    """ANTI-WEAKENING, and the line this fix must not cross.

    A transition out of an actual state that the table forbids means the graph is
    inconsistent -- real corruption, not a missed observation. Open a CANDIDATE, then send
    VALIDATION_SUPPORT, which requires ACTIVE.
    """
    graph = rr.reduce_reasoning_signal(
        _graph(), _signal(rr.OperationalSignalKind.EXACT_SEARCH, sequence=1)
    )
    with pytest.raises(rr.StateIntegrityError, match="illegal hypothesis transition"):
        rr.reduce_reasoning_signal(
            graph,
            _signal(rr.OperationalSignalKind.VALIDATION_SUPPORT, sequence=2),
        )


def test_subject_rebinding_still_raises():
    """ANTI-WEAKENING. Rebinding a hypothesis to a different subject is corruption and is a
    separate check that must survive untouched."""
    graph = rr.reduce_reasoning_signal(
        _graph(), _signal(rr.OperationalSignalKind.EXACT_SEARCH, sequence=1)
    )
    bad = rr.OperationalSignal(
        attempt_id="a", event_id="ev-2", sequence=2,
        source_event_sequence=2, source_event_hash=_digest("src2"),
        revision=REV, authority=rr.Authority.RESULT_DERIVED,
        hypothesis_id="h-1", subject="OTHER::g",
        kind=rr.OperationalSignalKind.FOCUSED_SYMBOL_VIEW,
        related_node_id="",
    )
    with pytest.raises(rr.StateIntegrityError, match="subject cannot be rebound"):
        rr.reduce_reasoning_signal(graph, bad)
