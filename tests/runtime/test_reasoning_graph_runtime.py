"""RED contract for the canonical observable-reasoning graph.

These tests deliberately target the new ``reasoning_runtime`` boundary rather
than the legacy mutable HypothesisLedger.  The graph may reconstruct only
operational commitments visible in canonical events; it must not accept hidden
belief or chain-of-thought text.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from groundtruth.runtime import reasoning_runtime as rr


REV = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)


def _signal(
    sequence: int,
    kind,
    *,
    related_node_id: str = "",
) -> object:
    return rr.OperationalSignal(
        attempt_id="attempt-1",
        event_id=f"ev-{sequence}",
        sequence=sequence,
        source_event_sequence=sequence * 2,
        source_event_hash=f"{sequence:064x}",
        revision=REV,
        authority=rr.Authority.RESULT_DERIVED,
        hypothesis_id="hyp:authorization-cache",
        subject="authorization_cache",
        kind=kind,
        related_node_id=related_node_id,
    )


def test_reasoning_graph_declares_the_complete_typed_vocabulary() -> None:
    assert {
        "ISSUE_REQUIREMENT",
        "BEHAVIORAL_INVARIANT",
        "UNKNOWN",
        "QUESTION",
        "CANDIDATE_TARGET",
        "OPERATIONAL_HYPOTHESIS",
        "EVIDENCE",
        "COUNTEREVIDENCE",
        "CONFLICT",
        "CONTRACT",
        "OBLIGATION",
        "DECISION",
        "EDIT",
        "VALIDATION",
        "FAILURE",
        "CLOSED_BRANCH",
    } <= {kind.value for kind in rr.ReasoningNodeKind}
    assert {
        "SUPPORTS",
        "CONTRADICTS",
        "DEPENDS_ON",
        "REQUIRES",
        "SATISFIES",
        "VIOLATES",
        "TARGETS",
        "TESTS",
        "INVALIDATES",
        "SUPERSEDES",
        "CLOSES",
        "DERIVED_FROM",
        "VISIBLE_BEFORE",
    } <= {kind.value for kind in rr.ReasoningEdgeKind}
    assert [state.value for state in rr.HypothesisState] == [
        "CANDIDATE",
        "ACTIVE",
        "SUPPORTED",
        "WEAKENED",
        "CONTRADICTED",
        "ABANDONED",
        "SUPERSEDED",
    ]


def test_hypothesis_lifecycle_is_driven_only_by_observable_commitments() -> None:
    kinds = rr.OperationalSignalKind
    signals = (
        _signal(1, kinds.EXACT_SEARCH),
        _signal(2, kinds.FOCUSED_SYMBOL_VIEW),
        _signal(3, kinds.VALIDATION_SUPPORT),
        _signal(4, kinds.UNCHANGED_FAILURE_AFTER_EDIT),
        _signal(5, kinds.VERIFIED_COUNTEREVIDENCE, related_node_id="fact:not-on-path"),
        _signal(6, kinds.ABANDON_TARGET),
    )

    graph = rr.ReasoningGraph.initial(attempt_id="attempt-1", revision=REV)
    observed_states = []
    for signal in signals:
        graph = rr.reduce_reasoning_signal(graph, signal)
        observed_states.append(
            graph.node("hyp:authorization-cache").hypothesis_state
        )

    assert observed_states == [
        rr.HypothesisState.CANDIDATE,
        rr.HypothesisState.ACTIVE,
        rr.HypothesisState.SUPPORTED,
        rr.HypothesisState.WEAKENED,
        rr.HypothesisState.CONTRADICTED,
        rr.HypothesisState.ABANDONED,
    ]
    assert all(
        transition.reason_code
        for transition in graph.node("hyp:authorization-cache").transitions
    )
    assert any(
        edge.kind is rr.ReasoningEdgeKind.CONTRADICTS
        and edge.source_id == "fact:not-on-path"
        and edge.target_id == "hyp:authorization-cache"
        for edge in graph.edges
    )


def test_superseding_hypothesis_is_explicit_and_connected() -> None:
    kinds = rr.OperationalSignalKind
    graph = rr.ReasoningGraph.initial(attempt_id="attempt-1", revision=REV)
    graph = rr.reduce_reasoning_signal(graph, _signal(1, kinds.EXACT_SEARCH))
    graph = rr.reduce_reasoning_signal(graph, _signal(2, kinds.FOCUSED_SYMBOL_VIEW))
    graph = rr.reduce_reasoning_signal(
        graph,
        _signal(3, kinds.SUPERSEDING_HYPOTHESIS, related_node_id="hyp:runtime-path"),
    )

    assert (
        graph.node("hyp:authorization-cache").hypothesis_state
        is rr.HypothesisState.SUPERSEDED
    )
    assert any(
        edge.kind is rr.ReasoningEdgeKind.SUPERSEDES
        and edge.source_id == "hyp:runtime-path"
        and edge.target_id == "hyp:authorization-cache"
        for edge in graph.edges
    )


def test_signal_schema_cannot_accept_hidden_reasoning_claims() -> None:
    fields = set(rr.OperationalSignal.__dataclass_fields__)
    assert "belief" not in fields
    assert "chain_of_thought" not in fields
    assert "rationale" not in fields
    assert {
        "event_id",
        "sequence",
        "hypothesis_id",
        "subject",
        "kind",
        "related_node_id",
    } <= fields


def test_reasoning_graph_replay_is_byte_deterministic() -> None:
    kinds = rr.OperationalSignalKind
    signals = (
        _signal(1, kinds.EXACT_SEARCH),
        _signal(2, kinds.FOCUSED_SYMBOL_VIEW),
        _signal(3, kinds.EDIT_PROPOSED),
        _signal(4, kinds.UNCHANGED_FAILURE_AFTER_EDIT),
    )
    a = rr.replay_reasoning_signals(
        attempt_id="attempt-1",
        revision=REV,
        signals=signals,
    )
    b = rr.replay_reasoning_signals(
        attempt_id="attempt-1",
        revision=REV,
        signals=signals,
    )
    assert a == b
    assert a.canonical_json() == b.canonical_json()
    assert a.graph_hash == b.graph_hash
    assert a.canonical_json().encode("utf-8") == b.canonical_json().encode("utf-8")


def test_reasoning_graph_rejects_event_gaps_and_illegal_lifecycle_jumps() -> None:
    kinds = rr.OperationalSignalKind
    graph = rr.ReasoningGraph.initial(attempt_id="attempt-1", revision=REV)
    with pytest.raises(rr.StateIntegrityError, match="sequence"):
        rr.reduce_reasoning_signal(graph, _signal(2, kinds.EXACT_SEARCH))

    # RE-POINTED 2026-07-28, identically to the pin in
    # test_orphaned_outcome_signal_20260727.py.  This asserted that CANDIDATE +
    # VALIDATION_SUPPORT raises.  That cell is no longer corruption: it is the
    # ordinary trajectory *grep a symbol, then a test passes on it*, and it was the
    # last live-reachable cell that quarantined the canonical observer for a whole
    # attempt.  VALIDATION_SUPPORT now admits CANDIDATE -- a MONOTONE advance to
    # SUPPORTED that introduces no newly reachable state and drops only an ACTIVE
    # waypoint GT never observes.
    #
    # The lifecycle-jump assertion moves to SUPPORTED + ABANDON_TARGET, which is
    # incoherent about PROGRESS (ABANDON_TARGET admits {ACTIVE, WEAKENED,
    # CONTRADICTED}) rather than merely unobserved -- genuine corruption, and the
    # property this test exists to hold.
    graph = rr.reduce_reasoning_signal(graph, _signal(1, kinds.EXACT_SEARCH))
    graph = rr.reduce_reasoning_signal(graph, _signal(2, kinds.VALIDATION_SUPPORT))
    with pytest.raises(rr.StateIntegrityError, match="transition"):
        rr.reduce_reasoning_signal(
            graph,
            _signal(3, kinds.ABANDON_TARGET),
        )


def test_reasoning_graph_values_are_immutable() -> None:
    graph = rr.ReasoningGraph.initial(attempt_id="attempt-1", revision=REV)
    with pytest.raises(FrozenInstanceError):
        graph.sequence = 99  # type: ignore[misc]


@pytest.mark.parametrize(
    "entry_kind",
    (
        rr.OperationalSignalKind.FOCUSED_SYMBOL_VIEW,
        rr.OperationalSignalKind.EDIT_PROPOSED,
    ),
)
def test_direct_observable_commitment_can_open_an_active_hypothesis(entry_kind) -> None:
    graph = rr.reduce_reasoning_signal(
        rr.ReasoningGraph.initial(attempt_id="attempt-1", revision=REV),
        _signal(1, entry_kind),
    )
    node = graph.node("hyp:authorization-cache")
    assert node.hypothesis_state is rr.HypothesisState.ACTIVE
    assert node.transitions[-1].from_state is None
    assert node.transitions[-1].reason_code


def test_reasoning_signal_binds_attempt_event_hash_order_and_revision() -> None:
    graph = rr.ReasoningGraph.initial(attempt_id="attempt-1", revision=REV)
    first = _signal(1, rr.OperationalSignalKind.EXACT_SEARCH)
    graph = rr.reduce_reasoning_signal(graph, first)
    assert graph.last_source_event_sequence == 2
    assert graph.last_source_event_hash == f"{1:064x}"

    wrong_attempt = rr.OperationalSignal(
        **{
            **first.__dict__,
            "attempt_id": "attempt-other",
            "sequence": 2,
            "source_event_sequence": 4,
            "source_event_hash": f"{2:064x}",
        }
    )
    with pytest.raises(rr.StateIntegrityError, match="attempt"):
        rr.reduce_reasoning_signal(graph, wrong_attempt)


def test_reasoning_graph_rejects_unknown_edge_endpoints_and_subject_rebinding() -> None:
    with pytest.raises(rr.StateIntegrityError, match="endpoint"):
        rr.ReasoningGraph(
            attempt_id="attempt-1",
            revision=REV,
            nodes=(),
            edges=(
                rr.ReasoningEdge(
                    source_id="ghost-a",
                    target_id="ghost-b",
                    kind=rr.ReasoningEdgeKind.SUPPORTS,
                    event_id="ev-1",
                ),
            ),
        )

    graph = rr.reduce_reasoning_signal(
        rr.ReasoningGraph.initial(attempt_id="attempt-1", revision=REV),
        _signal(1, rr.OperationalSignalKind.EXACT_SEARCH),
    )
    rebound = rr.OperationalSignal(
        **{
            **_signal(2, rr.OperationalSignalKind.FOCUSED_SYMBOL_VIEW).__dict__,
            "subject": "different_subject",
        }
    )
    with pytest.raises(rr.StateIntegrityError, match="subject"):
        rr.reduce_reasoning_signal(graph, rebound)
