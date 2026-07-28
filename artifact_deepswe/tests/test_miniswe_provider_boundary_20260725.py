"""RED contract for the real mini-swe-agent provider/trajectory boundary.

This suite exercises the installed mini-swe-agent 2.2.8 ``LitellmModel`` and
``DefaultAgent`` control flow with a local fake provider.  It deliberately
does not call a network service.  The missing production adapter must attach
to the actual boundaries in this order:

    _prepare_messages_for_api -> _query -> provider response -> add_messages

The canonical lifecycle values live in ``reasoning_runtime``.  This file pins
only the mini-swe integration: one staged ``CapsuleCompilation`` must be
structurally present in the exact messages handed to ``_query``; provider
terminal proof advances delivery; successful trajectory insertion advances
response commitment.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from types import MethodType, SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from minisweagent.agents.default import DefaultAgent  # noqa: E402
from minisweagent.models import litellm_model as miniswe_litellm_model  # noqa: E402
from minisweagent.models.litellm_model import (  # noqa: E402
    LitellmModel,
    LitellmModelConfig,
)

from groundtruth.runtime.miniswe_provider_boundary import (  # noqa: E402
    MiniSweProviderBoundary,
)
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    build_observation_binding,
)
from groundtruth.runtime.reasoning_runtime import (  # noqa: E402
    DECISION_CAPSULE_SCHEMA as _DECISION_CAPSULE_SCHEMA,
    CapsuleCompilation,
    CapsuleCompilationState,
    DecisionContext,
    DeliveryAttempt,
    DeliveryState,
    EvidenceGrade,
    ProviderTerminalKind,
    advance_delivery,
)


CAPSULE_TEXT = (
    "[GroundTruth · PATCH CONSTRUCTION]\n\n"
    "Decision\n"
    "Preserve the caller-visible return contract.\n"
)
RENDERED_CONTENT_HASH = hashlib.sha256(
    CAPSULE_TEXT.encode("utf-8")
).hexdigest()
EVIDENCE_MANIFEST_HASH = hashlib.sha256(
    b'{"decision":"patch","evidence":["GT-E144"]}'
).hexdigest()
CAPSULE_HASH = hashlib.sha256(
    json.dumps(
        {
            # Imported label, not a literal: this fixture recomputes the capsule hash, so a
            # local copy silently diverges from the writer on every version bump.
            "schema": _DECISION_CAPSULE_SCHEMA,
            "rendered_content_hash": RENDERED_CONTENT_HASH,
            "evidence_manifest_hash": EVIDENCE_MANIFEST_HASH,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()


class FakeProviderAbort(RuntimeError):
    """A non-retryable local stand-in for a provider transport failure."""


@dataclass
class _FakeMessage:
    content: str = "provider output"
    refusal: str | None = None
    tool_calls: list[Any] | None = None

    def model_dump(self) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": self.content,
            "refusal": self.refusal,
            "tool_calls": self.tool_calls,
        }


@dataclass
class _FakeChoice:
    message: _FakeMessage
    finish_reason: str = "stop"


class _FakeResponse:
    def __init__(
        self,
        *,
        response_id: str = "resp-1",
        status: str = "completed",
        finish_reason: str = "stop",
        refusal: str | None = None,
    ) -> None:
        self.id = response_id
        self.status = status
        self.choices = [
            _FakeChoice(
                _FakeMessage(refusal=refusal),
                finish_reason=finish_reason,
            )
        ]

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


class _FakeLitellmModel(LitellmModel):
    abort_exceptions = [FakeProviderAbort]

    def __init__(self, outcomes: list[_FakeResponse | BaseException]) -> None:
        self.config = LitellmModelConfig(
            model_name="fake/provider-model",
            cost_tracking="ignore_errors",
        )
        self._outcomes = list(outcomes)
        self.provider_payloads: list[list[dict[str, Any]]] = []

    def _query(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> _FakeResponse:
        # This is the exact installed-model dispatch boundary: the adapter
        # must have joined and recorded DISPATCHED before control reaches here.
        self.provider_payloads.append(copy.deepcopy(messages))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def _gt_exact_provider_payload(
        self,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Declare the literal request shape used by this test adapter."""

        return {"messages": messages, **kwargs}

    def _calculate_cost(self, response: object) -> dict[str, float]:
        return {"cost": 0.0}

    def _parse_actions(self, response: object) -> list[dict[str, Any]]:
        return []


class _LiteralRequestLitellmModel(LitellmModel):
    """Use Mini-SWE's real ``_query`` while replacing only LiteLLM itself."""

    abort_exceptions = [FakeProviderAbort]

    def __init__(self) -> None:
        self.config = LitellmModelConfig(
            model_name="fake/literal-request-model",
            cost_tracking="ignore_errors",
            model_kwargs={
                "temperature": 0.25,
                "stream": False,
                "metadata": {"trace": "provider-boundary"},
            },
        )

    def _calculate_cost(self, response: object) -> dict[str, float]:
        return {"cost": 0.0}

    def _parse_actions(self, response: object) -> list[dict[str, Any]]:
        return []


class _FakeEnvironment:
    def get_template_vars(self) -> dict[str, Any]:
        return {}

    def execute(self, action: dict[str, Any], cwd: str = "") -> dict[str, Any]:
        raise AssertionError("provider-boundary tests must not execute tools")

    def serialize(self) -> dict[str, Any]:
        return {}


def _agent(model: _FakeLitellmModel) -> DefaultAgent:
    agent = DefaultAgent(
        model=model,
        env=_FakeEnvironment(),
        system_template="{{ task }}",
        instance_template="{{ task }}",
        step_limit=0,
        cost_limit=0.0,
    )
    agent.messages = [
        {"role": "system", "content": "system"},
        {
            "role": "tool",
            "content": "$ sed -n '1,80p' src/auth/session.py\n",
            "extra": {"native_only": True},
        },
    ]
    return agent


def _stage_binding(compilation):
    """A real ObservationBinding for a canonical-runtime stage() call.

    A boundary constructed WITH an attempt_runtime fails closed without one
    (miniswe_provider_boundary.py:302-305) -- the C13 property. The binding must identify
    the staged capsule: the boundary validates with
    expected_candidate_id=compilation.capsule_hash.
    """
    return build_observation_binding(
        batch_start_iteration=0,
        parent_policy_sha256=hashlib.sha256(b"parent-policy").hexdigest(),
        parent_policy_chars=13,
        action_batch_sha256=hashlib.sha256(b"action-batch").hexdigest(),
        candidate_ordinal=0,
        candidate_kind="caller_contract",
        candidate_id=compilation.capsule_hash,
    )


def _compilation(
    *,
    model_call_id: str = "call-13",
    enabled: bool = True,
) -> CapsuleCompilation:
    if not enabled:
        return CapsuleCompilation(
            state=CapsuleCompilationState.DISABLED,
            native_observation="$ sed -n '1,80p' src/auth/session.py\n",
            decision_context=DecisionContext.PATCH_CONSTRUCTION,
            observation_id="obs-12",
            source_model_call_id="call-12",
            model_call_id=model_call_id,
        )
    delivery = advance_delivery(
        DeliveryAttempt(
            evidence_ids=("GT-E144",),
            capsule_hash=CAPSULE_HASH,
            model_call_id=model_call_id,
        ),
        DeliveryState.COMPILED,
        observation_id="obs-12",
    )
    return CapsuleCompilation(
        state=CapsuleCompilationState.COMPILED,
        native_observation="$ sed -n '1,80p' src/auth/session.py\n",
        decision_context=DecisionContext.PATCH_CONSTRUCTION,
        observation_id="obs-12",
        source_model_call_id="call-12",
        model_call_id=model_call_id,
        evidence_ids=("GT-E144",),
        capsule_text=CAPSULE_TEXT,
        capsule_hash=CAPSULE_HASH,
        overall_grade=EvidenceGrade.VERIFIED,
        delivery_attempt=delivery,
        rendered_token_estimate=24,
        rendered_content_hash=RENDERED_CONTENT_HASH,
        evidence_manifest_hash=EVIDENCE_MANIFEST_HASH,
    )


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_exact_compiled_capsule_is_a_structural_final_prepared_message() -> None:
    model = _FakeLitellmModel([_FakeResponse()])
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation())

    message = agent.query()

    exact_provider_messages = model.provider_payloads[0]
    assert exact_provider_messages[-1] == {
        "role": "user",
        "content": [{"type": "text", "text": CAPSULE_TEXT}],
    }
    assert sum(
        block.get("text") == CAPSULE_TEXT
        for provider_message in exact_provider_messages
        for block in (
            provider_message.get("content", [])
            if isinstance(provider_message.get("content"), list)
            else []
        )
        if isinstance(block, dict)
    ) == 1

    joined = boundary.bound_compilations[-1]
    assert joined.binding is not None
    assert joined.binding.message_index == len(exact_provider_messages) - 1
    assert joined.binding.content_index == 0
    assert joined.binding.capsule_hash == CAPSULE_HASH
    assert (
        joined.binding.provider_payload_hash
        == _canonical_hash({"messages": exact_provider_messages})
    )
    assert message in agent.messages


def test_dispatched_hash_covers_literal_final_litellm_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The witness must hash what ``litellm.completion`` receives, not `_query` args."""

    captured_requests: list[dict[str, Any]] = []

    def fake_completion(**request: Any) -> _FakeResponse:
        captured_requests.append(copy.deepcopy(request))
        return _FakeResponse(response_id="resp-literal")

    monkeypatch.setattr(
        miniswe_litellm_model.litellm,
        "completion",
        fake_completion,
    )
    model = _LiteralRequestLitellmModel()
    agent = _agent(model)  # type: ignore[arg-type]
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-literal"))

    model.query(agent.messages, timeout=17)

    literal_request = captured_requests[0]
    assert literal_request["model"] == "fake/literal-request-model"
    assert literal_request["tools"]
    assert literal_request["stream"] is False
    assert literal_request["temperature"] == 0.25
    assert literal_request["metadata"] == {"trace": "provider-boundary"}
    assert literal_request["timeout"] == 17
    assert literal_request["messages"][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": CAPSULE_TEXT}],
    }

    joined = boundary.bound_compilations[-1]
    assert joined.binding is not None
    assert (
        joined.binding.provider_payload_hash
        == _canonical_hash(literal_request)
    )
    assert (
        joined.bound_provider_payload_json
        == json.dumps(
            literal_request,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def test_dispatch_accept_delivery_and_commit_are_distinct_ordered_records() -> None:
    model = _FakeLitellmModel([_FakeResponse(response_id="resp-terminal")])
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation())

    agent.query()

    assert [record.state for record in boundary.records] == [
        DeliveryState.COMPILED,
        DeliveryState.JOINED,
        DeliveryState.DISPATCHED,
        DeliveryState.PROVIDER_ACCEPTED,
        DeliveryState.DELIVERED,
        DeliveryState.RESPONSE_COMMITTED,
    ]
    assert boundary.records[-1].provider_response_id == "resp-terminal"
    assert boundary.records[-1].terminal_kind is ProviderTerminalKind.COMPLETED
    assert boundary.records[-1].response_hash


def test_model_query_delivers_but_agent_add_messages_commits_response() -> None:
    model = _FakeLitellmModel([_FakeResponse()])
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation())

    message = model.query(agent.messages)

    assert boundary.records[-1].state is DeliveryState.DELIVERED
    assert message not in agent.messages

    agent.add_messages(message)

    assert boundary.records[-1].state is DeliveryState.RESPONSE_COMMITTED
    assert message in agent.messages


@pytest.mark.parametrize(
    ("status", "finish_reason", "refusal", "terminal_kind"),
    [
        ("completed", "stop", None, ProviderTerminalKind.COMPLETED),
        ("incomplete", "length", None, ProviderTerminalKind.INCOMPLETE),
        ("completed", "tool_calls", None, ProviderTerminalKind.TOOL_USE),
        ("completed", "stop", "cannot comply", ProviderTerminalKind.REFUSAL),
    ],
)
def test_provider_terminal_kinds_count_as_delivery(
    status: str,
    finish_reason: str,
    refusal: str | None,
    terminal_kind: ProviderTerminalKind,
) -> None:
    model = _FakeLitellmModel(
        [
            _FakeResponse(
                status=status,
                finish_reason=finish_reason,
                refusal=refusal,
            )
        ]
    )
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation())

    model.query(agent.messages)

    assert boundary.records[-1].state is DeliveryState.DELIVERED
    assert boundary.records[-1].terminal_kind is terminal_kind


@pytest.mark.parametrize(
    ("status", "expected_state"),
    [
        ("failed", DeliveryState.INFERENCE_FAILED),
        ("cancelled", DeliveryState.CANCELLED),
        ("partial_stream", DeliveryState.PARTIAL_OUTPUT),
    ],
)
def test_failed_cancelled_and_partial_responses_never_deliver(
    status: str,
    expected_state: DeliveryState,
) -> None:
    model = _FakeLitellmModel([_FakeResponse(status=status)])
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation())

    model.query(agent.messages)

    assert boundary.records[-1].state is expected_state
    assert all(
        record.state
        not in {DeliveryState.DELIVERED, DeliveryState.RESPONSE_COMMITTED}
        for record in boundary.records
    )


def test_response_without_provider_identity_is_persisted_as_rejected() -> None:
    model = _FakeLitellmModel([_FakeResponse(response_id="")])
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation())

    model.query(agent.messages)

    assert boundary.records[-1].state is DeliveryState.PROVIDER_REJECTED
    assert boundary.records[-1].failure_reason == "MISSING_PROVIDER_RESPONSE_ID"
    assert all(
        record.state
        not in {
            DeliveryState.PROVIDER_ACCEPTED,
            DeliveryState.DELIVERED,
            DeliveryState.RESPONSE_COMMITTED,
        }
        for record in boundary.records
    )


def test_explicit_nonterminal_response_stops_at_provider_accepted() -> None:
    model = _FakeLitellmModel(
        [
            _FakeResponse(
                response_id="resp-running",
                status="in_progress",
                finish_reason="stop",
            )
        ]
    )
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-running"))

    prepared = model._prepare_messages_for_api(agent.messages)
    model._query(prepared)

    assert boundary.records[-1].state is DeliveryState.PROVIDER_ACCEPTED
    assert all(
        record.state
        not in {DeliveryState.DELIVERED, DeliveryState.RESPONSE_COMMITTED}
        for record in boundary.records
    )
    assert boundary._active is not None
    assert boundary._active.delivery_attempt is not None
    assert (
        boundary._active.delivery_attempt.state
        is DeliveryState.PROVIDER_ACCEPTED
    )
    assert boundary.has_unconsumed_capsule is True

    with pytest.raises(ValueError, match="already staged"):
        boundary.stage(_compilation(model_call_id="call-while-running"))


def test_nonterminal_provider_response_reconciles_by_exact_response_identity() -> None:
    model = _FakeLitellmModel(
        [
            _FakeResponse(
                response_id="resp-running",
                status="in_progress",
            )
        ]
    )
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-running"))

    prepared = model._prepare_messages_for_api(agent.messages)
    model._query(prepared)
    assert boundary.records[-1].state is DeliveryState.PROVIDER_ACCEPTED

    terminal = boundary.reconcile_provider_response(
        _FakeResponse(
            response_id="resp-running",
            status="completed",
            finish_reason="stop",
        )
    )

    assert terminal.state is DeliveryState.DELIVERED
    assert terminal.provider_response_id == "resp-running"
    assert boundary.records[-1] == terminal
    assert boundary.has_unconsumed_capsule is False
    assert "resp-running" in boundary._pending_commits


def test_nonterminal_reconciliation_rejects_other_response_identity() -> None:
    model = _FakeLitellmModel(
        [
            _FakeResponse(
                response_id="resp-running",
                status="in_progress",
            )
        ]
    )
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-running"))
    prepared = model._prepare_messages_for_api(agent.messages)
    model._query(prepared)

    with pytest.raises(ValueError, match="provider_response identity"):
        boundary.reconcile_provider_response(
            _FakeResponse(
                response_id="resp-other",
                status="completed",
            )
        )

    assert boundary.records[-1].state is DeliveryState.PROVIDER_ACCEPTED
    assert boundary.has_unconsumed_capsule is True
    assert boundary._pending_commits == {}


def test_repeated_nonterminal_reconciliation_preserves_occupied_slot() -> None:
    model = _FakeLitellmModel(
        [
            _FakeResponse(
                response_id="resp-running",
                status="queued",
            )
        ]
    )
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-running"))
    prepared = model._prepare_messages_for_api(agent.messages)
    model._query(prepared)
    records_before = boundary.records

    accepted = boundary.reconcile_provider_response(
        _FakeResponse(
            response_id="resp-running",
            status="in_progress",
        )
    )

    assert accepted.state is DeliveryState.PROVIDER_ACCEPTED
    assert boundary.records == records_before
    assert boundary.has_unconsumed_capsule is True


def test_terminal_persistence_failure_preserves_accepted_occupancy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never clear the slot before terminal truth is durably recorded."""

    import groundtruth.runtime.miniswe_provider_boundary as boundary_module

    model = _FakeLitellmModel(
        [
            _FakeResponse(
                response_id="resp-running",
                status="in_progress",
            )
        ]
    )
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-running"))
    prepared = model._prepare_messages_for_api(agent.messages)
    model._query(prepared)
    accepted = boundary.records[-1]
    monkeypatch.setattr(
        boundary_module,
        "record_provider_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("terminal journal unavailable")
        ),
    )

    with pytest.raises(OSError, match="terminal journal unavailable"):
        boundary.reconcile_provider_response(
            _FakeResponse(
                response_id="resp-running",
                status="completed",
            )
        )

    assert boundary.records[-1] == accepted
    assert boundary._active is not None
    assert boundary._active.delivery_attempt == accepted
    assert boundary.has_unconsumed_capsule is True
    assert boundary._pending_commits == {}


def test_staging_cannot_overwrite_an_unconsumed_compiled_capsule() -> None:
    model = _FakeLitellmModel([_FakeResponse()])
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-first"))

    assert boundary.has_unconsumed_capsule is True

    with pytest.raises(ValueError, match="already staged"):
        boundary.stage(_compilation(model_call_id="call-second"))

    assert boundary._active is not None
    assert boundary._active.model_call_id == "call-first"
    assert boundary.has_unconsumed_capsule is True


def test_disabled_or_later_evidence_cannot_discard_active_capsule() -> None:
    model = _FakeLitellmModel([_FakeResponse()])
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    first = _compilation(model_call_id="call-first")
    later = _compilation(model_call_id="call-later")
    boundary.stage(first)

    # The host uses the public occupancy query before running coalition
    # selection.  Later evidence therefore remains held in the runtime rather
    # than replacing the one capsule already assigned to this observation.
    later_was_staged = False
    if not boundary.has_unconsumed_capsule:
        boundary.stage(later)
        later_was_staged = True

    # A feature-off/disabled compilation is also a no-op.  It must never be a
    # hidden "clear" operation for a capsule whose model inference is pending.
    boundary.stage(_compilation(model_call_id="call-disabled", enabled=False))

    assert later_was_staged is False
    assert boundary.has_unconsumed_capsule is True
    assert boundary._active is first
    assert boundary.records == (first.delivery_attempt,)
    assert later.delivery_attempt is not None
    assert later.delivery_attempt.state is DeliveryState.COMPILED


def test_transport_failure_records_immutable_dispatch_failed_terminal() -> None:
    model = _FakeLitellmModel([FakeProviderAbort("transport failed")])
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-failed"))

    before_dispatch = boundary.records
    with pytest.raises(FakeProviderAbort, match="transport failed"):
        model.query(agent.messages)

    assert [record.state for record in boundary.records] == [
        DeliveryState.COMPILED,
        DeliveryState.JOINED,
        DeliveryState.DISPATCHED,
        DeliveryState.DISPATCH_FAILED,
    ]
    assert boundary.records[: len(before_dispatch)] == before_dispatch
    assert boundary.records[-1].model_call_id == "call-failed"
    assert all(record.provider_response_id == "" for record in boundary.records)
    assert boundary._active is None
    assert boundary.has_unconsumed_capsule is False


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(
            id="resp-empty-choices",
            status="completed",
            choices=[],
        ),
        SimpleNamespace(
            id="resp-empty-message",
            status="completed",
            choices=[
                SimpleNamespace(
                    message=None,
                    finish_reason="",
                )
            ],
        ),
        SimpleNamespace(
            id="resp-missing-status",
            choices=[],
        ),
    ],
)
def test_malformed_provider_terminal_never_defaults_to_completed(
    response: object,
) -> None:
    model = _FakeLitellmModel([response])  # type: ignore[list-item]
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-malformed"))

    prepared = model._prepare_messages_for_api(agent.messages)
    model._query(prepared)

    assert boundary.records[-1].state is DeliveryState.INFERENCE_FAILED
    assert boundary.records[-1].terminal_kind is ProviderTerminalKind.FAILED
    assert all(
        record.state
        not in {DeliveryState.DELIVERED, DeliveryState.RESPONSE_COMMITTED}
        for record in boundary.records
    )


def test_provider_response_without_identity_is_rejected_and_slot_is_cleared() -> None:
    response = SimpleNamespace(
        status="completed",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(refusal=None),
                finish_reason="stop",
            )
        ],
    )
    model = _FakeLitellmModel([response])  # type: ignore[list-item]
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-no-response-id"))

    prepared = model._prepare_messages_for_api(agent.messages)
    model._query(prepared)

    assert boundary.records[-1].state is DeliveryState.PROVIDER_REJECTED
    assert boundary.records[-1].provider_response_id == ""
    assert boundary.records[-1].failure_reason == "MISSING_PROVIDER_RESPONSE_ID"
    assert boundary._active is None


def test_terminal_delivery_clears_active_injection_but_remains_committable() -> None:
    model = _FakeLitellmModel(
        [
            _FakeResponse(response_id="resp-first"),
            _FakeResponse(response_id="resp-native"),
        ]
    )
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-first"))

    message = model.query(agent.messages)

    assert boundary._active is None
    assert boundary.records[-1].state is DeliveryState.DELIVERED

    # Clearing the injection slot must not erase the response awaiting causal
    # trajectory commitment.
    agent.add_messages(message)
    assert boundary.records[-1].state is DeliveryState.RESPONSE_COMMITTED

    records_after_commit = boundary.records
    model.query(agent.messages)
    assert boundary.records == records_after_commit
    assert sum(
        block.get("text") == CAPSULE_TEXT
        for provider_message in model.provider_payloads[-1]
        for block in (
            provider_message.get("content", [])
            if isinstance(provider_message.get("content"), list)
            else []
        )
        if isinstance(block, dict)
    ) == 0


def test_duplicate_active_observation_is_rejected_even_with_new_call_id() -> None:
    model = _FakeLitellmModel([_FakeResponse()])
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-observation-a"))

    with pytest.raises(ValueError, match="observation_id already"):
        boundary.stage(_compilation(model_call_id="call-observation-b"))

    assert boundary.records == (
        _compilation(
            model_call_id="call-observation-a"
        ).delivery_attempt,
    )


def test_retry_is_a_new_immutable_model_call_attempt() -> None:
    model = _FakeLitellmModel(
        [
            FakeProviderAbort("first dispatch failed"),
            _FakeResponse(response_id="resp-success"),
        ]
    )
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)

    boundary.stage(_compilation(model_call_id="call-12a"))
    with pytest.raises(FakeProviderAbort, match="first dispatch failed"):
        model.query(agent.messages)
    first_records = boundary.records
    assert first_records[-1].state is DeliveryState.DISPATCH_FAILED

    boundary.stage(_compilation(model_call_id="call-12b"))
    model.query(agent.messages)

    assert first_records[-1].state is DeliveryState.DISPATCH_FAILED
    assert first_records[-1].model_call_id == "call-12a"
    assert boundary.records[-1].state is DeliveryState.DELIVERED
    assert boundary.records[-1].model_call_id == "call-12b"
    assert all(
        record.model_call_id == "call-12a"
        for record in first_records
    )


@pytest.mark.parametrize("mode", ["no_capsule", "gt_disabled"])
def test_native_provider_payload_is_byte_identical_without_active_capsule(
    mode: str,
) -> None:
    native_messages = [
        {"role": "system", "content": "system"},
        {
            "role": "tool",
            "content": "$ sed -n '1,80p' src/auth/session.py\n",
            "extra": {"native_only": True},
        },
    ]

    control_model = _FakeLitellmModel([_FakeResponse()])
    control_prepared = control_model._prepare_messages_for_api(
        copy.deepcopy(native_messages)
    )

    model = _FakeLitellmModel([_FakeResponse()])
    agent = _agent(model)
    agent.messages = copy.deepcopy(native_messages)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    if mode == "gt_disabled":
        boundary.stage(_compilation(enabled=False))

    model.query(agent.messages)

    assert json.dumps(
        model.provider_payloads[0],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") == json.dumps(
        control_prepared,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert boundary.records == ()
    assert boundary.bound_compilations == ()


def test_no_capsule_preserves_literal_final_litellm_request_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_requests: list[dict[str, Any]] = []

    def fake_completion(**request: Any) -> _FakeResponse:
        captured_requests.append(copy.deepcopy(request))
        return _FakeResponse(response_id=f"resp-{len(captured_requests)}")

    monkeypatch.setattr(
        miniswe_litellm_model.litellm,
        "completion",
        fake_completion,
    )
    native_messages = [
        {"role": "system", "content": "system"},
        {
            "role": "tool",
            "content": "$ sed -n '1,80p' src/auth/session.py\n",
            "extra": {"native_only": True},
        },
    ]

    control = _LiteralRequestLitellmModel()
    control.query(copy.deepcopy(native_messages), timeout=17)

    wrapped = _LiteralRequestLitellmModel()
    agent = _agent(wrapped)  # type: ignore[arg-type]
    agent.messages = copy.deepcopy(native_messages)
    boundary = MiniSweProviderBoundary(model=wrapped, agent=agent)
    wrapped.query(agent.messages, timeout=17)

    assert json.dumps(
        captured_requests[1],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") == json.dumps(
        captured_requests[0],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert boundary.records == ()
    assert boundary.bound_compilations == ()


def test_unwitnessed_overridden_query_fails_closed_and_dispatches_native_only() -> None:
    class UnwitnessedModel(_FakeLitellmModel):
        _gt_exact_provider_payload = None

    model = UnwitnessedModel([_FakeResponse(response_id="resp-native")])
    agent = _agent(model)
    faults: list[tuple[str, str]] = []
    boundary = MiniSweProviderBoundary(
        model=model,
        agent=agent,
        fault_handler=lambda stage, exc: faults.append(
            (stage, type(exc).__name__)
        ),
    )
    boundary.stage(_compilation(model_call_id="call-unwitnessed"))

    model.query(agent.messages)

    assert boundary.records[-1].state is DeliveryState.JOIN_FAILED
    assert "EXACT_PROVIDER_PAYLOAD_UNAVAILABLE" in (
        boundary.records[-1].failure_reason
    )
    assert model.provider_payloads[0][-1] != {
        "role": "user",
        "content": [{"type": "text", "text": CAPSULE_TEXT}],
    }
    assert all(
        CAPSULE_TEXT
        not in json.dumps(message, ensure_ascii=False)
        for message in model.provider_payloads[0]
    )
    assert faults == [("OBSERVATION_JOIN", "RuntimeError")]
    assert boundary._active is None


def test_canonical_bind_journal_failure_is_join_failed_and_native_continues() -> None:
    compilation = _compilation(model_call_id="call-bind-journal")

    class Journal:
        def __init__(self) -> None:
            self.history = [compilation.delivery_attempt]

        def delivery_history(self, _delivery_attempt_id: str):
            return tuple(self.history)

    class Runtime:
        def __init__(self) -> None:
            self.journal = Journal()

        def bind_provider_payload(self, *_args, **_kwargs):
            raise OSError("journal unavailable")

        def record_delivery_failure(self, _attempt_id, state, *, reason):
            failed = __import__(
                "groundtruth.runtime.reasoning_runtime",
                fromlist=["record_delivery_failure"],
            ).record_delivery_failure(
                self.journal.history[-1],
                state,
                reason=reason,
            )
            self.journal.history.append(failed)
            return failed

    runtime = Runtime()
    model = _FakeLitellmModel([_FakeResponse(response_id="resp-native")])
    agent = _agent(model)
    faults: list[str] = []
    boundary = MiniSweProviderBoundary(
        model=model,
        agent=agent,
        attempt_runtime=runtime,  # type: ignore[arg-type]
        fault_handler=lambda stage, _exc: faults.append(stage),
    )
    boundary.stage(
        compilation,
        delivery_attempt_id="delivery:call-bind-journal",
        observation_binding=_stage_binding(compilation),
    )

    model.query(agent.messages)

    assert boundary.records[-1].state is DeliveryState.JOIN_FAILED
    assert faults == ["OBSERVATION_JOIN"]
    assert all(
        CAPSULE_TEXT
        not in json.dumps(message, ensure_ascii=False)
        for message in model.provider_payloads[0]
    )
    assert boundary._active is None


def test_dispatch_journal_failure_does_not_escape_or_send_unwitnessed_capsule() -> None:
    compilation = _compilation(model_call_id="call-dispatch-journal")

    class Journal:
        def __init__(self) -> None:
            self.history = [compilation.delivery_attempt]

        def delivery_history(self, _delivery_attempt_id: str):
            return tuple(self.history)

    class Runtime:
        def __init__(self) -> None:
            self.journal = Journal()

        def bind_provider_payload(self, _attempt_id, payload):
            from groundtruth.runtime.reasoning_runtime import (
                bind_capsule_to_final_payload,
            )

            joined = bind_capsule_to_final_payload(compilation, payload)
            assert joined.delivery_attempt is not None
            self.journal.history.append(joined.delivery_attempt)
            return joined.delivery_attempt

        def mark_dispatched(self, *_args, **_kwargs):
            raise OSError("dispatch journal unavailable")

        def record_delivery_failure(self, *_args, **_kwargs):
            raise ValueError("JOINED cannot persist DISPATCH_FAILED")

    runtime = Runtime()
    model = _FakeLitellmModel([_FakeResponse(response_id="resp-native")])
    agent = _agent(model)
    faults: list[str] = []
    boundary = MiniSweProviderBoundary(
        model=model,
        agent=agent,
        attempt_runtime=runtime,  # type: ignore[arg-type]
        fault_handler=lambda stage, _exc: faults.append(stage),
    )
    boundary.stage(
        compilation,
        delivery_attempt_id="delivery:call-dispatch-journal",
        observation_binding=_stage_binding(compilation),
    )

    model.query(agent.messages)

    assert boundary.records[-1].state is DeliveryState.DISPATCH_FAILED
    assert faults == ["PROVIDER_DISPATCH"]
    assert all(
        CAPSULE_TEXT
        not in json.dumps(message, ensure_ascii=False)
        for message in model.provider_payloads[0]
    )
    assert boundary._active is None


def test_provider_authentication_error_is_rejected_not_transport_failed() -> None:
    class AuthenticationError(RuntimeError):
        status_code = 401

    model = _FakeLitellmModel(
        [AuthenticationError("invalid provider credentials")]
    )
    agent = _agent(model)
    faults: list[str] = []
    boundary = MiniSweProviderBoundary(
        model=model,
        agent=agent,
        fault_handler=lambda stage, _exc: faults.append(stage),
    )
    boundary.stage(_compilation(model_call_id="call-auth-rejected"))

    with pytest.raises(AuthenticationError, match="invalid provider"):
        prepared = model._prepare_messages_for_api(agent.messages)
        model._query(prepared)

    assert boundary.records[-1].state is DeliveryState.PROVIDER_REJECTED
    assert faults == ["PROVIDER_REJECTION"]
    assert boundary._active is None


def test_fault_handler_failure_never_replaces_native_provider_result() -> None:
    class UnwitnessedModel(_FakeLitellmModel):
        _gt_exact_provider_payload = None

    def broken_fault_handler(_stage: str, _exc: BaseException) -> None:
        raise RuntimeError("fault sink unavailable")

    model = UnwitnessedModel([_FakeResponse(response_id="resp-native")])
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(
        model=model,
        agent=agent,
        fault_handler=broken_fault_handler,
    )
    boundary.stage(_compilation(model_call_id="call-fault-handler"))

    response = model.query(agent.messages)

    assert response["extra"]["response"]["id"] == "resp-native"
    assert boundary.records[-1].state is DeliveryState.JOIN_FAILED


def test_join_failed_attempt_can_retry_same_observation_with_new_call() -> None:
    class InitiallyUnwitnessedModel(_FakeLitellmModel):
        _gt_exact_provider_payload = None

    model = InitiallyUnwitnessedModel(
        [
            _FakeResponse(response_id="resp-native"),
            _FakeResponse(response_id="resp-retry"),
        ]
    )
    agent = _agent(model)
    boundary = MiniSweProviderBoundary(model=model, agent=agent)
    boundary.stage(_compilation(model_call_id="call-join-failed"))
    model.query(agent.messages)
    assert boundary.records[-1].state is DeliveryState.JOIN_FAILED

    model._gt_exact_provider_payload = MethodType(  # type: ignore[method-assign]
        lambda _model, messages, kwargs: {
            "messages": messages,
            **kwargs,
        },
        model,
    )
    boundary.stage(_compilation(model_call_id="call-join-retry"))
    model.query(agent.messages)

    assert boundary.records[-1].state is DeliveryState.DELIVERED
    assert boundary.records[-1].model_call_id == "call-join-retry"


def test_commit_journal_fault_does_not_undo_native_trajectory_insertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import groundtruth.runtime.miniswe_provider_boundary as boundary_module

    model = _FakeLitellmModel([_FakeResponse(response_id="resp-commit-fault")])
    agent = _agent(model)
    faults: list[str] = []
    boundary = MiniSweProviderBoundary(
        model=model,
        agent=agent,
        fault_handler=lambda stage, _exc: faults.append(stage),
    )
    boundary.stage(_compilation(model_call_id="call-commit-fault"))
    message = model.query(agent.messages)
    monkeypatch.setattr(
        boundary_module,
        "commit_response",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("commit journal unavailable")
        ),
    )

    result = agent.add_messages(message)

    assert message in result
    assert message in agent.messages
    assert boundary.records[-1].state is DeliveryState.DELIVERED
    assert faults == ["RESPONSE_COMMIT"]


def test_discard_fault_never_masks_original_native_add_failure() -> None:
    model = _FakeLitellmModel([_FakeResponse(response_id="resp-add-fault")])
    agent = _agent(model)
    faults: list[str] = []
    boundary = MiniSweProviderBoundary(
        model=model,
        agent=agent,
        fault_handler=lambda stage, _exc: faults.append(stage),
    )
    boundary.stage(_compilation(model_call_id="call-add-fault"))
    message = model.query(agent.messages)

    boundary._original_add_messages = lambda *_messages: (_ for _ in ()).throw(
        ValueError("native add failed")
    )
    boundary._discard_pending = lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(OSError("discard journal unavailable"))

    with pytest.raises(ValueError, match="native add failed"):
        agent.add_messages(message)

    assert faults == ["RESPONSE_COMMIT"]
