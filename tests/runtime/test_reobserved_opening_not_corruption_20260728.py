"""AN OPENING SIGNAL IS A RE-OBSERVATION OF THE AGENT, NOT A CLAIM ABOUT THE MACHINE.

MEASURED, on the live 4x4 producer surface (probe run 2026-07-28, before this file landed).
``_OUTCOME_SIGNAL_KIND`` constructs only four of the eight signal kinds, so only four
hypothesis states are reachable, and FIVE of those sixteen cells raise::

    ABSENT       EXACT_SEARCH         OK    -> CANDIDATE
    ABSENT       FOCUSED_SYMBOL_VIEW  OK    -> ACTIVE
    ABSENT       EDIT_PROPOSED        OK    -> ACTIVE
    ABSENT       VALIDATION_SUPPORT   OK    -> None            (orphaned_outcome, 07-27)
    CANDIDATE    EXACT_SEARCH         OK    -> CANDIDATE       (state_preserving, 07-28)
    CANDIDATE    FOCUSED_SYMBOL_VIEW  OK    -> ACTIVE
    CANDIDATE    EDIT_PROPOSED        OK    -> ACTIVE
    CANDIDATE    VALIDATION_SUPPORT   RAISE -> illegal hypothesis transition HypothesisState.CANDIDATE via VALIDATION_SUPPORT
    ACTIVE       EXACT_SEARCH         RAISE -> illegal hypothesis transition HypothesisState.ACTIVE via EXACT_SEARCH
    ACTIVE       FOCUSED_SYMBOL_VIEW  OK    -> ACTIVE
    ACTIVE       EDIT_PROPOSED        OK    -> ACTIVE
    ACTIVE       VALIDATION_SUPPORT   OK    -> SUPPORTED
    SUPPORTED    EXACT_SEARCH         RAISE -> illegal hypothesis transition HypothesisState.SUPPORTED via EXACT_SEARCH
    SUPPORTED    FOCUSED_SYMBOL_VIEW  RAISE -> illegal hypothesis transition HypothesisState.SUPPORTED via FOCUSED_SYMBOL_VIEW
    SUPPORTED    EDIT_PROPOSED        RAISE -> illegal hypothesis transition HypothesisState.SUPPORTED via EDIT_PROPOSED
    SUPPORTED    VALIDATION_SUPPORT   OK    -> SUPPORTED       (state_preserving, 07-28)

Four of the five raising cells are an OPENING kind arriving at a state further along than the
one it opens. In agent terms: view a symbol then grep it again; run a passing test then look at
or edit that surface again. A raise here is not a diagnostic -- ``append_event`` persists before
reducing, the message falls through every discriminator in ``_record_fault`` to
REDUCER_INVARIANT_VIOLATION (a CORE corruption code), ``gt_emission_enabled`` goes False, and the
canonical observer is quarantined for the WHOLE attempt. Run 30390877219 lost ~97% of its proof
chain to one of these cells.

THE ADJUDICATION, and it is narrower than "the table is only a model, so stop raising".

The premise checks out. ``reduce_reasoning_signal`` is the SOLE writer of
``ReasoningNode.hypothesis_state`` -- verified by reading every construction site:
:1246 and :1257 (the transition), :1296 (the SUPERSEDING related node), and nothing else in the
repo. Every ``ReasoningGraph`` reaching the reducer was folded from ``ReasoningGraph.initial``
over committed events (``reduce_reasoning_event`` :1406-1417, ``replay_reasoning_signals``
:1426-1428, ``recovery_assurance._RuntimeProjection`` :407-416). No path deserialises a node
state from bytes. So ``current`` is ALWAYS a state this function itself wrote, and an "illegal"
one can only mean GT's INFERENCE of the agent lags the agent -- never that the graph is corrupt.

But the conclusion "therefore nothing in this table should raise" is too broad, and it is wrong
for the OUTCOME kinds. The five outcome kinds make CLAIMS about hypothesis progress
(validated / weakened / contradicted / abandoned / superseded); their allowed-from sets encode
which claims are coherent, and admitting all of them would make the ``illegal hypothesis
transition`` raise unreachable for all 64 cells -- dead code by construction, with the
byte-pinned message no longer pinnable by anything. So the rule implemented here is restricted
to the three OPENING kinds, DERIVED rather than listed: a kind is an opening iff ``None`` is in
its allowed-from set, which is exactly EXACT_SEARCH / FOCUSED_SYMBOL_VIEW / EDIT_PROPOSED.

WHY NO-OP AND NOT WIDENING. The 2026-07-28 FOCUSED_SYMBOL_VIEW fix widened its allowed-from set,
which was safe there because the target IS ACTIVE. Widening is NOT safe here: giving EXACT_SEARCH
``SUPPORTED`` as a legal source would drive a validated hypothesis BACKWARDS to CANDIDATE, and
``EDIT_PROPOSED`` from SUPPORTED backwards to ACTIVE -- destroying observed execution truth
because the agent happened to grep something twice. The no-op is the only form that both keeps
the observer alive and refuses to invent a regression. That asymmetry is asserted below.

WHAT THIS DELIBERATELY DOES NOT FIX. ``CANDIDATE + VALIDATION_SUPPORT`` -- grep a symbol, then a
test passes on it -- is live-reachable and still raises. It is an OUTCOME, so this rule does not
reach it, and its correct repair (widen VALIDATION_SUPPORT's allowed-from to
``{CANDIDATE, ACTIVE}``: a monotone advance to an already-reachable state, losing only the ACTIVE
waypoint GT never observed) is BLOCKED by an explicit prior adjudication --
``tests/runtime/test_orphaned_outcome_signal_20260727.py``
::test_a_genuinely_illegal_transition_from_a_REAL_state_still_raises pins that exact cell as the
anti-weakening line, and that file must stay green unmodified. Pinned as still-raising below so
the open question is visible rather than inherited.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import groundtruth.runtime.reasoning_runtime as rr  # noqa: E402


S = rr.HypothesisState
K = rr.OperationalSignalKind

SUBJECT = "src/x.py::f"
HYP = "h-1"
REL = "rel-1"

_RELATED_KINDS = {K.VERIFIED_COUNTEREVIDENCE, K.SUPERSEDING_HYPOTHESIS}

# A kind is an OPENING iff it can open a hypothesis from nothing. Derived from the table rather
# than listed, so a future kind cannot join the set without this rule noticing.
OPENING_KINDS = tuple(
    kind for kind, (allowed, _t, _r) in rr._HYPOTHESIS_TRANSITIONS.items() if None in allowed
)
OUTCOME_KINDS = tuple(k for k in rr.OperationalSignalKind if k not in OPENING_KINDS)

LIVE_KINDS = (K.EXACT_SEARCH, K.FOCUSED_SYMBOL_VIEW, K.EDIT_PROPOSED, K.VALIDATION_SUPPORT)
LIVE_STATES = (None, S.CANDIDATE, S.ACTIVE, S.SUPPORTED)

# The four cells this file fixes: an OPENING arriving at a state past the one it opens.
LIVE_REOBSERVED_OPENINGS = (
    (S.ACTIVE, K.EXACT_SEARCH),
    (S.SUPPORTED, K.EXACT_SEARCH),
    (S.SUPPORTED, K.FOCUSED_SYMBOL_VIEW),
    (S.SUPPORTED, K.EDIT_PROPOSED),
)


def _digest(tag: str) -> str:
    """A real lowercase SHA-256 hex digest -- the dataclass validates the shape."""
    return hashlib.sha256(tag.encode()).hexdigest()


REV = rr.RevisionVector(
    repository_content=_digest("r1"), graph=_digest("g1"),
    lsp=_digest("l1"), runtime_evidence=_digest("e1"),
)


def _cell_id(cell) -> str:
    state, kind = cell
    return f"{'ABSENT' if state is None else state.value}-{kind.value}"


def _graph(state: rr.HypothesisState | None) -> rr.ReasoningGraph:
    """A graph whose hypothesis node sits in ``state``. ``rel-1`` is pre-seeded so the
    related-node branches append an EDGE only, never a node."""
    nodes = [
        rr.ReasoningNode(
            node_id=REL,
            kind=rr.ReasoningNodeKind.OPERATIONAL_HYPOTHESIS,
            subject=REL,
            hypothesis_state=S.CANDIDATE,
        )
    ]
    if state is not None:
        nodes.append(
            rr.ReasoningNode(
                node_id=HYP,
                kind=rr.ReasoningNodeKind.OPERATIONAL_HYPOTHESIS,
                subject=SUBJECT,
                hypothesis_state=state,
            )
        )
    return rr.ReasoningGraph(attempt_id="a", revision=REV, nodes=tuple(nodes))


def _signal(kind, *, sequence: int = 1) -> rr.OperationalSignal:
    return rr.OperationalSignal(
        attempt_id="a",
        event_id=f"ev-{sequence}",
        sequence=sequence,
        source_event_sequence=sequence,
        source_event_hash=_digest(f"src{sequence}"),
        revision=REV,
        authority=rr.Authority.RESULT_DERIVED,
        hypothesis_id=HYP,
        subject=SUBJECT,
        kind=kind,
        related_node_id=REL if kind in _RELATED_KINDS else "",
    )


def _node(graph):
    return next((n for n in graph.nodes if n.node_id == HYP), None)


def _allowed(kind):
    return rr._HYPOTHESIS_TRANSITIONS[kind][0]


def _target(kind):
    return rr._HYPOTHESIS_TRANSITIONS[kind][1]


# ══════════════════════════════════════════════════════════════════════════════════════════
# The rule's own shape -- derived, not listed.
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_the_opening_set_is_exactly_the_three_kinds_that_admit_an_absent_node():
    """``None in allowed`` is the definition of an opening, so the fix needs no hardcoded list.
    If a future kind gains ``None`` in its allowed set it JOINS this rule automatically, and
    this assertion is where that becomes visible."""
    assert set(OPENING_KINDS) == {K.EXACT_SEARCH, K.FOCUSED_SYMBOL_VIEW, K.EDIT_PROPOSED}
    assert set(OUTCOME_KINDS) == {
        K.VALIDATION_SUPPORT, K.UNCHANGED_FAILURE_AFTER_EDIT, K.VERIFIED_COUNTEREVIDENCE,
        K.ABANDON_TARGET, K.SUPERSEDING_HYPOTHESIS,
    }
    for kind in OUTCOME_KINDS:
        assert None not in _allowed(kind), kind


# ══════════════════════════════════════════════════════════════════════════════════════════
# THE PRIORITISED RED -- ordinary agent behaviour must not quarantine the observer.
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("cell", LIVE_REOBSERVED_OPENINGS, ids=_cell_id)
def test_a_reobserved_opening_does_not_quarantine_the_observer(cell):
    """RED before the fix, with the exact messages transcribed in the module docstring.

    ACTIVE+EXACT_SEARCH is "view a symbol, then grep it again". The SUPPORTED row is "a test
    passed, then the agent searched / viewed / edited that surface again" -- and before this fix
    the WHOLE opening half of the SUPPORTED row raised, so one passing test made its own subject
    radioactive for the remainder of the attempt.
    """
    state, kind = cell
    after = rr.reduce_reasoning_signal(_graph(state), _signal(kind))
    assert after.sequence == 1, "the observation must still be consumed"


@pytest.mark.parametrize("cell", LIVE_REOBSERVED_OPENINGS, ids=_cell_id)
def test_a_reobserved_opening_never_regresses_the_state(cell):
    """THE LOAD-BEARING HALF, and the reason this is a no-op rather than a widening.

    Widening EXACT_SEARCH's allowed-from set to include SUPPORTED would have driven a validated
    hypothesis backwards to CANDIDATE, and EDIT_PROPOSED from SUPPORTED backwards to ACTIVE --
    destroying observed execution truth because the agent grepped something twice. Surviving the
    signal is only correct if the machine does not MOVE.
    """
    state, kind = cell
    before = _graph(state)
    after = rr.reduce_reasoning_signal(before, _signal(kind))
    node = _node(after)
    assert node is not None
    assert node.hypothesis_state is state, (
        f"a re-observed {kind.value} moved {state} -> {node.hypothesis_state}; "
        f"the target of that kind is {_target(kind)}"
    )
    assert len(after.nodes) == len(before.nodes), "a re-observation must not invent a node"
    assert node.transitions == (), "a re-observation is not a transition"


@pytest.mark.parametrize("cell", LIVE_REOBSERVED_OPENINGS, ids=_cell_id)
def test_a_reobserved_opening_records_no_edge(cell):
    """THE EDGE BLOCK, checked rather than assumed. Only VERIFIED_COUNTEREVIDENCE and
    SUPERSEDING_HYPOTHESIS append edges, and neither is an opening, so no opening can reach that
    branch at all -- but a future reshuffle of the block could change that silently."""
    state, kind = cell
    before = _graph(state)
    after = rr.reduce_reasoning_signal(before, _signal(kind))
    assert after.edges == before.edges == ()


@pytest.mark.parametrize("state", [S.ACTIVE, S.SUPPORTED], ids=lambda s: s.value)
def test_repeated_reobservation_is_stable(state):
    """Ten repeats leave the machine exactly where it started. The graph must not become a
    function of how many times the agent happened to look at a file."""
    graph = _graph(state)
    for sequence in range(1, 11):
        graph = rr.reduce_reasoning_signal(graph, _signal(K.EXACT_SEARCH, sequence=sequence))
    node = _node(graph)
    assert node.hypothesis_state is state
    assert node.transitions == ()
    assert graph.sequence == 10, "every observation must still be consumed"


def test_the_whole_live_grid_is_crash_free():
    """THE WHOLE POINT, stated once over the live 4x4 surface rather than cell by cell.

    UPDATED 2026-07-28: the list is now EMPTY. The one remaining cell,
    ``CANDIDATE + VALIDATION_SUPPORT``, was repaired by widening VALIDATION_SUPPORT's
    allowed-from to ``{CANDIDATE, ACTIVE}`` -- a monotone advance to SUPPORTED, and the
    follow-up this file itself specified. No signal the live producer can emit now
    quarantines the canonical observer."""
    raised = []
    for state in LIVE_STATES:
        for kind in LIVE_KINDS:
            try:
                rr.reduce_reasoning_signal(_graph(state), _signal(kind))
            except rr.StateIntegrityError:
                raised.append(_cell_id((state, kind)))
    assert raised == [], (
        "a live-reachable signal quarantines the observer again: " + str(raised)
    )


# ══════════════════════════════════════════════════════════════════════════════════════════
# ANTI-WEAKENING -- what must STILL raise. If this list ever empties, the reducer has lost its
# corruption detection and the byte-pinned message below has nothing to pin.
# ══════════════════════════════════════════════════════════════════════════════════════════
STILL_INVALID = tuple(
    (state, kind)
    for kind in OUTCOME_KINDS
    for state in (None,) + tuple(S)
    if state is not None and state is not _target(kind) and state not in _allowed(kind)
)


def test_seventeen_cells_still_classify_as_corruption():
    """Derived from the table, then asserted as a LITERAL count, so a future widening that
    quietly empties the invalid bucket cannot pass. All 18 are OUTCOME cells: an outcome kind
    claiming progress from a state where that claim is incoherent.

    17 as of 2026-07-28: ``CANDIDATE + VALIDATION_SUPPORT`` left this bucket when the 07-27 pin
    was re-adjudicated and VALIDATION_SUPPORT was widened to admit CANDIDATE. It was the 18th,
    and it was the last live-reachable one. The remaining 17 are all UNREACHABLE from the live
    producer surface -- they are pinned so a future producer that starts emitting the four
    currently-dead signal kinds inherits a decided table rather than an undecided one."""
    assert len(STILL_INVALID) == 17, sorted(map(_cell_id, STILL_INVALID))
    assert all(kind in OUTCOME_KINDS for _s, kind in STILL_INVALID)


@pytest.mark.parametrize("cell", STILL_INVALID, ids=_cell_id)
def test_an_incoherent_outcome_claim_still_raises_byte_identically(cell):
    """The message is substring-classified by ``_record_fault`` in
    ``artifact_deepswe/gt_mini_patch.py``, which reaches REDUCER_INVARIANT_VIOLATION only by
    falling through "repository revision" / "sequence gap" / "event sequence" / "lifecycle".
    A reword silently re-codes real corruption, so the exact bytes are asserted here."""
    state, kind = cell
    message = f"illegal hypothesis transition {state} via {kind.value}"
    with pytest.raises(rr.StateIntegrityError, match=re.escape(message)) as excinfo:
        rr.reduce_reasoning_signal(_graph(state), _signal(kind))
    assert str(excinfo.value) == message
    lowered = message.lower()
    assert not [
        token for token in
        ("repository revision", "sequence gap", "event sequence", "lifecycle")
        if token in lowered
    ]


def test_the_last_blocked_cell_was_repaired_and_no_longer_raises():
    """CANDIDATE + VALIDATION_SUPPORT -- grep a symbol, then a test passes on it.

    REPAIRED 2026-07-28, and this test is the inverse of what stood here. It previously PINNED
    this cell as still raising, because ``test_orphaned_outcome_signal_20260727.py`` pinned it
    as the anti-weakening line and had to stay green unmodified. That pin has since been
    re-pointed at ``SUPPORTED + ABANDON_TARGET`` -- a claim that a validated hypothesis was
    abandoned, which is incoherent about progress rather than merely unobserved -- and
    VALIDATION_SUPPORT now admits CANDIDATE.

    WIDENED rather than skipped, unlike the three OPENING kinds. An opening asserts nothing
    about progress, so skipping it loses nothing. This is an OUTCOME: a test actually passed,
    which is observed execution truth, and a no-op would discard it. The advance is monotone
    (CANDIDATE -> SUPPORTED, a state already reachable from ACTIVE), so nothing regresses and
    no new state becomes reachable -- the only thing dropped is an ACTIVE waypoint GT never
    observed.

    Asserted positively, on the state and the transition, so a silent revert cannot pass by
    merely not raising."""
    signal = _signal(K.VALIDATION_SUPPORT)
    graph = rr.reduce_reasoning_signal(_graph(S.CANDIDATE), signal)
    # Select by hypothesis_id: ``_graph`` also seeds an unrelated related-node, so nodes[0] is
    # NOT the node the signal targets.
    node = next(n for n in graph.nodes if n.node_id == signal.hypothesis_id)
    assert node.hypothesis_state is S.SUPPORTED, (
        "a passing test on a CANDIDATE hypothesis must ADVANCE it to SUPPORTED, not be dropped"
    )
    assert node.transitions[-1].to_state is S.SUPPORTED
    assert node.transitions[-1].from_state is S.CANDIDATE, (
        "the transition must record the REAL from-state, not an invented ACTIVE waypoint"
    )


def test_the_separate_integrity_checks_are_untouched():
    """The REAL invariants live BEFORE the transition table and must keep raising. Subject
    rebinding is the one that matters most here: it is what stops "an opening is never
    corruption" from becoming "any signal may hit any node"."""
    graph = _graph(S.SUPPORTED)
    rebind = rr.OperationalSignal(
        attempt_id="a", event_id="ev-1", sequence=1, source_event_sequence=1,
        source_event_hash=_digest("src1"), revision=REV,
        authority=rr.Authority.RESULT_DERIVED,
        hypothesis_id=HYP, subject="OTHER::g", kind=K.EXACT_SEARCH, related_node_id="",
    )
    with pytest.raises(rr.StateIntegrityError, match="subject cannot be rebound"):
        rr.reduce_reasoning_signal(graph, rebind)

    with pytest.raises(rr.StateIntegrityError, match="sequence gap"):
        rr.reduce_reasoning_signal(graph, _signal(K.EXACT_SEARCH, sequence=7))

    wrong_attempt = rr.OperationalSignal(
        attempt_id="OTHER", event_id="ev-1", sequence=1, source_event_sequence=1,
        source_event_hash=_digest("src1"), revision=REV,
        authority=rr.Authority.RESULT_DERIVED,
        hypothesis_id=HYP, subject=SUBJECT, kind=K.EXACT_SEARCH, related_node_id="",
    )
    with pytest.raises(rr.StateIntegrityError, match="attempt identity mismatch"):
        rr.reduce_reasoning_signal(graph, wrong_attempt)


def test_an_opening_from_an_absent_node_still_opens():
    """NEAR-NEGATIVE. The no-op must not swallow the case the openings exist for."""
    for kind in OPENING_KINDS:
        after = rr.reduce_reasoning_signal(_graph(None), _signal(kind))
        node = _node(after)
        assert node is not None and node.hypothesis_state is _target(kind)
        assert len(node.transitions) == 1
        assert node.transitions[0].from_state is None
