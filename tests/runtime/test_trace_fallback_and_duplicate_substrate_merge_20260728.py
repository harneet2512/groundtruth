"""RED contracts for trace fallback and duplicate substrate reconciliation."""

from __future__ import annotations

from dataclasses import replace
from itertools import permutations

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import evidence_envelope as ee
from groundtruth.runtime import fact_registry
from groundtruth.runtime import feature_lineage
from groundtruth.runtime import gateway
from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-trace",
    graph="degraded:graph-trace",
    lsp="lsp-trace",
    runtime_evidence="runtime-trace",
)

SATISFIED = frozenset(
    {
        rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
        rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
        rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
        rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
        rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
        rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
        rr.TemporalPredicate.AUTHORIZED_BYTE_OWNER_LINEAGE_PRESENT,
    }
)


def test_trace_frame_uses_repository_path_fallback_through_degraded_graph(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    trace_file = tmp_path / "svc" / "watch.py"
    trace_file.parent.mkdir(parents=True)
    trace_file.write_text(
        "def run():\n    raise ValueError('boom')\n",
        encoding="utf-8",
    )
    event = gateway.ToolEvent(
        kind="test",
        command="pytest -q",
        output=(
            "Traceback (most recent call last):\n"
            '  File "svc/watch.py", line 2, in run\n'
            "    raise ValueError('boom')\n"
            "ValueError: boom\n"
        ),
        action_index=1,
    )
    state = gateway.GatewayState(
        graph_db=str(tmp_path / "absent-graph.db"),
        repo_root=str(tmp_path),
        canonical_revision=REVISION,
    )

    envelopes = gateway.augment(event, state)
    trace = next(
        envelope
        for envelope in envelopes
        if envelope.evidence_type == "trace_frame"
    )
    record = rr.canonical_evidence_from_envelope(trace)

    assert trace.canonical_semantics is not None
    assert trace.canonical_semantics.observed_substrates == (
        "repository_paths",
    )
    assert seam._envelope_observes_graph(trace) is False
    assert fact_registry.freshness_surfaces("trace_frame") is None
    assert record is not None
    assert record.observed_substrates == ("repository_paths",)

    journal = rr.RuntimeJournal(tmp_path / "trace-fallback.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-trace-fallback",
        journal=journal,
        initial_revision=REVISION,
    )
    runtime.ingest_evidence(record)
    decision = rr.ActiveDecision(
        decision_id="decision:trace-fallback",
        context=record.decision_context,
        primary_claim="localize the observed failure",
        required_roles=record.roles,
        causal_neighborhood=record.causal_neighborhood,
        token_budget=180,
        current_revision=REVISION,
    )
    try:
        plan = runtime.prepare_next_inference(
            decisions=(decision,),
            satisfied_predicates=SATISFIED,
            commitment_window=rr.CommitmentWindowState.OPEN,
            available_substrates=("repository_paths",),
            native_observation=event.output,
            observation_id="observation:trace-fallback",
            source_model_call_id="model:trace-source",
            model_call_id="model:trace-next",
        )
    finally:
        journal.close()

    assert tuple(
        item.evidence_id for item in plan.oracle_decision.coalition
    ) == (record.evidence_id,)
    assert plan.delivery_attempt_id
    assert plan.compilation.state is rr.CapsuleCompilationState.COMPILED


def _duplicate_envelope(
    owner_id: str,
    observed_substrates: tuple[str, ...],
) -> ee.EvidenceEnvelope:
    fact_class = "submit_refusal"
    registration = fact_registry.REGISTRY[fact_class]
    contract = rr.feature_contract_for(fact_class)
    assert contract is not None
    lineage = feature_lineage.build_lineage(
        runtime_producer_id=registration.producer,
        evidence_type=fact_class,
        actual_event=fact_registry.required_event(fact_class),
    )
    assert lineage is not None
    lineage = replace(
        lineage,
        features=tuple(
            sorted(
                {
                    feature_lineage.FeatureRef(
                        "FACT",
                        fact_class,
                        "fact",
                    ),
                    feature_lineage.FeatureRef(
                        "CAP",
                        owner_id,
                        "byte_owner",
                    ),
                }
            )
        ),
    )
    semantics = rr.CanonicalEvidenceSemantics(
        decision_context=contract.decision_context,
        roles=contract.roles,
        claim="Submission lacks required validation.",
        actionable_consequence="Completion remains blocked.",
        causal_neighborhood=(
            f"decision:{contract.decision_context.value}",
            "subject:submission",
        ),
        authority=rr.Authority.RESULT_DERIVED,
        revision=REVISION,
        revision_dependencies=contract.revision_dependencies,
        mandatory_reason=rr.MandatoryReason.BLOCKER,
        failure_prevention=8,
        causal_value=7,
        contradiction_resolution=5,
        anchoring_risk=0,
        observed_substrates=observed_substrates,
    )
    return ee.EvidenceEnvelope.build(
        producer=registration.producer,
        fact_id="physical:duplicate-substrates",
        target="submission",
        evidence_type=fact_class,
        payload=("submission remains blocked",),
        provenance=(("src/product.py", 1),),
        confidence=0.91,
        tier=ee.VERIFIED,
        graph_revision=REVISION.graph,
        valid_until=REVISION.graph,
        preferred_event=ee.EVENT_SUBMIT,
        estimated_cost_tokens=24,
        lineage=lineage,
        producer_inputs={"verdict": "blocked"},
        canonical_semantics=semantics,
    )


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    (
        (("parser_result",), ("parser_result",), ("parser_result",)),
        ((), ("parser_result",), ()),
        (("graph",), ("parser_result",), ()),
        (
            ("parser_result", "repository_paths"),
            ("repository_paths", "structured_test_result"),
            ("repository_paths",),
        ),
    ),
    ids=("identical", "empty", "disjoint", "partial-overlap"),
)
def test_duplicate_substrates_use_order_independent_intersection_and_owner_union(
    left: tuple[str, ...],
    right: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    first = _duplicate_envelope("GT_SS_SUBMIT_RED", left)
    second = _duplicate_envelope("GT_CERT_DELIVERY", right)
    assert first.dedup_key == second.dedup_key

    forward = rr.canonicalize_evidence_envelopes((first, second))
    reverse = rr.canonicalize_evidence_envelopes((second, first))

    assert len(forward) == len(reverse) == 1
    assert forward[0].observed_substrates == expected
    assert reverse[0].observed_substrates == expected
    assert forward[0].owner_feature_ids == reverse[0].owner_feature_ids == (
        "GT_CERT_DELIVERY",
        "GT_SS_SUBMIT_RED",
    )


def test_conflicting_physical_identity_is_poisoned_for_the_whole_batch() -> None:
    trusted = _duplicate_envelope(
        "GT_SS_SUBMIT_RED",
        ("parser_result", "repository_paths"),
    )
    co_owner = _duplicate_envelope(
        "GT_CERT_DELIVERY",
        ("repository_paths", "structured_test_result"),
    )
    assert trusted.canonical_semantics is not None
    conflicting = replace(
        trusted,
        canonical_semantics=replace(
            trusted.canonical_semantics,
            claim="Conflicting semantics under the same physical identity.",
        ),
    )
    assert {
        envelope.dedup_key
        for envelope in (trusted, co_owner, conflicting)
    } == {trusted.dedup_key}

    for ordering in permutations((trusted, co_owner, conflicting)):
        assert rr.canonicalize_evidence_envelopes(ordering) == ()
