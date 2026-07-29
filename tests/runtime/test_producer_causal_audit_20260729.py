"""Causal producer instrumentation: no invocation may disappear silently.

These tests exercise telemetry only.  The producer return value and model-visible
bytes must remain identical whether a recorder is attached or not.
"""

from __future__ import annotations

from types import SimpleNamespace

from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
from groundtruth.runtime import gateway
from groundtruth.runtime.producer_audit import ProducerAudit


def _envelope() -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="ranked_localization",
        fact_id="src/pkg/api.py",
        target="src/pkg/api.py",
        evidence_type="localization",
        payload=("src/pkg/api.py:10:parse_url",),
        provenance=(("src/pkg/api.py", 10),),
        confidence=0.7,
        tier="WARNING",
        graph_revision="graph-1",
        preferred_event="search",
    )


def _context() -> dict:
    return {
        "observation_id": "observation-1",
        "decision_id": "SOURCE_TARGET_SELECTION:abc",
        "decision_context": "SOURCE_TARGET_SELECTION",
        "decision_open": True,
        "required_roles": ["TARGET_IDENTITY"],
        "roles_present_before": [],
        "required_roles_satisfied_before": False,
    }


def test_abstention_has_entered_and_exact_terminal_reason() -> None:
    rows: list[dict] = []
    audit = ProducerAudit(
        recorder=rows.append,
        producer="def_ref_partition",
        evidence_types=("def_ref_partition",),
        invocation_site="gateway.search.ambiguous",
        event_type="search_result",
        subject="",
        action_index=3,
        context=_context(),
    )
    audit.note(
        "search_pattern_missing",
        category="dependency_failure",
        detail={"command_shape": "non_symbol"},
    )
    returned: list[EvidenceEnvelope] = []
    assert audit.finish(returned) is returned

    assert [row["outcome"] for row in rows] == [
        "entered",
        "returned_nothing",
    ]
    terminal = rows[-1]
    assert terminal["returned_nothing"] is True
    assert terminal["returned_fact"] is False
    assert terminal["abstention_reasons"] == [
        {
            "category": "dependency_failure",
            "reason": "search_pattern_missing",
            "detail": {"command_shape": "non_symbol"},
        }
    ]
    assert terminal["decision_open"] is True
    assert terminal["required_roles_satisfied_before"] is False
    assert terminal["registry_allowed"] is True


def test_unexplained_empty_return_is_loud_not_silent() -> None:
    rows: list[dict] = []
    audit = ProducerAudit(
        recorder=rows.append,
        producer="trace",
        evidence_types=("trace_frame",),
        invocation_site="gateway.trace",
        event_type="failure_obs",
        subject="",
        action_index=7,
        context=_context(),
    )
    audit.finish([])

    assert rows[-1]["outcome"] == "returned_nothing"
    assert rows[-1]["abstention_reasons"] == [
        {
            "category": "instrumentation_gap",
            "reason": "unexplained_abstention",
            "detail": {},
        }
    ]


def test_returned_fact_records_authority_confidence_and_dedup() -> None:
    rows: list[dict] = []
    delivered = set()
    env = _envelope()
    audit = ProducerAudit(
        recorder=rows.append,
        producer="ranked_localization",
        evidence_types=("localization",),
        invocation_site="gateway.search.ranked_localization",
        event_type="search_result",
        subject="parse_url",
        action_index=4,
        context=_context(),
        delivered_keys=delivered,
    )
    returned = [env]
    assert audit.finish(returned) is returned

    terminal = rows[-1]
    assert terminal["outcome"] == "returned_fact"
    assert terminal["returned_fact"] is True
    assert terminal["returned_nothing"] is False
    assert terminal["confidence"] == [0.7]
    assert terminal["dedup_result"] == "novel"
    assert terminal["authority_result"] == "producer_registered"
    assert terminal["facts"][0]["candidate_id"] == env.dedup_key


def test_recorder_failure_cannot_change_producer_value() -> None:
    def broken(_row: dict) -> None:
        raise RuntimeError("telemetry unavailable")

    env = _envelope()
    audit = ProducerAudit(
        recorder=broken,
        producer="ranked_localization",
        evidence_types=("localization",),
        invocation_site="gateway.search.ranked_localization",
        event_type="search_result",
        subject="parse_url",
        action_index=4,
        context=_context(),
    )
    returned = [env]
    assert audit.finish(returned) is returned


def test_fault_has_terminal_row_and_does_not_claim_abstention() -> None:
    rows: list[dict] = []
    audit = ProducerAudit(
        recorder=rows.append,
        producer="caller_contract",
        evidence_types=("caller_contract_view",),
        invocation_site="gateway.file_view.caller_contract",
        event_type="file_view",
        subject="src/api.py",
        action_index=8,
        context=_context(),
    )
    audit.fault(ValueError("bad graph row"))

    terminal = rows[-1]
    assert terminal["outcome"] == "fault"
    assert terminal["returned_nothing"] is False
    assert terminal["fault_type"] == "ValueError"
    assert "bad graph row" not in str(terminal), "exception text may contain private data"


def test_optional_context_accepts_runtime_like_objects() -> None:
    """The audit boundary must not require reasoning-runtime imports or mutate state."""
    rows: list[dict] = []
    context = SimpleNamespace(
        observation_id="obs",
        decision_id="decision",
        decision_context="PATCH_PROPAGATION",
        decision_open=True,
        required_roles=("VALIDATION",),
        roles_present_before=("BLOCKER",),
        required_roles_satisfied_before=False,
    )
    audit = ProducerAudit(
        recorder=rows.append,
        producer="edit_check",
        evidence_types=("syntax_result",),
        invocation_site="canonical.edit_check",
        event_type="edit_result",
        subject="src/api.py",
        action_index=9,
        context=context,
    )
    audit.note("clean_parse", category="correct_quiet")
    audit.finish(None)
    assert rows[-1]["required_roles"] == ["VALIDATION"]
    assert rows[-1]["roles_present_before"] == ["BLOCKER"]


def test_gateway_producer_names_its_exact_empty_branch() -> None:
    rows: list[dict] = []
    state = gateway.GatewayState(
        producer_recorder=rows.append,
        producer_audit_context=_context(),
    )
    event = gateway.ToolEvent(
        kind=gateway.KIND_SEARCH,
        command="grep -rn 'two words' src/",
        output="",
        action_index=12,
        semantic_events=("search_result",),
        primary_boundary="search_result",
        semantics_authoritative=True,
    )

    assert gateway._produce_def_ref_partition(event, state) == []
    assert [row["outcome"] for row in rows] == [
        "entered",
        "returned_nothing",
    ]
    assert rows[-1]["abstention_reasons"] == [
        {
            "category": "dependency_failure",
            "reason": "search_pattern_missing",
            "detail": {},
        }
    ]


def test_gateway_audit_is_return_value_inert() -> None:
    event = gateway.ToolEvent(
        kind=gateway.KIND_SEARCH,
        command="grep -rn parse_url src/",
        output="",
        action_index=13,
        semantic_events=("search_result",),
        primary_boundary="search_result",
        semantics_authoritative=True,
    )
    without = gateway.GatewayState()
    rows: list[dict] = []
    with_audit = gateway.GatewayState(
        producer_recorder=rows.append,
        producer_audit_context=_context(),
    )
    assert gateway._produce_def_ref_partition(event, without) == (
        gateway._produce_def_ref_partition(event, with_audit)
    )
    assert rows[-1]["outcome"] == "returned_nothing"
