"""RED contract for the sole model-facing adherence observation compiler.

The compiler emits at most one decision-complete capsule for one fresh model
call.  Compilation, exact provider-payload joining, and provider-terminal
delivery are intentionally separate boundaries.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from groundtruth.runtime.reasoning_runtime import (
    CapsuleCompilationState,
    DecisionContext,
    DeliveryState,
    EvidenceGrade,
    EvidenceRef,
    EvidenceRole,
    OracleDecision,
    bind_capsule_to_final_payload,
    capsule_budget_for,
    compile_observation_capsule,
    verify_bound_payload_at_dispatch,
)


NATIVE_OBSERVATION = "$ sed -n '1,120p' src/auth/session.py\nclass Session:\n"


def _item(
    evidence_id: str,
    role: EvidenceRole,
    grade: EvidenceGrade,
    *,
    context: DecisionContext = DecisionContext.PATCH_CONSTRUCTION,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        feature_id=f"internal-producer-{evidence_id}",
        decision_context=context,
        roles=(role,),
        claim=f"claim for {evidence_id}",
        actionable_consequence=f"act on {evidence_id}",
        provenance=(f"src/auth/session.py::{evidence_id}",),
        grade=grade,
    )


def _decision(
    *items: EvidenceRef,
    context: DecisionContext = DecisionContext.PATCH_CONSTRUCTION,
    total_tokens: int = 80,
    overall_grade: EvidenceGrade = EvidenceGrade.WARNING,
) -> OracleDecision:
    coverage = tuple(role for item in items for role in item.roles)
    return OracleDecision(
        decision_context=context,
        primary_claim=(
            "repair refreshSession without changing its caller-visible contract"
        ),
        coalition=tuple(items),
        mandatory_items=(),
        suppressed=(),
        total_tokens=total_tokens,
        coverage=coverage,
        unresolved_roles=(),
        overall_grade=overall_grade,
        decision_complete=True,
        release_allowed=True,
        over_budget=False,
    )


def _compile(
    decision: OracleDecision,
    **overrides: object,
):
    arguments: dict[str, object] = {
        "native_observation": NATIVE_OBSERVATION,
        "decision": decision,
        "observation_id": "obs-205",
        "source_model_call_id": "call-12",
        "model_call_id": "call-13",
        "enabled": True,
    }
    arguments.update(overrides)
    return compile_observation_capsule(**arguments)


def _payload(capsule_text: str) -> dict[str, object]:
    return {
        "model": "provider/model",
        "messages": [
            {
                "role": "tool",
                "content": [{"type": "text", "text": NATIVE_OBSERVATION}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Continue from the observation."},
                    {"type": "text", "text": capsule_text},
                ],
            },
        ],
    }


def _payload_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_one_decision_coalition_renders_as_one_native_structured_capsule() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )
    contract = _item(
        "contract",
        EvidenceRole.BEHAVIORAL_CONTRACT,
        EvidenceGrade.WARNING,
    )

    compiled = _compile(_decision(target, contract))

    assert compiled.state is CapsuleCompilationState.COMPILED
    assert compiled.native_observation == NATIVE_OBSERVATION
    assert compiled.capsule_text.count("Decision\n") == 1
    assert compiled.capsule_text.count("Evidence\n") == 1
    assert "repair refreshSession" in compiled.capsule_text
    assert "claim for target" in compiled.capsule_text
    assert "act on target" in compiled.capsule_text
    assert "claim for contract" in compiled.capsule_text
    assert "act on contract" in compiled.capsule_text

    # Internal producer identities and legacy feature tags never enter the
    # model-facing capsule.
    assert "internal-producer-" not in compiled.capsule_text
    assert "<gt-" not in compiled.capsule_text.lower()
    assert compiled.capsule_text.count("claim for target") == 1
    assert compiled.capsule_text.count("claim for contract") == 1
    assert compiled.delivery_attempt.state is DeliveryState.COMPILED


def test_compiler_rejects_a_coalition_that_mixes_decision_contexts() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )
    completion = _item(
        "completion",
        EvidenceRole.TERMINAL_ASSURANCE,
        EvidenceGrade.VERIFIED,
        context=DecisionContext.COMPLETION,
    )

    compiled = _compile(_decision(target, completion))

    assert compiled.state is CapsuleCompilationState.FAILED
    assert compiled.native_observation == NATIVE_OBSERVATION
    assert compiled.capsule_text == ""
    assert compiled.delivery_attempt is None


def test_item_grades_and_provenance_survive_without_blanket_upgrade() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )
    contract = _item(
        "contract",
        EvidenceRole.BEHAVIORAL_CONTRACT,
        EvidenceGrade.WARNING,
    )

    compiled = _compile(
        _decision(
            target,
            contract,
            overall_grade=EvidenceGrade.WARNING,
        )
    )

    assert "[VERIFIED] claim for target" in compiled.capsule_text
    assert "[WARNING] claim for contract" in compiled.capsule_text
    assert "src/auth/session.py::target" in compiled.capsule_text
    assert "src/auth/session.py::contract" in compiled.capsule_text
    assert compiled.overall_grade is EvidenceGrade.WARNING
    assert "[VERIFIED] Decision" not in compiled.capsule_text
    assert compiled.rendered_content_hash == hashlib.sha256(
        compiled.capsule_text.encode("utf-8")
    ).hexdigest()
    assert compiled.capsule_hash != compiled.rendered_content_hash


def test_gt_off_is_byte_identical_and_creates_no_delivery_attempt() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )

    compiled = _compile(_decision(target), enabled=False)

    assert compiled.state is CapsuleCompilationState.DISABLED
    assert compiled.native_observation.encode("utf-8") == NATIVE_OBSERVATION.encode(
        "utf-8"
    )
    assert compiled.capsule_text == ""
    assert compiled.capsule_hash == ""
    assert compiled.delivery_attempt is None


def test_render_failure_preserves_native_observation_unchanged() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )

    def broken_renderer(_decision: OracleDecision) -> str:
        raise RuntimeError("renderer exploded")

    compiled = _compile(_decision(target), renderer=broken_renderer)

    assert compiled.state is CapsuleCompilationState.FAILED
    assert compiled.native_observation.encode("utf-8") == NATIVE_OBSERVATION.encode(
        "utf-8"
    )
    assert compiled.capsule_text == ""
    assert compiled.delivery_attempt is None
    assert compiled.failure_code == "RENDERING_FAILED"


def test_compiler_requires_a_fresh_model_call_after_capsule_creation() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )

    compiled = _compile(
        _decision(target),
        source_model_call_id="call-12",
        model_call_id="call-12",
    )

    assert compiled.state is CapsuleCompilationState.FAILED
    assert compiled.capsule_text == ""
    assert compiled.delivery_attempt is None
    assert compiled.failure_code == "FRESH_MODEL_CALL_REQUIRED"


def test_decision_specific_hard_budget_is_enforced_before_compilation() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )
    patch_budget = capsule_budget_for(DecisionContext.PATCH_CONSTRUCTION)
    completion_budget = capsule_budget_for(DecisionContext.COMPLETION)

    assert patch_budget.hard_max_tokens > completion_budget.hard_max_tokens
    over_budget = _decision(
        target,
        total_tokens=patch_budget.hard_max_tokens + 1,
    )

    compiled = _compile(over_budget)

    assert compiled.state is CapsuleCompilationState.FAILED
    assert compiled.capsule_text == ""
    assert compiled.delivery_attempt is None
    assert compiled.failure_code == "CAPSULE_BUDGET_EXCEEDED"


def test_exact_final_payload_identity_creates_structural_binding_and_joined_state() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )
    compiled = _compile(_decision(target))
    final_payload = _payload(compiled.capsule_text)

    joined = bind_capsule_to_final_payload(compiled, final_payload)

    assert joined.delivery_attempt.state is DeliveryState.JOINED
    assert joined.binding.schema == "gt.capsule_binding.v1"
    assert joined.binding.model_call_id == "call-13"
    assert joined.binding.observation_id == "obs-205"
    assert joined.binding.decision_context is DecisionContext.PATCH_CONSTRUCTION
    assert joined.binding.evidence_ids == ("target",)
    assert joined.binding.capsule_hash == compiled.capsule_hash
    assert joined.binding.provider_payload_hash == _payload_hash(final_payload)
    assert joined.binding.message_index == 1
    assert joined.binding.content_index == 1

    # Joining creates a successor; compilation itself remains only COMPILED.
    assert compiled.delivery_attempt.state is DeliveryState.COMPILED


def test_substring_or_wrong_capsule_never_advances_to_joined() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )
    compiled = _compile(_decision(target))
    payload = _payload(f"prefix {compiled.capsule_text} suffix")

    with pytest.raises(ValueError, match="exact|capsule"):
        bind_capsule_to_final_payload(compiled, payload)

    assert compiled.delivery_attempt.state is DeliveryState.COMPILED


def test_ambiguous_duplicate_capsule_blocks_join_proof() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )
    compiled = _compile(_decision(target))
    payload = _payload(compiled.capsule_text)
    messages = payload["messages"]
    assert isinstance(messages, list)
    messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": compiled.capsule_text}],
        }
    )

    with pytest.raises(ValueError, match="multiple|ambiguous"):
        bind_capsule_to_final_payload(compiled, payload)

    assert compiled.delivery_attempt.state is DeliveryState.COMPILED


def test_actual_rendered_capsule_not_estimate_controls_hard_budget() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )

    def oversized(_decision: OracleDecision) -> str:
        return (
            "[GroundTruth · PATCH CONSTRUCTION]\n\n"
            "Decision\n"
            "repair refreshSession without changing its caller-visible contract\n\n"
            "Evidence\n"
            "• [VERIFIED] claim for target\n"
            "  Action: act on target\n"
            "  Source: src/auth/session.py::target\n"
            + ("high-signal-token " * 2000)
        )

    compiled = _compile(_decision(target, total_tokens=1), renderer=oversized)

    assert compiled.state is CapsuleCompilationState.FAILED
    assert compiled.failure_code == "CAPSULE_BUDGET_EXCEEDED"
    assert compiled.native_observation == NATIVE_OBSERVATION


def test_custom_renderer_cannot_reintroduce_internal_tags_or_producer_ids() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )
    for unsafe in (
        "<gt-localization>unsafe</gt-localization>",
        "internal-producer-target",
        "   ",
    ):
        compiled = _compile(
            _decision(target),
            renderer=lambda _decision, value=unsafe: value,
        )
        assert compiled.state is CapsuleCompilationState.FAILED
        assert compiled.capsule_text == ""


def test_join_freezes_exact_payload_and_dispatch_rejects_later_mutation() -> None:
    target = _item(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        EvidenceGrade.VERIFIED,
    )
    compiled = _compile(_decision(target))
    payload = _payload(compiled.capsule_text)
    joined = bind_capsule_to_final_payload(compiled, payload)
    frozen_payload = joined.bound_provider_payload_json

    messages = payload["messages"]
    assert isinstance(messages, list)
    messages.append({"role": "user", "content": "late mutation"})

    assert joined.bound_provider_payload_json == frozen_payload
    with pytest.raises(ValueError, match="payload|mutat|hash"):
        verify_bound_payload_at_dispatch(joined, payload)
    assert joined.delivery_attempt.state is DeliveryState.JOINED
