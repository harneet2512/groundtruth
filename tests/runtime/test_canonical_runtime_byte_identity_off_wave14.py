from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import evidence_envelope as ee
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.canonical_producers import (
    ProducerContext,
    produce_syntax_result,
)
from groundtruth.runtime.commitment_control import CommitmentDecision


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class _Model:
    def __init__(self) -> None:
        self.provider_payloads: list[dict[str, object]] = []

    def _prepare_messages_for_api(self, messages):
        return copy.deepcopy(messages)

    def _query(self, messages, **kwargs):
        self.provider_payloads.append(
            {
                "messages": copy.deepcopy(messages),
                "kwargs": copy.deepcopy(kwargs),
            }
        )
        return SimpleNamespace(
            id="native-response",
            status="completed",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(refusal=None),
                )
            ],
        )

    def _gt_exact_provider_payload(self, messages, kwargs):
        return {"messages": messages, **kwargs}


class _Agent:
    def __init__(self) -> None:
        self.executed = 0

    def add_messages(self, *messages):
        return list(messages)

    def execute_actions(self, message):
        self.executed += 1
        return [
            {
                "output": "native submit executed",
                "returncode": 0,
                "native": {"preserved": True},
            }
        ]


def test_installed_canonical_runtime_is_byte_identical_with_attachments_off(
    tmp_path,
    monkeypatch,
) -> None:
    for flag in (
        "GT_EDIT_CHECK",
        "GT_VERIFY_EXECUTE",
        "GT_HYPOTHESIS",
        "GT_SS_SUBMIT_RED",
        "GT_CERT_DELIVERY",
        "GT_CS_EDIT_TRIGGER",
    ):
        monkeypatch.setenv(flag, "0")
    monkeypatch.delenv("GT_RL_PROFILE", raising=False)

    native_messages = [
        {"role": "system", "content": "native system"},
        {
            "role": "tool",
            "content": "$ sed -n '1,80p' src/session.py\n",
            "extra": {"native_only": True},
        },
    ]
    submit_message = {
        "extra": {
            "response": {"id": "native-submit-proposal"},
            "actions": [{"operation": "SUBMIT", "command": "submit"}],
        }
    }

    control_model = _Model()
    control_agent = _Agent()
    control_result = control_agent.execute_actions(
        copy.deepcopy(submit_message)
    )
    control_prepared = control_model._prepare_messages_for_api(
        copy.deepcopy(native_messages)
    )
    control_model._query(
        control_prepared,
        temperature=0,
        response_format={"type": "json_object"},
    )

    model = _Model()
    agent = _Agent()
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        seam,
        "_db_path",
        lambda: str(tmp_path / "graph.db"),
    )
    attachment = seam.install_canonical_runtime(
        model=model,
        agent=agent,
        env={
            "GT_ATTEMPT_ID": "attempt-byte-identity-off",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_BRIEF_FILE": str(tmp_path / "absent.txt"),
        },
        task="native task without canonical attachments",
    )
    assert attachment.attached is True
    assert attachment.provider_boundary._active is None

    installed_result = agent.execute_actions(copy.deepcopy(submit_message))
    installed_prepared = model._prepare_messages_for_api(
        copy.deepcopy(native_messages)
    )
    model._query(
        installed_prepared,
        temperature=0,
        response_format={"type": "json_object"},
    )

    assert _canonical_bytes(installed_result) == _canonical_bytes(
        control_result
    )
    assert _canonical_bytes(installed_prepared) == _canonical_bytes(
        control_prepared
    )
    assert _canonical_bytes(model.provider_payloads) == _canonical_bytes(
        control_model.provider_payloads
    )
    assert attachment.attempt_runtime._evidence == {}
    assert attachment.provider_boundary.records == ()
    assert attachment.provider_boundary.bound_compilations == ()
    assert attachment.commitment_boundary.plans[-1].decision is (
        CommitmentDecision.BLOCK_CERTIFICATE
    )
    attachment.attempt_runtime.journal.close()


def test_runtime_witness_sidecar_never_reaches_compiled_capsule() -> None:
    revision = rr.RevisionVector(
        repository_content="repo-wave14",
        graph="graph-wave14",
        lsp="lsp-wave14",
        runtime_evidence="runtime-wave14",
    )
    event_id = "ev-wave14-internal-syntax"
    event_hash = hashlib.sha256(b"canonical validation event").hexdigest()
    witness = ee.CanonicalRuntimeWitness.canonical_event(
        event_id=event_id,
        content_sha256=event_hash,
    )
    envelope = produce_syntax_result(
        context=ProducerContext(
            subject="src/session.py",
            provenance=(),
            revision=revision,
            decision_id="PATCH_PROPAGATION:wave14",
            causal_neighborhood=(
                "subject:src/session.py",
                "decision:PATCH_PROPAGATION",
            ),
            runtime_witnesses=(witness,),
        ),
        result={
            "verdict": "syntax_error",
            "reason": "parse_error",
            "language": ".py",
            "checker": ["ast.parse"],
            "diagnostic": "SyntaxError: '(' was never closed",
        },
    )
    assert envelope is not None
    serialized = _canonical_bytes(ee.to_dict(envelope))
    committed_render = ee.render_bytes(envelope)
    assert event_id.encode() not in serialized
    assert event_hash.encode() not in serialized
    assert event_id.encode() not in committed_render
    assert event_hash.encode() not in committed_render

    record = rr.canonical_evidence_from_envelope(
        envelope,
        committed_event_hashes={event_id: event_hash},
    )
    assert record is not None
    record = replace(record, lifecycle=rr.EvidenceLifecycle.READY)
    decision = rr.ActiveDecision(
        decision_id="PATCH_PROPAGATION:wave14",
        context=rr.DecisionContext.PATCH_PROPAGATION,
        primary_claim="Do not continue from a structurally invalid edit.",
        required_roles=record.roles,
        causal_neighborhood=("subject:src/session.py",),
        token_budget=280,
        current_revision=revision,
    )
    oracle = rr.select_evidence_coalition(decision, (record,))
    native_observation = "$ python -m compileall src/session.py\n"
    disabled = rr.compile_observation_capsule(
        decision=oracle,
        native_observation=native_observation,
        observation_id="obs-wave14-disabled",
        source_model_call_id="model-before-wave14-disabled",
        model_call_id="model-after-wave14-disabled",
        enabled=False,
        token_counter=lambda _text: 64,
    )
    assert disabled.state is rr.CapsuleCompilationState.DISABLED
    assert disabled.native_observation.encode() == native_observation.encode()
    assert disabled.capsule_text == ""

    compilation = rr.compile_observation_capsule(
        decision=oracle,
        native_observation=native_observation,
        observation_id="obs-wave14",
        source_model_call_id="model-before-wave14",
        model_call_id="model-after-wave14",
        enabled=True,
        token_counter=lambda _text: 64,
    )

    assert compilation.state is rr.CapsuleCompilationState.COMPILED
    assert event_id not in compilation.capsule_text
    assert event_hash not in compilation.capsule_text
