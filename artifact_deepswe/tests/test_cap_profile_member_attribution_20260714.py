"""CAP producer attribution: exact sealed payload -> one owning Profile-2 member.

The runtime ledger may name ``profile_member`` only when the delivered bytes have a
single producer member.  Shared lane facts, advisory/kernel mediators, and a member
that is not actually active must remain unattributed.
"""
from __future__ import annotations

import gt_mini_patch as g
from groundtruth.runtime import gateway as gw
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope


class Submitted(Exception):
    pass


def _envelope(*, producer: str, evidence_type: str) -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer=producer,
        fact_id="symbol",
        target="src/example.py",
        evidence_type=evidence_type,
        payload=("exact payload",),
        provenance=(("src/example.py", 7),),
        confidence=0.9,
        tier="verified",
        graph_revision="rev",
        valid_until="rev",
        preferred_event="edit",
        blocking_eligibility="advisory",
        estimated_cost_tokens=4,
        measured=False,
    )


def test_lane_owner_requires_exact_kind_and_active_member(monkeypatch):
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    assert g._lane_profile_member_extra("edit.syntax") == {
        "profile_member": "GT_EDIT_CHECK"
    }

    monkeypatch.setenv("GT_EDIT_CHECK", "0")
    assert g._lane_profile_member_extra("edit.syntax") is None

    # Shared/class facts and kernel mediators never inherit attribution by proximity.
    monkeypatch.setenv("GT_GATEWAY", "1")
    assert g._lane_profile_member_extra("l3.contract") is None
    assert g._lane_profile_member_extra("detect.coherence") is None


def test_lane_owner_allowlist_covers_only_single_owner_payloads(monkeypatch):
    expected = {
        "edit.syntax": "GT_EDIT_CHECK",
        "recovery": "GT_HYPOTHESIS",
    }
    for member in expected.values():
        monkeypatch.setenv(member, "1")
    assert {
        kind: g._lane_profile_member_extra(kind)["profile_member"]
        for kind in expected
    } == expected

    # The plan and independent covering producers share this kind. Even with both
    # members active, the delivery row cannot infer which engine owned the bytes.
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    assert g._lane_profile_member_extra("verify.horizon.executed") is None


def test_plan_shaped_executed_delivery_is_not_falsely_attributed(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kw: calls.append(kw))
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_last_gate_winner_kind", "verify.horizon.executed")
    monkeypatch.setattr(g, "_last_gate_winner_hash", "")
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setenv("GT_LANE_ENVELOPE", "0")

    payload = "$ python -m py_compile src/example.py\nSyntaxError\n[exit 1]"
    out = {"output": "edit result"}
    g._deliver_gate_winner(
        out, "edit", payload, kkind="", kf="", krel="src/example.py",
        event=None, steer_base="edit result",
    )

    delivered = [row for row in calls if row.get("content") == payload]
    assert len(delivered) == 1
    assert delivered[0].get("extra") is None


def test_gateway_owner_requires_exact_producer_class_pair_and_active_member(monkeypatch):
    monkeypatch.setenv("GT_PATCH_DELTA", "1")
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    assert g._gateway_profile_member_extra(
        _envelope(producer="patch_delta", evidence_type="signature_mismatch")
    ) == {"profile_member": "GT_PATCH_DELTA"}
    assert g._gateway_profile_member_extra(
        _envelope(producer="patch_delta", evidence_type="companion_surface")
    ) == {"profile_member": "GT_PATCH_DELTA"}
    assert g._gateway_profile_member_extra(
        _envelope(producer="change_surface", evidence_type="new_file_destination")
    ) == {"profile_member": "GT_CHANGE_SURFACE"}
    assert g._gateway_profile_member_extra(
        _envelope(producer="change_surface", evidence_type="missing_role:registry")
    ) == {"profile_member": "GT_CHANGE_SURFACE"}

    # A producer label alone is insufficient: an unexpected class fails closed.
    assert g._gateway_profile_member_extra(
        _envelope(producer="patch_delta", evidence_type="caller_break")
    ) is None
    assert g._gateway_profile_member_extra(
        _envelope(producer="caller_contract", evidence_type="caller_break")
    ) is None

    monkeypatch.setenv("GT_PATCH_DELTA", "0")
    assert g._gateway_profile_member_extra(
        _envelope(producer="patch_delta", evidence_type="signature_mismatch")
    ) is None


def test_gate_winner_threads_owner_only_to_exact_sealed_delivery(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kw: calls.append(kw))
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_last_gate_winner_kind", "edit.syntax")
    monkeypatch.setattr(g, "_last_gate_winner_hash", "")
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.setenv("GT_LANE_ENVELOPE", "0")

    payload = "src/example.py:7: invalid syntax"
    out = {"output": "tool output"}
    g._deliver_gate_winner(
        out, "edit", payload, kkind="", kf="", krel="src/example.py",
        event=None, steer_base="tool output",
    )

    delivered = [row for row in calls if row.get("content") == payload]
    assert len(delivered) == 1
    assert delivered[0]["extra"] == {"profile_member": "GT_EDIT_CHECK"}
    assert out["output"].endswith(payload)


def test_gateway_commit_threads_exact_envelope_owner(monkeypatch):
    calls: list[dict] = []
    winner = _envelope(
        producer="patch_delta", evidence_type="signature_mismatch"
    )
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kw: calls.append(kw))
    monkeypatch.setattr(gw, "augment", lambda event, state: [winner])
    monkeypatch.setattr(g, "_db_path", lambda: "")
    monkeypatch.setattr(g, "_root", lambda: "")
    monkeypatch.setattr(g, "_issue_text", lambda: "")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_PATCH_DELTA", "1")
    monkeypatch.setenv("GT_GATEWAY_NATIVE", "0")
    g._reset_oracle_state()

    out = {"output": "edit result", "returncode": 0}
    g._gt_gateway_deliver(
        {"command": "edit src/example.py"}, out,
        "edit src/example.py", "edit result",
    )

    delivered = [row for row in calls if row.get("content")]
    assert len(delivered) == 1
    assert delivered[0]["extra"] == {"profile_member": "GT_PATCH_DELTA"}
    assert delivered[0]["content"] in out["output"]


def test_completion_delivery_attributes_render_owner_not_telemetry_builder(
        tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kw: calls.append(kw))
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        g, "_gt_submit_hygiene",
        lambda root: {"blocking": True, "reason": "hygiene", "detail": "dirty diff"},
    )
    monkeypatch.setattr(g, "_gt_submit_covering", lambda root: (None, []))
    monkeypatch.setattr(g, "_build_env_executor", lambda: None)
    monkeypatch.setattr(g, "_gt_submit_bounce_count", 0, raising=False)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setenv("GT_COMPLETION_CERT", "1")
    monkeypatch.setenv("GT_CERT_DELIVERY", "1")
    g._oracle_delivered_hashes.clear()

    out = g._gt_gate_submit_exception(object(), "submit", Submitted())

    assert out is not None and out["returncode"] == 1
    delivered = [row for row in calls if row.get("content") == out["output"]]
    assert len(delivered) == 1
    assert delivered[0]["extra"] == {"profile_member": "GT_CERT_DELIVERY"}
    assert all(
        row.get("profile_member") != "GT_COMPLETION_CERT" for row in calls
    ), "the host-only certificate builder must not receive model-byte credit"


def test_profile_attribution_does_not_change_delivery_bytes(monkeypatch):
    payload = "same rendered bytes"
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kw: None)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_last_gate_winner_kind", "recovery")
    monkeypatch.setattr(g, "_last_gate_winner_hash", "")
    monkeypatch.setenv("GT_LANE_ENVELOPE", "0")

    def deliver() -> bytes:
        out = {"output": "base"}
        g._deliver_gate_winner(
            out, "pytest", payload, kkind="", kf="", krel="src/example.py",
            event=None, steer_base="base",
        )
        return out["output"].encode("utf-8")

    monkeypatch.setenv("GT_HYPOTHESIS", "0")
    off = deliver()
    assert g._lane_profile_member_extra("recovery") is None
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    on = deliver()
    assert g._lane_profile_member_extra("recovery") == {
        "profile_member": "GT_HYPOTHESIS"
    }
    assert on == off == b"base\nsame rendered bytes"
