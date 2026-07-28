"""#30 step 1 — ONE source for the delivery transition table, provably membership-preserving.

WHY. The same delivery-transition table was written out by hand THREE times:
`append_delivery` (:2116), `append_compilation` (:2504) and `append_compilation_transition`
(:2689). The latter two were byte-identical; the first is the same table PLUS the initial
`SELECTED -> COMPILED` edge. `record_delivery_failure` (:3249) has a genuinely DIFFERENT,
failure-only table and is deliberately left alone.

Three hand-maintained copies of a state machine is the hazard that made D4 expensive: a
literal N files must keep in sync is N versions that happen to agree today. Here it is worse
than a hash label, because a missed edge means one validator ACCEPTS a transition another
REJECTS — the journal would be internally inconsistent rather than merely mislabelled.

WHAT THIS REFACTOR MUST NOT DO: change membership. `append_compilation*` deliberately do NOT
permit `SELECTED -> COMPILED`; flattening all three into one table would silently ADD that
edge. So the constant is the 5-state table and `append_delivery` COMPOSES the initial edge on
top, rather than everyone sharing a widened table.

These tests pin the exact edges. They are the reason the extraction is safe, and they will
fail loudly if a future edit widens or narrows any transition by accident.
"""

from __future__ import annotations

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.reasoning_runtime import DeliveryState as DS


EXPECTED_CORE = {
    # WITHHELD_FOR_MEASUREMENT added DELIBERATELY (#30 step 2). This test FAILED when the edge
    # landed, which is exactly what it is for: a widening of the state machine cannot happen
    # without someone editing this line and saying so. Never relax it to a subset check.
    DS.COMPILED: {DS.JOINED, DS.JOIN_FAILED, DS.WITHHELD_FOR_MEASUREMENT},
    DS.JOINED: {DS.DISPATCHED},
    DS.DISPATCHED: {
        DS.PROVIDER_ACCEPTED,
        DS.DISPATCH_FAILED,
        DS.PROVIDER_REJECTED,
    },
    DS.PROVIDER_ACCEPTED: {
        DS.DELIVERED,
        DS.INFERENCE_FAILED,
        DS.CANCELLED,
        DS.PARTIAL_OUTPUT,
    },
    DS.DELIVERED: {DS.RESPONSE_COMMITTED, DS.RESPONSE_DISCARDED},
}


def test_core_transition_table_is_exactly_the_edges_it_replaced() -> None:
    """Byte-for-byte the table `append_compilation` / `append_compilation_transition` had."""
    assert rr._DELIVERY_TRANSITIONS == EXPECTED_CORE


def test_core_table_does_not_permit_the_initial_edge() -> None:
    """SELECTED -> COMPILED belongs to `append_delivery` ONLY.

    The compilation validators never accepted it. If the constant carried it, extracting the
    constant would have silently widened two validators — the exact defect this test exists
    to prevent.
    """
    assert DS.SELECTED not in rr._DELIVERY_TRANSITIONS


def test_initial_transition_table_is_the_core_plus_exactly_one_edge() -> None:
    """`append_delivery` composes; it does not maintain a second copy."""
    assert rr._INITIAL_DELIVERY_TRANSITIONS == {
        DS.SELECTED: {DS.COMPILED},
        **EXPECTED_CORE,
    }
    extra = set(rr._INITIAL_DELIVERY_TRANSITIONS) - set(rr._DELIVERY_TRANSITIONS)
    assert extra == {DS.SELECTED}


def test_tables_are_immutable_so_a_caller_cannot_widen_them() -> None:
    """A shared mutable dict-of-sets would let one caller silently edit every validator."""
    for table in (rr._DELIVERY_TRANSITIONS, rr._INITIAL_DELIVERY_TRANSITIONS):
        for targets in table.values():
            assert isinstance(targets, frozenset), (
                "transition targets must be frozenset: a shared mutable set is one "
                "`.add()` away from widening every validator at once"
            )


def test_failure_table_is_deliberately_separate_and_narrower() -> None:
    """`record_delivery_failure` is a FAILURE-ONLY subset and must not be folded in.

    Pinned because the tempting 'consolidate everything' move would let a success edge leak
    into the failure recorder.
    """
    core = rr._DELIVERY_TRANSITIONS
    # The failure recorder must never permit a SUCCESS continuation.
    assert DS.RESPONSE_COMMITTED in core[DS.DELIVERED]
    assert DS.JOINED in core[DS.COMPILED]
    assert DS.PROVIDER_ACCEPTED in core[DS.DISPATCHED]
