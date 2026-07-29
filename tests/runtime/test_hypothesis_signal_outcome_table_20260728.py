"""The hypothesis transition table treats ORDINARY REPETITION as CORRUPTION, and the shipped
one-line widening fixed exactly ONE of the eight live-reachable cells where it does.

WHAT WAS FIXED. Run 30390877219 killed the canonical observer on 4/5 tasks with::

    observe_failed:StateIntegrityError:illegal hypothesis transition HypothesisState.ACTIVE via FOCUSED_SYMBOL_VIEW

The agent viewed a symbol, GT opened an ACTIVE hypothesis, the agent viewed the SAME symbol
again. The fix added ACTIVE to FOCUSED_SYMBOL_VIEW's allowed-from set (reasoning_runtime.py
:1102-1106). That is correct for the invariant it defends -- the target state IS ACTIVE, so
admitting ACTIVE as a source adds no newly reachable state.

WHAT WAS NOT FIXED, and why this file is bigger than that one cell.

``_OUTCOME_SIGNAL_KIND`` (:1295-1300) is the ONLY producer of OperationalSignals, and it emits
just FOUR of the eight kinds -- EXACT_SEARCH, FOCUSED_SYMBOL_VIEW, EDIT_PROPOSED,
VALIDATION_SUPPORT. Nothing constructs UNCHANGED_FAILURE_AFTER_EDIT, VERIFIED_COUNTEREVIDENCE,
ABANDON_TARGET or SUPERSEDING_HYPOTHESIS from an event, so WEAKENED, CONTRADICTED, ABANDONED
and SUPERSEDED are unreachable states. The LIVE surface is a 4x4 subgrid of 16 cells. When this
file was written EIGHT of those sixteen raised; as of 2026-07-28 **ZERO** do::

                    EXACT_SEARCH   FOCUSED_VIEW   EDIT_PROPOSED   VALIDATION_SUPPORT
    (absent)        open           open           open            skip
    CANDIDATE       preserve       open           open            open   (widened)
    ACTIVE          reobserved     preserve       preserve        open
    SUPPORTED       reobserved     reobserved     reobserved      preserve

The eight that used to raise, as trajectories rather than states:

  * ``CANDIDATE + EXACT_SEARCH`` -- grep the same symbol twice.
  * ``ACTIVE + EXACT_SEARCH`` -- view a symbol, then grep it again.
  * ``SUPPORTED + VALIDATION_SUPPORT`` -- run a passing test twice.
  * ``SUPPORTED + FOCUSED_SYMBOL_VIEW`` / ``+ EDIT_PROPOSED`` -- a test passed, then the agent
    looked at or edited that surface again. The whole SUPPORTED row raised: once a hypothesis
    was validated, NO signal the producer could emit avoided quarantining the observer.
  * ``CANDIDATE + VALIDATION_SUPPORT`` -- grep a symbol, then a test passes on it. The last one
    repaired, and the only one repaired by WIDENING rather than by the reobserved rule.

THE CATEGORY ERROR BEHIND ALL OF THEM. The reducer recognises TWO outcomes: transition, or
corruption. It already carries a third as an inline boolean (``orphaned_outcome``, :1198, from
run 30276041709) and has no concept of a fourth: the signal is legitimate, the machine is
already in the state the signal targets, and the correct response is to consume the observation
without moving. Resolved in this order -- ``current == target`` FIRST, because it is the only
rule that covers both FOCUSED_SYMBOL_VIEW (ACTIVE not in ``allowed`` before the fix) and
EDIT_PROPOSED (ACTIVE in ``allowed`` all along) with one law::

    VALID_TRANSITION          state moves along a declared edge
    STATE_PRESERVING_UPDATE   current == target(kind); observed again, nothing moves
    INTENTIONAL_NO_OP         current is None and the kind requires an existing hypothesis
    REOBSERVED_OPENING        an OPENING kind (``None in allowed``) at a state past the one it
                              opens; consume it, do NOT drag the state backwards to the target
    INVALID_EVENT             an OUTCOME kind claiming progress that is incoherent from a REAL
                              state -- corruption, and still 17 of the 64 cells

REOBSERVED_OPENING landed 2026-07-28 (``tests/runtime/test_reobserved_opening_not_corruption_
20260728.py``) and emptied every OPENING cell above. It deliberately does NOT reach
``CANDIDATE + VALIDATION_SUPPORT``, which is an OUTCOME: skipping it would discard real
execution truth (a test passed). That cell was repaired separately, by widening
VALIDATION_SUPPORT's allowed-from to ``{CANDIDATE, ACTIVE}`` -- a MONOTONE advance to SUPPORTED
that adds no newly reachable state and drops only an ACTIVE waypoint GT never observed.

That widening required releasing the anti-weakening pin in
``test_orphaned_outcome_signal_20260727.py``, which had pinned that exact cell. The pin was
RE-POINTED, not deleted: it now guards ``SUPPORTED + ABANDON_TARGET``, a claim that a validated
hypothesis was abandoned, which that file's own docstring already named as genuine corruption.
The anti-weakening property is unchanged; only the cell carrying it moved, because the old one
stopped being an example of the thing it pinned.

THE FABRICATED SELF-TRANSITION -- adjudicated, and deliberately NOT asserted as a defect.
On the two cells that already tolerate their own target state, the reducer appends
``HypothesisTransition(from_state=ACTIVE, to_state=ACTIVE)`` on every repeat. Measured: ten
views mint nine such entries. ``transitions`` IS inside ``canonical_json`` and therefore inside
``graph_hash`` (:1045-1050), which flows to ``_RuntimeProjection.reasoning_graph_hash`` ->
``RuntimeIntegrityAttestation`` -> ``aggregate_hash`` (recovery_assurance.py:533, 577). Ten
views produce ten distinct graph hashes. BUT: the two-replay determinism check replays the SAME
committed log twice, so both replays mint the same entries and the check is not weakened; and a
whole-repo search finds NO production reader of ``.transitions`` at all -- only the reducer's
own append (:1226) and tests. So it is a ledger of non-transitions that grows O(views) and
perturbs a hash nobody compares across trajectories. Real, but not a correctness break, and not
grounds for a refactor. It is pinned below as CHARACTERIZATION, so a future change in either
direction is visible.

WHY EXHAUSTIVELY. 8 states (7 + node-absent) x 8 kinds = 64 cells. Two successive fixes to this
table each repaired the one cell a run happened to hit. The 64-cell map is written out by hand
HERE so exhaustiveness lives in the test rather than in the source it grades -- a table that
generated its own expectations could never fail. No new source API is required to run this
file; the classification is a pure test-local function.

DO NOT WEAKEN. INVALID_EVENT must keep raising with the message BYTE-IDENTICAL: the fault
classifier in ``artifact_deepswe/gt_mini_patch.py`` (``_record_fault``) routes
StateIntegrityError by substring and reaches REDUCER_INVARIANT_VIOLATION only by falling
through all three discriminators.
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

VALID = "VALID_TRANSITION"
PRESERVING = "STATE_PRESERVING_UPDATE"
NO_OP = "INTENTIONAL_NO_OP"
REOBSERVED = "REOBSERVED_OPENING"
INVALID = "INVALID_EVENT"

ALL_STATES: tuple[rr.HypothesisState | None, ...] = (None,) + tuple(S)
ALL_KINDS: tuple[rr.OperationalSignalKind, ...] = tuple(K)


# ══════════════════════════════════════════════════════════════════════════════════════════
# LAYER 1 -- the literal 64-cell map.
#
# Hand-derived by READING ``_HYPOTHESIS_TRANSITIONS`` (reasoning_runtime.py:1077-1136). The
# starred rows in the module docstring are the live-reachable subset; the rest are pinned so a
# future producer that starts emitting the four currently-dead kinds inherits a decided table
# instead of an undecided one.
# ══════════════════════════════════════════════════════════════════════════════════════════
EXPECTED: dict[tuple[rr.HypothesisState | None, rr.OperationalSignalKind], str] = {
    # EXACT_SEARCH            allowed={None}                             -> CANDIDATE
    # (CANDIDATE, ...) = grep the same symbol twice. RAISES today.
    (None,             K.EXACT_SEARCH): VALID,
    (S.CANDIDATE,      K.EXACT_SEARCH): PRESERVING,
    (S.ACTIVE,         K.EXACT_SEARCH): REOBSERVED,
    (S.SUPPORTED,      K.EXACT_SEARCH): REOBSERVED,
    (S.WEAKENED,       K.EXACT_SEARCH): REOBSERVED,
    (S.CONTRADICTED,   K.EXACT_SEARCH): REOBSERVED,
    (S.ABANDONED,      K.EXACT_SEARCH): REOBSERVED,
    (S.SUPERSEDED,     K.EXACT_SEARCH): REOBSERVED,

    # FOCUSED_SYMBOL_VIEW     allowed={None, CANDIDATE, ACTIVE}          -> ACTIVE
    # (ACTIVE, ...) was the run-30390877219 killer; widened 2026-07-28, tolerated now.
    (None,             K.FOCUSED_SYMBOL_VIEW): VALID,
    (S.CANDIDATE,      K.FOCUSED_SYMBOL_VIEW): VALID,
    (S.ACTIVE,         K.FOCUSED_SYMBOL_VIEW): PRESERVING,
    (S.SUPPORTED,      K.FOCUSED_SYMBOL_VIEW): REOBSERVED,
    (S.WEAKENED,       K.FOCUSED_SYMBOL_VIEW): REOBSERVED,
    (S.CONTRADICTED,   K.FOCUSED_SYMBOL_VIEW): REOBSERVED,
    (S.ABANDONED,      K.FOCUSED_SYMBOL_VIEW): REOBSERVED,
    (S.SUPERSEDED,     K.FOCUSED_SYMBOL_VIEW): REOBSERVED,

    # EDIT_PROPOSED           allowed={None, CANDIDATE, ACTIVE}          -> ACTIVE
    # (ACTIVE, ...) is PRESERVING even though ACTIVE IS in ``allowed``: current==target
    # outranks membership. This is the cell that has been minting self-transitions all along.
    (None,             K.EDIT_PROPOSED): VALID,
    (S.CANDIDATE,      K.EDIT_PROPOSED): VALID,
    (S.ACTIVE,         K.EDIT_PROPOSED): PRESERVING,
    (S.SUPPORTED,      K.EDIT_PROPOSED): REOBSERVED,
    (S.WEAKENED,       K.EDIT_PROPOSED): REOBSERVED,
    (S.CONTRADICTED,   K.EDIT_PROPOSED): REOBSERVED,
    (S.ABANDONED,      K.EDIT_PROPOSED): REOBSERVED,
    (S.SUPERSEDED,     K.EDIT_PROPOSED): REOBSERVED,

    # VALIDATION_SUPPORT      allowed={ACTIVE}                           -> SUPPORTED
    # (SUPPORTED, ...) = run a passing test twice. RAISES today.
    (None,             K.VALIDATION_SUPPORT): NO_OP,
    # WIDENED 2026-07-28 to {CANDIDATE, ACTIVE}. Live trajectory: grep a symbol, then a test
    # passes on it. It was the LAST live-reachable cell that quarantined the observer. Widened
    # rather than skipped because an OUTCOME carries execution truth a no-op would discard; the
    # advance CANDIDATE -> SUPPORTED is monotone and adds no newly reachable state.
    (S.CANDIDATE,      K.VALIDATION_SUPPORT): VALID,
    (S.ACTIVE,         K.VALIDATION_SUPPORT): VALID,
    (S.SUPPORTED,      K.VALIDATION_SUPPORT): PRESERVING,
    (S.WEAKENED,       K.VALIDATION_SUPPORT): INVALID,
    (S.CONTRADICTED,   K.VALIDATION_SUPPORT): INVALID,
    (S.ABANDONED,      K.VALIDATION_SUPPORT): INVALID,
    (S.SUPERSEDED,     K.VALIDATION_SUPPORT): INVALID,

    # UNCHANGED_FAILURE_AFTER_EDIT  allowed={ACTIVE, SUPPORTED}          -> WEAKENED
    (None,             K.UNCHANGED_FAILURE_AFTER_EDIT): NO_OP,
    (S.CANDIDATE,      K.UNCHANGED_FAILURE_AFTER_EDIT): INVALID,
    (S.ACTIVE,         K.UNCHANGED_FAILURE_AFTER_EDIT): VALID,
    (S.SUPPORTED,      K.UNCHANGED_FAILURE_AFTER_EDIT): VALID,
    (S.WEAKENED,       K.UNCHANGED_FAILURE_AFTER_EDIT): PRESERVING,
    (S.CONTRADICTED,   K.UNCHANGED_FAILURE_AFTER_EDIT): INVALID,
    (S.ABANDONED,      K.UNCHANGED_FAILURE_AFTER_EDIT): INVALID,
    (S.SUPERSEDED,     K.UNCHANGED_FAILURE_AFTER_EDIT): INVALID,

    # VERIFIED_COUNTEREVIDENCE  allowed={ACTIVE, SUPPORTED, WEAKENED}    -> CONTRADICTED
    (None,             K.VERIFIED_COUNTEREVIDENCE): NO_OP,
    (S.CANDIDATE,      K.VERIFIED_COUNTEREVIDENCE): INVALID,
    (S.ACTIVE,         K.VERIFIED_COUNTEREVIDENCE): VALID,
    (S.SUPPORTED,      K.VERIFIED_COUNTEREVIDENCE): VALID,
    (S.WEAKENED,       K.VERIFIED_COUNTEREVIDENCE): VALID,
    (S.CONTRADICTED,   K.VERIFIED_COUNTEREVIDENCE): PRESERVING,
    (S.ABANDONED,      K.VERIFIED_COUNTEREVIDENCE): INVALID,
    (S.SUPERSEDED,     K.VERIFIED_COUNTEREVIDENCE): INVALID,

    # ABANDON_TARGET          allowed={ACTIVE, WEAKENED, CONTRADICTED}   -> ABANDONED
    (None,             K.ABANDON_TARGET): NO_OP,
    (S.CANDIDATE,      K.ABANDON_TARGET): INVALID,
    (S.ACTIVE,         K.ABANDON_TARGET): VALID,
    (S.SUPPORTED,      K.ABANDON_TARGET): INVALID,
    (S.WEAKENED,       K.ABANDON_TARGET): VALID,
    (S.CONTRADICTED,   K.ABANDON_TARGET): VALID,
    (S.ABANDONED,      K.ABANDON_TARGET): PRESERVING,
    (S.SUPERSEDED,     K.ABANDON_TARGET): INVALID,

    # SUPERSEDING_HYPOTHESIS  allowed={ACTIVE, SUPPORTED, WEAKENED}      -> SUPERSEDED
    (None,             K.SUPERSEDING_HYPOTHESIS): NO_OP,
    (S.CANDIDATE,      K.SUPERSEDING_HYPOTHESIS): INVALID,
    (S.ACTIVE,         K.SUPERSEDING_HYPOTHESIS): VALID,
    (S.SUPPORTED,      K.SUPERSEDING_HYPOTHESIS): VALID,
    (S.WEAKENED,       K.SUPERSEDING_HYPOTHESIS): VALID,
    (S.CONTRADICTED,   K.SUPERSEDING_HYPOTHESIS): INVALID,
    (S.ABANDONED,      K.SUPERSEDING_HYPOTHESIS): INVALID,
    (S.SUPERSEDED,     K.SUPERSEDING_HYPOTHESIS): PRESERVING,
}

CELLS = [(state, kind) for kind in ALL_KINDS for state in ALL_STATES]

# The kinds a canonical event can actually produce, and therefore the states reachable at all.
REACHABLE_KINDS = (
    K.EXACT_SEARCH, K.FOCUSED_SYMBOL_VIEW, K.EDIT_PROPOSED, K.VALIDATION_SUPPORT,
)
REACHABLE_STATES = (None, S.CANDIDATE, S.ACTIVE, S.SUPPORTED)
LIVE_CELLS = [(s, k) for k in REACHABLE_KINDS for s in REACHABLE_STATES]

_RELATED_KINDS = {K.VERIFIED_COUNTEREVIDENCE, K.SUPERSEDING_HYPOTHESIS}


def _cell_id(cell) -> str:
    state, kind = cell
    return f"{'ABSENT' if state is None else state.value}-{kind.value}"


# ══════════════════════════════════════════════════════════════════════════════════════════
# construction idiom -- copied from tests/runtime/test_orphaned_outcome_signal_20260727.py
# ══════════════════════════════════════════════════════════════════════════════════════════
def _digest(tag: str) -> str:
    """A real lowercase SHA-256 hex digest -- the dataclass validates the shape."""
    return hashlib.sha256(tag.encode()).hexdigest()


REV = rr.RevisionVector(
    repository_content=_digest("r1"), graph=_digest("g1"),
    lsp=_digest("l1"), runtime_evidence=_digest("e1"),
)

SUBJECT = "src/x.py::f"
HYP = "h-1"
REL = "rel-1"


def _graph(state: rr.HypothesisState | None) -> rr.ReasoningGraph:
    """A graph whose hypothesis node sits in ``state`` (ABSENT when None).

    ``rel-1`` is pre-seeded so the related-node branches of VERIFIED_COUNTEREVIDENCE /
    SUPERSEDING_HYPOTHESIS do not APPEND a node -- that keeps ``len(graph.nodes)`` a clean
    signal about the hypothesis node itself rather than about edge bookkeeping.
    """
    nodes = [
        rr.ReasoningNode(
            node_id=REL,
            kind=rr.ReasoningNodeKind.OPERATIONAL_HYPOTHESIS,
            subject=REL,
            hypothesis_state=rr.HypothesisState.CANDIDATE,
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


def _signal(kind: rr.OperationalSignalKind, *, sequence: int = 1) -> rr.OperationalSignal:
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


def _node(graph: rr.ReasoningGraph):
    return next((n for n in graph.nodes if n.node_id == HYP), None)


def _allowed(kind):
    return rr._HYPOTHESIS_TRANSITIONS[kind][0]


def _target(kind):
    return rr._HYPOTHESIS_TRANSITIONS[kind][1]


def classify(current, kind) -> str:
    """The FIVE-outcome law, PURE and test-local.

    Deliberately not imported from the runtime: the point of this file is that exhaustiveness
    lives in the test. If the runtime later grows its own classifier, this function is the
    reference it must agree with -- but nothing here forces that refactor to land.

    Resolution ORDER is the whole design. ``current == target`` is checked before allowed-set
    membership because that single rule covers FOCUSED_SYMBOL_VIEW and EDIT_PROPOSED alike.
    ``REOBSERVED_OPENING`` (added 2026-07-28) is checked next: an OPENING kind -- derived as
    ``None in allowed`` -- arriving at a state past the one it opens is GT's inference lagging
    the agent, not corruption, and it must NOT move the machine backwards to its target.
    """
    allowed, target, _reason = rr._HYPOTHESIS_TRANSITIONS[kind]
    if current is target:
        return PRESERVING
    if current is None and None not in allowed:
        return NO_OP
    if current in allowed:
        return VALID
    if current is not None and None in allowed:
        return REOBSERVED
    return INVALID


# ══════════════════════════════════════════════════════════════════════════════════════════
# LAYER 1 -- every one of the 64 cells classifies as written down.
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("cell", CELLS, ids=_cell_id)
def test_every_cell_classifies_as_specified(cell):
    state, kind = cell
    assert classify(state, kind) == EXPECTED[cell]


def test_the_literal_map_covers_all_sixty_four_cells():
    """The map is the specification, so its EXHAUSTIVENESS is itself an assertion."""
    assert len(ALL_STATES) == 8, ALL_STATES
    assert len(ALL_KINDS) == 8, ALL_KINDS
    assert len(EXPECTED) == 64
    assert set(EXPECTED) == set(CELLS)


# ══════════════════════════════════════════════════════════════════════════════════════════
# LAYER 2 -- derived invariants, so the hand-written map cannot be silently wrong.
# These read ``_HYPOTHESIS_TRANSITIONS`` directly; they pass on the current tree.
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("cell", CELLS, ids=_cell_id)
def test_current_equals_target_is_always_state_preserving(cell):
    """RULE 1, and the reason it must be checked FIRST: it is the only law that covers both
    FOCUSED_SYMBOL_VIEW and EDIT_PROPOSED, which disagreed about whether ACTIVE may be a source
    while agreeing that ACTIVE is the target."""
    state, kind = cell
    if state is not _target(kind):
        pytest.skip("not a self-target cell")
    assert EXPECTED[cell] == PRESERVING


@pytest.mark.parametrize("cell", CELLS, ids=_cell_id)
def test_absent_node_for_an_outcome_kind_is_always_an_intentional_no_op(cell):
    """RULE 2 -- exactly today's ``orphaned_outcome`` boolean, promoted to a named outcome."""
    state, kind = cell
    if not (state is None and None not in _allowed(kind)):
        pytest.skip("not an orphaned-outcome cell")
    assert EXPECTED[cell] == NO_OP


@pytest.mark.parametrize("cell", CELLS, ids=_cell_id)
def test_valid_transition_iff_allowed_and_not_self_target(cell):
    """RULE 3, stated as an IFF so neither direction can drift."""
    state, kind = cell
    declared = state in _allowed(kind) and state is not _target(kind)
    assert (EXPECTED[cell] == VALID) is declared, (
        f"cell says {EXPECTED[cell]}; table says allowed={state in _allowed(kind)} "
        f"self_target={state is _target(kind)}"
    )


def test_the_five_outcomes_partition_all_sixty_four_cells():
    """Union == 64, pairwise disjoint. A cell in no bucket, or in two, means the resolution
    order is ambiguous -- which is precisely the defect: the same repeat-observation case was
    being answered two different ways depending on which kind sent it."""
    buckets = {
        name: {cell for cell, out in EXPECTED.items() if out == name}
        for name in (VALID, PRESERVING, NO_OP, REOBSERVED, INVALID)
    }
    assert sum(len(b) for b in buckets.values()) == 64
    assert set().union(*buckets.values()) == set(CELLS)
    names = list(buckets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not (buckets[a] & buckets[b]), f"{a}/{b} overlap: {buckets[a] & buckets[b]}"
    # One self-target cell per kind (8); one absent-node cell per kind whose allowed set
    # excludes None (5); the non-allowed, non-target real states of the three OPENING kinds
    # (6 + 5 + 5 = 16).
    assert len(buckets[PRESERVING]) == 8, sorted(map(_cell_id, buckets[PRESERVING]))
    assert len(buckets[NO_OP]) == 5, sorted(map(_cell_id, buckets[NO_OP]))
    # VALID went 17 -> 18 and INVALID 18 -> 17 on 2026-07-28, when VALIDATION_SUPPORT was
    # widened to admit CANDIDATE. That was the LAST live-reachable cell that quarantined the
    # observer; it moved buckets rather than being skipped, because an OUTCOME carries
    # execution truth (a test passed) that a no-op would discard.
    assert len(buckets[VALID]) == 18, sorted(map(_cell_id, buckets[VALID]))
    assert len(buckets[REOBSERVED]) == 16, sorted(map(_cell_id, buckets[REOBSERVED]))
    # 17 and not 0: restricting the reobserved rule to OPENINGS is what keeps the reducer's
    # corruption detection alive. Every survivor is an OUTCOME kind claiming progress from a
    # state where that claim is incoherent -- and all 17 are now UNREACHABLE from the live
    # producer surface, pinned for a future producer rather than describing today's risk.
    assert len(buckets[INVALID]) == 17, sorted(map(_cell_id, buckets[INVALID]))
    assert all(None not in _allowed(kind) for _s, kind in buckets[INVALID])


@pytest.mark.parametrize("cell", CELLS, ids=_cell_id)
def test_a_reobserved_opening_is_never_corruption(cell):
    """RULE 4 (2026-07-28). An OPENING kind reaching a real state it cannot advance from is GT's
    inference lagging the agent -- the same root cause as ``orphaned_outcome`` and the
    run-30390877219 crash, with the polarity flipped again."""
    state, kind = cell
    if not (state is not None and None in _allowed(kind) and state not in _allowed(kind)):
        pytest.skip("not a re-observed opening cell")
    assert EXPECTED[cell] in {PRESERVING, REOBSERVED}


def test_only_four_signal_kinds_are_reachable_from_a_canonical_event():
    """The prioritisation claim in the docstring, asserted rather than asserted-in-prose.

    ``_OUTCOME_SIGNAL_KIND`` is the sole producer. If a future mapping starts emitting one of
    the four dead kinds, the live surface grows from 16 cells to more and this test fails,
    forcing the reachability analysis above to be redone rather than silently inherited.
    """
    produced = set(rr._OUTCOME_SIGNAL_KIND.values())
    assert produced == set(REACHABLE_KINDS), sorted(k.value for k in produced)
    targets = {_target(k) for k in REACHABLE_KINDS}
    assert targets | {None} == set(REACHABLE_STATES), sorted(t.value for t in targets)


# ══════════════════════════════════════════════════════════════════════════════════════════
# LAYER 3 -- behaviour through the REAL reducer: ``reduce_reasoning_signal(graph, signal)``.
#
# Expectations come from the LITERAL map. This layer is RED today on the six self-target cells
# the widening did not reach.
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("cell", CELLS, ids=_cell_id)
def test_the_reducer_behaves_as_the_cell_specifies(cell):
    state, kind = cell
    expected = EXPECTED[cell]
    before = _graph(state)
    signal = _signal(kind)

    if expected == INVALID:
        message = f"illegal hypothesis transition {state} via {kind.value}"
        with pytest.raises(rr.StateIntegrityError, match=re.escape(message)):
            rr.reduce_reasoning_signal(before, signal)
        return

    after = rr.reduce_reasoning_signal(before, signal)
    assert after.sequence == signal.sequence, "a non-raising outcome must consume the signal"
    node = _node(after)

    if expected == NO_OP:
        assert node is None or node.hypothesis_state is None, (
            f"an orphaned {kind.value} invented state {node.hypothesis_state}"
        )
        assert len(after.nodes) == len(before.nodes), "the skip must not invent a node"
        return

    if expected == REOBSERVED:
        # The state must be PRESERVED, not driven to the opening's target -- a widening would
        # have regressed SUPPORTED back to CANDIDATE/ACTIVE on an ordinary repeat grep.
        assert node is not None
        assert node.hypothesis_state is state, (
            f"a re-observed {kind.value} regressed {state} -> {node.hypothesis_state}"
        )
        assert len(after.nodes) == len(before.nodes), "a re-observation must not add nodes"
        assert node.transitions == _node(before).transitions
        assert after.edges == before.edges, "an opening must not fabricate an edge"
        return

    if expected == PRESERVING:
        # Deliberately does NOT assert on ``len(node.transitions)``: the fabricated
        # self-transition is adjudicated separately (and tolerated) below. What must hold is
        # that an ordinary repeat observation neither raises nor moves the machine.
        assert node is not None
        assert node.hypothesis_state is state, (
            f"a repeat {kind.value} moved the state {state} -> {node.hypothesis_state}"
        )
        assert len(after.nodes) == len(before.nodes), "a repeat observation must not add nodes"
        return

    assert expected == VALID
    before_node = _node(before)
    assert node is not None
    assert node.hypothesis_state is _target(kind)
    prior = 0 if before_node is None else len(before_node.transitions)
    assert len(node.transitions) == prior + 1
    assert node.transitions[-1].from_state is state
    assert node.transitions[-1].to_state is _target(kind)


@pytest.mark.parametrize("cell", LIVE_CELLS, ids=_cell_id)
def test_no_live_reachable_trajectory_quarantines_the_observer(cell):
    """THE PRIORITISED RED. Restricted to the 16 cells a real trajectory can actually reach.

    Every one of these is an ordinary agent behaviour -- grep twice, view twice, edit twice,
    test twice, or do anything at all after a test passed. None may raise, because a raise here
    classifies REDUCER_INVARIANT_VIOLATION, quarantines the observer, and takes GT dark for the
    remainder of the attempt. That is what happened on 4/5 tasks of run 30390877219 for exactly
    ONE of these cells.
    """
    state, kind = cell
    if EXPECTED[cell] == INVALID:
        pytest.skip("a genuinely forbidden move from a real state -- must keep raising")
    rr.reduce_reasoning_signal(_graph(state), _signal(kind))


def test_the_run_30390877219_cell_no_longer_raises():
    """REGRESSION PIN on the shipped widening. The exact live cell, isolated."""
    graph = rr.reduce_reasoning_signal(
        _graph(rr.HypothesisState.ACTIVE), _signal(K.FOCUSED_SYMBOL_VIEW)
    )
    assert _node(graph).hypothesis_state is rr.HypothesisState.ACTIVE


def test_CHARACTERIZATION_the_whole_supported_row_quarantines_the_observer():
    """CURRENT BEHAVIOUR, named on its own so it cannot be lost among 64 cells. NOT asserted as
    a defect here, because the four-outcome law above genuinely classifies these cells
    INVALID -- SUPPORTED is neither in their allowed set nor their target.

    But look at what it MEANS. Once VALIDATION_SUPPORT moves a hypothesis to SUPPORTED, there
    is no signal the producer can emit for that subject that does not raise: searching it,
    viewing it, editing it, or testing it again all quarantine the observer. A passing test is
    the most ordinary thing that can happen mid-trajectory, and it makes that subject
    radioactive for the rest of the attempt.

    That points at a limit of the four-outcome model rather than at a bad cell. The three
    OPENING kinds (EXACT_SEARCH, FOCUSED_SYMBOL_VIEW, EDIT_PROPOSED) are RE-OBSERVATIONS, not
    claims about state; the same "GT is never told the agent's hypotheses, it infers them" root
    cause that produced ``orphaned_outcome`` and the run-30390877219 crash says an opening
    should never be corruption from ANY state. Deciding that is a design call, not a test's
    call, so this pins the status quo and points at it.

    UPDATED 2026-07-28, after the state-preserving rule landed in ``reduce_reasoning_signal``.
    VALIDATION_SUPPORT left this row: SUPPORTED is its own target, so re-running a passing test
    is now a state-preserving update rather than corruption. The row is 3/4, not 4/4.

    UPDATED AGAIN 2026-07-28, and the row is now EMPTY. The design question this docstring
    pointed at was adjudicated in
    ``tests/runtime/test_reobserved_opening_not_corruption_20260728.py``: an OPENING is never
    corruption from any state, because the reducer is the sole writer of ``hypothesis_state``
    and every graph reaching it was folded from ``initial``, so ``current`` is always a legally
    reached state. The rule is a SKIP, never a widening -- widening would have regressed
    SUPPORTED to CANDIDATE on a repeat grep. It is confined to the three OPENING kinds
    (``None in allowed``), so 18 OUTCOME cells still raise and the reducer keeps its corruption
    detection.
    """
    expected_still_raising: list[str] = []
    raised = []
    for kind in REACHABLE_KINDS:
        try:
            rr.reduce_reasoning_signal(_graph(rr.HypothesisState.SUPPORTED), _signal(kind))
        except rr.StateIntegrityError:
            raised.append(kind.value)
    assert raised == expected_still_raising, (
        "the SUPPORTED row's behaviour changed -- re-adjudicate the opening-vs-outcome "
        f"question in this docstring. still raising: {raised}"
    )


# ══════════════════════════════════════════════════════════════════════════════════════════
# CHARACTERIZATION -- the fabricated self-transition. Adjudicated as TOLERABLE, pinned so a
# change in either direction is visible.
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("kind", [K.FOCUSED_SYMBOL_VIEW, K.EDIT_PROPOSED], ids=lambda k: k.value)
def test_a_repeat_observation_mints_no_self_transition(kind):
    """INVARIANT as of 2026-07-28. Ten repeat observations mint exactly ONE transition.

    This was written as a CHARACTERIZATION of the opposite behaviour -- ten repeats minting
    nine ACTIVE -> ACTIVE entries and ten distinct graph hashes -- and adjudicated tolerable on
    the grounds that (a) a whole-repo search finds NO production reader of
    ``ReasoningNode.transitions`` outside the reducer's own append and tests, and (b) the
    two-replay determinism check in ``recovery_assurance._verify_with_request`` replays the SAME
    committed log twice, so both replays mint identical entries and the check was not weakened.

    It is now an invariant instead, because the state-preserving rule in
    ``reduce_reasoning_signal`` had to land anyway for a different and larger reason -- seven
    other self-target cells were still quarantining the observer -- and it removes this as a
    side effect rather than as its purpose.

    Why it is worth asserting in the strong direction: ``transitions`` IS serialised into
    ``canonical_json`` and therefore into ``graph_hash``, which reaches
    ``RuntimeIntegrityAttestation.reasoning_graph_hash`` and ``aggregate_hash``. A transition
    ledger whose entries are not transitions is a bad thing to hash, and reacquiring that
    property silently is exactly what this pins against.

    WHAT THIS DOES **NOT** CLAIM: that ``graph_hash`` is now stable across repeats. It is not,
    and an earlier draft of this test asserted that it was. ``sequence``,
    ``last_source_event_sequence`` and ``source_event_ids`` are ALSO ``ReasoningGraph`` fields,
    and the reducer requires ``signal.sequence == graph.sequence + 1``, so ten repeat
    observations produce ten distinct hashes both before and after the change. What changed is
    WHAT the hash encodes, not how many values it takes -- and asserting otherwise would be a
    green test pinning a false property.
    """
    graph = rr.ReasoningGraph.initial(attempt_id="a", revision=REV)
    hashes = []
    for sequence in range(1, 11):
        graph = rr.reduce_reasoning_signal(graph, _signal(kind, sequence=sequence))
        hashes.append(graph.graph_hash)
    node = _node(graph)
    assert node.hypothesis_state is rr.HypothesisState.ACTIVE
    assert len(node.transitions) == 1, "a re-observation is not a transition"
    assert sum(1 for t in node.transitions if t.from_state is t.to_state) == 0
    # Still ten, and that is correct: the per-observation sequence is a graph field.
    assert len(set(hashes)) == 10


def test_transitions_are_inside_the_graph_hash():
    """The load-bearing half of the characterization above, isolated so it cannot be assumed.

    If this ever goes GREEN-by-becoming-False (transitions excluded from canonical_json), the
    self-transition question collapses to pure bookkeeping and the characterization test should
    be relaxed rather than chased.
    """
    bare = rr.ReasoningNode(
        node_id="h", kind=rr.ReasoningNodeKind.OPERATIONAL_HYPOTHESIS,
        subject="s", hypothesis_state=rr.HypothesisState.ACTIVE, transitions=(),
    )
    with_self = rr.ReasoningNode(
        node_id="h", kind=rr.ReasoningNodeKind.OPERATIONAL_HYPOTHESIS,
        subject="s", hypothesis_state=rr.HypothesisState.ACTIVE,
        transitions=(rr.HypothesisTransition(
            rr.HypothesisState.ACTIVE, rr.HypothesisState.ACTIVE, "ev", "R"),),
    )
    a = rr.ReasoningGraph(attempt_id="a", revision=REV, nodes=(bare,))
    b = rr.ReasoningGraph(attempt_id="a", revision=REV, nodes=(with_self,))
    assert "transitions" in a.canonical_json()
    assert a.graph_hash != b.graph_hash


def test_repeating_a_state_preserving_signal_is_deterministic():
    """Two identical repeat views must leave the hypothesis STATE identical. Determinism here
    is about the machine's position, not its ledger length -- a repeat that moved the state
    would make the graph a function of how many times the agent happened to look."""
    once = rr.reduce_reasoning_signal(
        _graph(rr.HypothesisState.ACTIVE), _signal(K.FOCUSED_SYMBOL_VIEW, sequence=1)
    )
    twice = rr.reduce_reasoning_signal(once, _signal(K.FOCUSED_SYMBOL_VIEW, sequence=2))
    assert _node(twice).hypothesis_state is _node(once).hypothesis_state
    assert _node(twice).subject == _node(once).subject
    assert twice.sequence == 2, "the second observation must still be consumed"


# ══════════════════════════════════════════════════════════════════════════════════════════
# LAYER 4 -- fault-taxonomy pin.
# ══════════════════════════════════════════════════════════════════════════════════════════
# The ladder lives in ``_record_fault`` in artifact_deepswe/gt_mini_patch.py -- a METHOD BODY,
# not an importable helper, inside a 22k-line module (locate it by the symbol; the file is
# under concurrent edit and its line numbers move). It reads:
#
#     message = str(exc).lower()
#     ...
#     elif isinstance(exc, StateIntegrityError):
#         if   "repository revision" in message: REPOSITORY_REVISION_INCONSISTENCY
#         elif "sequence gap" in message or "event sequence" in message: CAUSAL_EVENT_GAP
#         elif "lifecycle" in message: IMPOSSIBLE_LIFECYCLE_TRANSITION
#         else: REDUCER_INVARIANT_VIOLATION
#
# REDUCER_INVARIANT_VIOLATION is reached ONLY by falling through all three. So asserting the
# message matches none of the discriminating substrings is EXACTLY equivalent to asserting the
# ladder returns REDUCER_INVARIANT_VIOLATION, with none of the import weight. "lifecycle" is
# the near-miss to watch: an illegal hypothesis transition IS a lifecycle violation in plain
# English, so a well-meaning reword to "illegal lifecycle transition" silently re-codes it.
_LADDER_DISCRIMINATORS = ("repository revision", "sequence gap", "event sequence", "lifecycle")


@pytest.mark.parametrize(
    "cell", [c for c in CELLS if EXPECTED[c] == INVALID], ids=_cell_id,
)
def test_invalid_event_messages_still_classify_as_reducer_invariant_violation(cell):
    state, kind = cell
    with pytest.raises(rr.StateIntegrityError) as excinfo:
        rr.reduce_reasoning_signal(_graph(state), _signal(kind))
    message = str(excinfo.value).lower()
    hits = [token for token in _LADDER_DISCRIMINATORS if token in message]
    assert not hits, (
        f"the fault ladder would re-code this corruption as {hits} instead of "
        f"REDUCER_INVARIANT_VIOLATION: {excinfo.value!s}"
    )


def test_the_live_message_shape_is_byte_pinned():
    """The exact string shape run 30390877219 emitted, pinned character for character. It is
    substring-classified into a fault code AND it is the only line that named the killer, so a
    reword breaks both the taxonomy and every grep-based triage.

    RE-POINTED 2026-07-28. This pinned ``SUPPORTED + FOCUSED_SYMBOL_VIEW``, which no longer
    raises -- it is a re-observed opening. The FORMAT is what is load-bearing, not the cell, so
    it now pins an OUTCOME cell that is genuinely incoherent (a hypothesis abandoned from a
    state where nothing was ever validated to abandon). Both halves of the interpolation are
    still exercised: ``repr``-style state on the left, ``.value`` on the right.
    """
    with pytest.raises(rr.StateIntegrityError) as excinfo:
        rr.reduce_reasoning_signal(
            _graph(rr.HypothesisState.SUPPORTED), _signal(K.ABANDON_TARGET)
        )
    assert str(excinfo.value) == (
        "illegal hypothesis transition HypothesisState.SUPPORTED via ABANDON_TARGET"
    )
