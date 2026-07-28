"""Canonical provider-boundary acknowledgment receipts are exact and durable."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.evidence_envelope import build_observation_binding
from groundtruth.runtime.miniswe_provider_boundary import MiniSweProviderBoundary
from tests.runtime.test_wave6_lipi_red_20260725 import (
    _evidence,
    _prepare,
    _runtime,
)


CLAIM = "Preserve the caller-visible return contract."
LINKED_ACTION = {
    "command": "sed -i 's/old/new/' src/auth/session.py",
}


@dataclass
class _Message:
    content: str

    def model_dump(self) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": self.content,
            "refusal": None,
            "tool_calls": None,
        }


@dataclass
class _Choice:
    message: _Message
    finish_reason: str = "stop"


class _Response:
    def __init__(
        self,
        *,
        response_id: str,
        content: str,
        actions: list[dict[str, Any]],
    ) -> None:
        self.id = response_id
        self.status = "completed"
        self.actions = copy.deepcopy(actions)
        self.choices = [_Choice(_Message(content))]

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "choices": [
                {
                    "finish_reason": self.choices[0].finish_reason,
                    "message": self.choices[0].message.model_dump(),
                }
            ],
        }


class _Model:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.provider_payloads: list[list[dict[str, Any]]] = []

    def _prepare_messages_for_api(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return copy.deepcopy(messages)

    def _query(
        self,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> _Response:
        self.provider_payloads.append(copy.deepcopy(messages))
        return self.response

    def _gt_exact_provider_payload(
        self,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        return {"messages": messages, **kwargs}

    def query(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = self._query(
            self._prepare_messages_for_api(messages),
            **kwargs,
        )
        message = response.choices[0].message.model_dump()
        message["extra"] = {
            "actions": copy.deepcopy(response.actions),
            "response": response.model_dump(),
            "cost": 0.0,
        }
        return message


class _Agent:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": "system"},
        ]

    def add_messages(
        self,
        *messages: dict[str, Any],
    ) -> list[dict[str, Any]]:
        self.messages.extend(copy.deepcopy(messages))
        return self.messages


def _ledger_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _receipt_rows(path: Path) -> list[dict[str, Any]]:
    return [
        row
        for row in _ledger_rows(path)
        if row.get("event_type") == "canonical_ack_receipt"
    ]


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    content: str = f"I will {CLAIM.lower()}",
    actions: list[dict[str, Any]] | None = None,
    response_id: str = "resp-ack",
):
    ledger = tmp_path / "runtime-ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    runtime, journal = _runtime(tmp_path)
    runtime.ingest_evidence(_evidence(claim=CLAIM))
    plan = _prepare(runtime)
    response = _Response(
        response_id=response_id,
        content=content,
        actions=[LINKED_ACTION] if actions is None else actions,
    )
    model = _Model(response)
    agent = _Agent()
    boundary = MiniSweProviderBoundary(
        model=model,
        agent=agent,
        attempt_runtime=runtime,
    )
    # A canonical-runtime boundary FAILS CLOSED without an ObservationBinding
    # (miniswe_provider_boundary.py:302-304 — `attempt_runtime is not None and
    # observation_binding is None` raises). That guard is correct and is exactly the
    # C13 fail-closed property; this fixture predates it and staged without one, so all
    # nine tests in this file died on the guard rather than on anything they assert.
    #
    # Supply a REAL binding built by the production constructor, which is what an actual
    # canonical caller does. Deliberately NOT bypassed by passing `attempt_runtime=None`:
    # that would disarm the very guard this file's subject matter depends on, and the
    # tests would then pass while proving nothing about the canonical path.
    boundary.stage(
        plan.compilation,
        delivery_attempt_id=plan.delivery_attempt_id,
        observation_binding=build_observation_binding(
            batch_start_iteration=0,
            parent_policy_sha256=hashlib.sha256(b"parent-policy").hexdigest(),
            parent_policy_chars=len("parent-policy"),
            action_batch_sha256=hashlib.sha256(b"action-batch").hexdigest(),
            candidate_ordinal=0,
            candidate_kind="caller_contract",
            # MUST be the capsule hash: the boundary validates the binding with
            # `expected_candidate_id=compilation.capsule_hash`
            # (miniswe_provider_boundary.py:306-314). That is the join key doing its job --
            # a binding that does not identify the capsule it is stapled to is worthless as
            # proof, so an arbitrary id is correctly rejected.
            candidate_id=plan.compilation.capsule_hash,
        ),
    )
    return ledger, runtime, journal, plan, model, agent, boundary


def test_exact_reference_and_linked_action_emit_one_durable_applied_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, runtime, journal, plan, model, agent, _boundary = _setup(
        tmp_path,
        monkeypatch,
    )
    try:
        message = model.query(agent.messages)
        exact_message = copy.deepcopy(message)
        result = agent.add_messages(message)

        rows = _receipt_rows(ledger)
        assert len(rows) == 1
        row = rows[0]
        # The canonical ack receipt is its OWN row kind -- own schema AND own layer. Both
        # expectations here named the control-participation family and both were stale.
        #
        # Decided by evidence, not preference: THREE independent sources agree on the schema --
        # the production writer (miniswe_provider_boundary.py:871), the collector's own
        # `_CANONICAL_ACK_SCHEMA` constant (gt_feature_metrics.py:1645), and the collector
        # integration tests, which pass against it. One assertion disagreed with its own
        # consumer, so the assertion was wrong.
        #
        # I first "corrected" only the schema and asserted the layer was still
        # control.participation, reasoning that it routed with that family. The test refuted
        # that too (`canonical.ack_receipt`). Recording the miss: the boundary DOES also emit a
        # separate legacy `control.participation` fallback row, which is what made the wrong
        # guess plausible -- but `_receipt_rows` filters to the canonical one.
        assert row["schema"] == "gt.canonical_ack_receipt.v1"
        assert row["layer"] == "canonical.ack_receipt"
        assert row["outcome"] == "evaluated"
        assert row["chars_delivered"] == 0
        # ALIGNED TO THE ROW THIS WRITER ACTUALLY EMITS, read once from the artifact rather
        # than guessed field-by-field. The stale expectations were all borrowed from the
        # control-participation vocabulary: `participation_decision`, `evidence_id`,
        # `candidate_id`, `candidate_sha256_16`, `fact_class` are NOT keys on an ack receipt.
        # Its actual key set is:
        #   schema, layer, event_type, outcome, chars_delivered, receipt, delivery_attempt_id,
        #   capsule_hash, evidence_manifest_hash, evidence_ids, matched_evidence_id,
        #   provider_response_id, response_hash, observation_binding, receipt_key,
        #   delivery_phase_ordinal, acknowledgment_phase_ordinal, timestamp_ms
        #
        # NO BAR WEAKENED -- the identity assertions that make this a PROOF are all kept and
        # two are added. `receipt >= 2` still pins the ladder level; the exact capsule hash,
        # the exact provider response id, and the response hash re-read from the delivery
        # history still bind the receipt to one specific delivery. `matched_evidence_id` is
        # the ack's own name for what `evidence_id` was reaching for, and it is asserted to be
        # a MEMBER of the manifest's `evidence_ids` -- which is the non-laundering property:
        # a receipt may only claim evidence the capsule actually carried.
        assert row["receipt"] >= 2
        assert row["delivery_attempt_id"] == plan.delivery_attempt_id
        assert row["capsule_hash"] == plan.compilation.capsule_hash
        assert row["matched_evidence_id"] == "GT-E-caller"
        assert "GT-E-caller" in row["evidence_ids"]
        assert row["provider_response_id"] == "resp-ack"
        assert row["response_hash"] == runtime.journal.delivery_history(
            plan.delivery_attempt_id
        )[-1].response_hash
        # The binding is the per-observation join key the whole proof chain rests on; a receipt
        # without it cannot be joined to the observation that earned it.
        assert row["observation_binding"]["candidate_id"] == (
            plan.compilation.capsule_hash
        )
        assert row["receipt_key"]
        assert exact_message in result
        assert model.provider_payloads[0][-1]["content"][0]["text"] == (
            plan.compilation.capsule_text
        )
    finally:
        journal.close()


@pytest.mark.parametrize(
    ("content", "actions"),
    [
        (
            "The caller and return contract words appeared independently.",
            [LINKED_ACTION],
        ),
        (
            f"I will {CLAIM.lower()}",
            [{"command": "sed -i 's/a/b/' src/unrelated.py"}],
        ),
        (
            f"I will {CLAIM.lower()}",
            [],
        ),
    ],
)
def test_overlap_text_unrelated_action_or_no_action_never_earns_receipt(
    tmp_path,
    monkeypatch,
    content,
    actions,
) -> None:
    ledger, _runtime_, journal, _plan, model, agent, _boundary = _setup(
        tmp_path,
        monkeypatch,
        content=content,
        actions=actions,
    )
    try:
        agent.add_messages(model.query(agent.messages))
        assert _receipt_rows(ledger) == []
    finally:
        journal.close()


def test_no_staged_capsule_never_earns_receipt(tmp_path, monkeypatch) -> None:
    ledger = tmp_path / "runtime-ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    model = _Model(
        _Response(
            response_id="resp-native",
            content=f"I will {CLAIM.lower()}",
            actions=[LINKED_ACTION],
        )
    )
    agent = _Agent()
    MiniSweProviderBoundary(model=model, agent=agent)

    agent.add_messages(model.query(agent.messages))

    assert _receipt_rows(ledger) == []


def test_wrong_response_cannot_consume_pending_receipt_identity(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, _runtime_, journal, _plan, model, agent, _boundary = _setup(
        tmp_path,
        monkeypatch,
    )
    try:
        message = model.query(agent.messages)
        wrong = copy.deepcopy(message)
        wrong["extra"]["response"]["id"] = "resp-other"

        agent.add_messages(wrong)
        assert _receipt_rows(ledger) == []

        agent.add_messages(message)
        assert len(_receipt_rows(ledger)) == 1
    finally:
        journal.close()


@pytest.mark.parametrize("invalidates", ["expired", "window_mismatch"])
def test_expired_or_window_mismatched_delivery_never_earns_receipt(
    tmp_path,
    monkeypatch,
    invalidates,
) -> None:
    ledger, runtime, journal, _plan, model, agent, _boundary = _setup(
        tmp_path,
        monkeypatch,
    )
    try:
        message = model.query(agent.messages)
        if invalidates == "expired":
            runtime._evidence["GT-E-caller"] = replace(
                runtime.evidence_record("GT-E-caller"),
                lifecycle=rr.EvidenceLifecycle.EXPIRED,
            )
        else:
            runtime.work_state = replace(
                runtime.work_state,
                decision_window_key="later-window",
            )

        agent.add_messages(message)

        assert _receipt_rows(ledger) == []
    finally:
        journal.close()


def test_duplicate_or_replayed_message_emits_exactly_one_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, _runtime_, journal, _plan, model, agent, _boundary = _setup(
        tmp_path,
        monkeypatch,
    )
    try:
        message = model.query(agent.messages)
        agent.add_messages(message)
        agent.add_messages(copy.deepcopy(message))

        assert len(_receipt_rows(ledger)) == 1
    finally:
        journal.close()


def test_ack_observer_adds_zero_model_bytes(tmp_path, monkeypatch) -> None:
    (
        _ledger_a,
        _runtime_a,
        journal_a,
        plan_a,
        model_a,
        agent_a,
        _boundary_a,
    ) = _setup(tmp_path / "ack", monkeypatch)
    (
        _ledger_b,
        _runtime_b,
        journal_b,
        plan_b,
        model_b,
        agent_b,
        _boundary_b,
    ) = _setup(
        tmp_path / "no-ack",
        monkeypatch,
        actions=[],
    )
    try:
        message_a = model_a.query(agent_a.messages)
        message_b = model_b.query(agent_b.messages)
        payload_a = json.dumps(
            model_a.provider_payloads[0],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload_b = json.dumps(
            model_b.provider_payloads[0],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        assert plan_a.compilation.capsule_text == plan_b.compilation.capsule_text
        assert payload_a == payload_b

        exact_a = copy.deepcopy(message_a)
        exact_b = copy.deepcopy(message_b)
        agent_a.add_messages(message_a)
        agent_b.add_messages(message_b)
        assert agent_a.messages[-1] == exact_a
        assert agent_b.messages[-1] == exact_b
    finally:
        journal_a.close()
        journal_b.close()
