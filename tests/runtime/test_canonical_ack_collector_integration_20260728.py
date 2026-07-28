"""RED contracts for canonical provider delivery -> acknowledgment grading.

The canonical provider path delivers one capsule that may carry several FACT
records. It must not be laundered into the legacy single-FACT delivery-row
shape. These tests exercise the real provider writer and the authoritative
feature collector together; every mutation test first requires its unmodified
control to grade, so a globally broken collector cannot make the negatives
pass vacuously.
"""

from __future__ import annotations

import copy
import inspect
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swebench"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from scripts.swebench.gt_feature_metrics import (  # noqa: E402
    _control_participation_evidence,
)
from groundtruth.runtime import reasoning_runtime as rr  # noqa: E402
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    build_observation_binding,
    observation_binding_to_dict,
)
from groundtruth.runtime import (  # noqa: E402
    miniswe_provider_boundary as _boundary_module,
)
from groundtruth.runtime.miniswe_provider_boundary import (  # noqa: E402
    MiniSweProviderBoundary,
)
from tests.runtime.test_canonical_ack_receipt_20260728 import (  # noqa: E402
    CLAIM,
    LINKED_ACTION,
    _Agent,
    _Model,
    _Response,
    _ledger_rows,
)
from tests.runtime.test_wave6_lipi_red_20260725 import (  # noqa: E402
    _evidence,
    _prepare,
    _runtime,
)


ACK_FEATURE = "GT_SS_ACK_METRICS"
DELIVERY_SCHEMA = "gt.canonical_delivery.v1"
ACK_SCHEMA = "gt.canonical_ack_receipt.v1"


def _binding(plan: rr.InferencePlan):
    return build_observation_binding(
        batch_start_iteration=0,
        parent_policy_sha256="1" * 64,
        parent_policy_chars=12,
        action_batch_sha256="2" * 64,
        candidate_ordinal=0,
        candidate_kind="canonical_runtime.capsule",
        candidate_id=plan.compilation.capsule_hash,
    )


def _second_evidence() -> rr.EvidenceRecord:
    return replace(
        _evidence(
            evidence_id="GT-E-caller-secondary",
            claim="Preserve the secondary cache invalidation contract.",
        ),
        subject="invalidateCache",
        actionable_consequence="Keep cache invalidation observable.",
        provenance=("src/cache/invalidation.py:19",),
    )


def _write_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    two_evidence: bool = False,
    delivery_handler=None,
    fault_handler=None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    rr.InferencePlan,
    rr.RuntimeJournal,
]:
    ledger = tmp_path / "runtime-ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    runtime, journal = _runtime(tmp_path)
    runtime.ingest_evidence(_evidence(claim=CLAIM))
    if two_evidence:
        runtime.ingest_evidence(_second_evidence())
    plan = _prepare(runtime)
    expected_count = 2 if two_evidence else 1
    assert len(plan.compilation.evidence_ids) == expected_count

    response = _Response(
        response_id="resp-canonical-ack",
        content=f"I will {CLAIM.lower()}",
        actions=[LINKED_ACTION],
    )
    model = _Model(response)
    agent = _Agent()
    boundary = MiniSweProviderBoundary(
        model=model,
        agent=agent,
        attempt_runtime=runtime,
        delivery_handler=delivery_handler,
        fault_handler=fault_handler,
    )
    # The binding must survive staging through provider terminal and response
    # commitment; reconstructing it later from a capsule hash is not proof of
    # the parent policy/action opportunity that owned the delivery.
    stage_parameters = inspect.signature(boundary.stage).parameters
    if "observation_binding" in stage_parameters:
        boundary.stage(
            plan.compilation,
            delivery_attempt_id=plan.delivery_attempt_id,
            observation_binding=_binding(plan),
        )
    else:
        # Current RED path: still run the real provider lifecycle so the first
        # failure is the absent canonical delivery schema/collector join, not a
        # synthetic TypeError at the proposed binding API.
        boundary.stage(
            plan.compilation,
            delivery_attempt_id=plan.delivery_attempt_id,
        )
    committed = model.query(agent.messages)
    agent.add_messages(committed)
    return _ledger_rows(ledger), copy.deepcopy(agent.messages), plan, journal


def _collect(
    rows: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    return _control_participation_evidence(
        rows,
        messages,
        {
            "schema": "gt.consumption_ledger.v2",
            "entries": [],
        },
    )


def _ack_join(result: dict[str, Any]) -> list[dict[str, Any]]:
    return list(result.get("joins", {}).get(ACK_FEATURE, ()))


def _assert_valid(
    rows: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    plan: rr.InferencePlan,
    *,
    matched_evidence_id: str = "GT-E-caller",
) -> None:
    deliveries = [
        row for row in rows if row.get("schema") == DELIVERY_SCHEMA
    ]
    receipts = [
        row for row in rows if row.get("schema") == ACK_SCHEMA
    ]
    assert len(deliveries) == len(receipts) == 1
    delivery = deliveries[0]
    receipt = receipts[0]
    binding = observation_binding_to_dict(_binding(plan))
    assert delivery["event_type"] == "canonical_provider_delivery"
    assert delivery["outcome"] == "delivered"
    assert delivery["delivery_attempt_id"] == plan.delivery_attempt_id
    assert delivery["capsule_hash"] == plan.compilation.capsule_hash
    assert delivery["evidence_manifest_hash"] == (
        plan.compilation.evidence_manifest_hash
    )
    assert delivery["evidence_ids"] == list(plan.compilation.evidence_ids)
    assert delivery["observation_binding"] == binding
    assert receipt["event_type"] == "canonical_ack_receipt"
    assert receipt["delivery_attempt_id"] == plan.delivery_attempt_id
    assert receipt["matched_evidence_id"] == matched_evidence_id
    assert receipt["observation_binding"] == binding

    result = _collect(rows, messages)
    assert result["invalid_rows"] == []
    joins = _ack_join(result)
    assert len(joins) == 1
    assert joins[0]["delivery_attempt_id"] == plan.delivery_attempt_id
    assert joins[0]["capsule_hash"] == plan.compilation.capsule_hash
    assert joins[0]["matched_evidence_id"] == matched_evidence_id
    assert joins[0]["canonical_delivery_joined"] is True
    assert joins[0]["receipt_level"] >= 3


def _assert_rejected(
    rows: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> None:
    result = _collect(rows, messages)
    assert not _ack_join(result)


def test_real_writer_grades_one_canonical_provider_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows, messages, plan, journal = _write_attempt(tmp_path, monkeypatch)
    try:
        _assert_valid(rows, messages, plan)
    finally:
        journal.close()


def test_multi_evidence_capsule_grades_only_the_exact_matched_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows, messages, plan, journal = _write_attempt(
        tmp_path,
        monkeypatch,
        two_evidence=True,
    )
    try:
        _assert_valid(rows, messages, plan)
        joins = _ack_join(_collect(rows, messages))
        assert joins[0]["matched_evidence_id"] == "GT-E-caller"
        assert joins[0]["evidence_ids"] == list(
            plan.compilation.evidence_ids
        )
        assert "fact_class" not in next(
            row for row in rows if row.get("schema") == DELIVERY_SCHEMA
        )
    finally:
        journal.close()


def _mutate_row(
    rows: list[dict[str, Any]],
    schema: str,
    key: str,
    value: Any,
) -> None:
    row = next(item for item in rows if item.get("schema") == schema)
    row[key] = value


@pytest.mark.parametrize(
    "mutation",
    (
        lambda rows, _messages: _mutate_row(
            rows, DELIVERY_SCHEMA, "schema", "gt.canonical_delivery.v0"
        ),
        lambda rows, _messages: _mutate_row(
            rows, DELIVERY_SCHEMA, "event_type", "control_decision"
        ),
        lambda rows, _messages: _mutate_row(
            rows, ACK_SCHEMA, "delivery_attempt_id", "delivery:wrong"
        ),
        lambda rows, _messages: _mutate_row(
            rows, DELIVERY_SCHEMA, "capsule_text", "tampered capsule"
        ),
        lambda rows, _messages: _mutate_row(
            rows, ACK_SCHEMA, "capsule_hash", "0" * 64
        ),
        lambda rows, _messages: _mutate_row(
            rows, DELIVERY_SCHEMA, "evidence_manifest_hash", "0" * 64
        ),
        lambda rows, _messages: _mutate_row(
            rows, ACK_SCHEMA, "matched_evidence_id", "GT-E-not-in-manifest"
        ),
        lambda rows, _messages: _mutate_row(
            rows,
            ACK_SCHEMA,
            "observation_binding",
            {
                "schema": "gt.observation_binding.v1",
                "opportunity_id": "0" * 64,
                "candidate_id": "0" * 64,
                "candidate_ordinal": 0,
                "candidate_kind": "canonical_runtime.capsule",
                "batch_start_iteration": 0,
                "parent_policy_sha256": "1" * 64,
                "parent_policy_chars": 12,
                "action_batch_sha256": "2" * 64,
            },
        ),
        lambda rows, _messages: _mutate_row(
            rows, ACK_SCHEMA, "provider_response_id", "resp-wrong"
        ),
        lambda rows, _messages: _mutate_row(
            rows, ACK_SCHEMA, "response_hash", "0" * 64
        ),
    ),
    ids=(
        "delivery-schema",
        "delivery-event",
        "delivery-attempt",
        "capsule-text",
        "capsule-hash",
        "manifest-hash",
        "manifest-member",
        "observation-binding",
        "provider-response-id",
        "response-hash",
    ),
)
def test_canonical_ack_identity_mutations_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[list[dict[str, Any]], list[dict[str, Any]]], None],
) -> None:
    rows, messages, plan, journal = _write_attempt(tmp_path, monkeypatch)
    try:
        _assert_valid(rows, messages, plan)
        mutation(rows, messages)
        _assert_rejected(rows, messages)
    finally:
        journal.close()


@pytest.mark.parametrize(
    "mutate_messages",
    (
        lambda messages: messages[-1].__setitem__(
            "content", "I independently chose this edit."
        ),
        lambda messages: messages[-1]["extra"].__setitem__(
            "actions",
            [{"command": "sed -i 's/old/new/' src/unrelated.py"}],
        ),
        lambda messages: messages.append(copy.deepcopy(messages[-1])),
    ),
    ids=("claim-reference", "linked-action", "duplicate-committed-message"),
)
def test_canonical_ack_trajectory_mutations_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate_messages: Callable[[list[dict[str, Any]]], None],
) -> None:
    rows, messages, plan, journal = _write_attempt(tmp_path, monkeypatch)
    try:
        _assert_valid(rows, messages, plan)
        mutate_messages(messages)
        _assert_rejected(rows, messages)
    finally:
        journal.close()


# --------------------------------------------------------------------------- #
# C30 half 3c — `delivery_handler`, the success-path twin of `fault_handler`.
#
# The brief attestations can only be finalized where BOTH halves of the join identity
# exist: the delivered bytes' seal and the evidence lineage. That moment is the canonical
# delivery ROW. The seam registers a handler here rather than the boundary persisting
# attestations itself, because the attestation output root belongs to the seam and
# re-deriving it in the boundary would create a second authority for where audit
# artifacts land.
#
# These live in THIS file, not the boundary suite, because only this harness supplies an
# ObservationBinding — and without one no canonical row is written, so the handler
# correctly never fires. My first attempt asserted against the other harness and read the
# absence as a broken hook; the row-ordering probe showed a row being written by a
# different writer entirely, which is what exposed the mistake.
#
# BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
#   M1 — call the handler BEFORE `append_ledger_line`: the ordering test goes RED, and a
#        handler could attest a delivery the join will never see.
#   M2 — let a handler exception propagate: the fault-isolation test goes RED. Audit
#        persistence may not un-deliver bytes the model has already received.
# --------------------------------------------------------------------------- #
def test_delivery_handler_fires_once_with_the_delivered_compilation(
    tmp_path,
    monkeypatch,
) -> None:
    seen: list[Any] = []
    rows, _messages, plan, _journal = _write_attempt(
        tmp_path, monkeypatch, delivery_handler=seen.append
    )

    delivery = [r for r in rows if r.get("schema") == "gt.canonical_delivery.v1"]
    assert len(delivery) == 1, "no canonical row -- the assertion below would be vacuous"
    assert len(seen) == 1
    assert seen[0].capsule_hash == plan.compilation.capsule_hash
    # The handler receives the identity the join needs, not just a hash.
    assert seen[0].evidence_lineage == plan.compilation.evidence_lineage


def test_delivery_handler_never_fires_before_the_row_is_durable(
    tmp_path,
    monkeypatch,
) -> None:
    """M1. A handler that ran first could attest a delivery the join will never see."""
    order: list[str] = []
    original_append = _boundary_module.append_ledger_line

    def _tracking_append(row, path):
        if isinstance(row, dict) and row.get("schema") == "gt.canonical_delivery.v1":
            order.append("row")
        return original_append(row, path)

    monkeypatch.setattr(_boundary_module, "append_ledger_line", _tracking_append)
    _write_attempt(tmp_path, monkeypatch, delivery_handler=lambda _c: order.append("handler"))

    assert "row" in order and "handler" in order, order
    assert order.index("row") < order.index("handler")


def test_a_raising_delivery_handler_cannot_undeliver_the_capsule(
    tmp_path,
    monkeypatch,
) -> None:
    """M2. The bytes are already with the model; an audit fault cannot retract them."""

    def _boom(_compilation):
        raise RuntimeError("attestation persistence blew up")

    faults: list[str] = []
    rows, messages, _plan, _journal = _write_attempt(
        tmp_path,
        monkeypatch,
        delivery_handler=_boom,
        fault_handler=lambda stage, _exc: faults.append(stage),
    )

    delivery = [r for r in rows if r.get("schema") == "gt.canonical_delivery.v1"]
    assert len(delivery) == 1, "the delivery row must still be written"
    assert messages, "the agent must still have received its response"
    # The NAMED stage matters. Asserting only that the row survived passed even with the
    # try/except deleted -- something upstream swallows it too -- so that version of this
    # test guarded nothing. What this module owes is a fault reported UNDER ITS OWN NAME.
    assert "DELIVERY_HANDLER" in faults
