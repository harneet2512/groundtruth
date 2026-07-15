from __future__ import annotations

from groundtruth.pretask import v1r_brief
from groundtruth.runtime import episode_state, gateway
from groundtruth.runtime.control_participation import (
    build_control_participation,
    participation_to_dict,
)
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
from groundtruth.runtime.evidence_envelope import render_bytes as envelope_candidate_bytes


def _env(evidence_type: str = "def_ref_partition") -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="def_ref_partition",
        fact_id="Widget",
        target="src/widget.py",
        evidence_type=evidence_type,
        payload=("src/widget.py:4:Widget",),
        provenance=(("src/widget.py", 4),),
        confidence=0.9,
        tier="VERIFIED",
        preferred_event="search",
    )


def _state(rows: list[dict], **kwargs) -> gateway.GatewayState:
    def recorder(feature_id, decision_site, decision, **extra):
        rows.append(participation_to_dict(build_control_participation(
            feature_id=feature_id,
            decision_site=decision_site,
            decision=decision,
            iteration=0,
            candidate_bytes=extra.get("candidate_bytes", ""),
            fact_class=extra.get("fact_class"),
            candidate_id=extra.get("candidate_id", ""),
            reason=extra.get("reason", ""),
        )))

    return gateway.GatewayState(control_recorder=recorder, **kwargs)


def test_edit_overlay_owner_records_terminal_stale_drop(monkeypatch) -> None:
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    rows: list[dict] = []
    state = _state(rows, episode_overlay={
        "src/widget.py": {"changed": True},
    })
    verdict = gateway.route_delivery(
        _env(), gateway.ToolEvent(kind="search"), state)
    assert verdict == gateway.ROUTE_STALE
    row = rows[0]
    assert row["control_ref"]["feature_id"] == "GT_EDIT_OVERLAY"
    assert row["decision_site"] == "gateway.route_delivery.episode_overlay"
    assert row["decision"] == "APPLIED"


def test_registry_timing_owner_records_declared_boundary(monkeypatch) -> None:
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    rows: list[dict] = []
    state = _state(rows)
    env = _env("covering_verdict")
    verdict = gateway.route_delivery(env, gateway.ToolEvent(kind="search"), state)
    assert verdict == gateway.ROUTE_DEFER
    row = rows[0]
    assert row["decision_site"] == "gateway.route_delivery.registry_timing"
    assert row["decision"] == "APPLIED"
    assert row["reason"] == "too_early"


def test_registry_renderer_owner_records_candidate_check(monkeypatch) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    monkeypatch.setattr(gateway, "classify_outcome", lambda event, state: gateway.AMBIGUOUS_HIT)
    monkeypatch.setattr(gateway, "_produce_def_ref_partition", lambda event, state: [_env()])
    rows: list[dict] = []
    gateway.augment(gateway.ToolEvent(kind="search"), _state(rows))
    row = next(r for r in rows if r["decision_site"] ==
               "gateway.augment.renderer_enforcement")
    assert row["decision"] == "NO_EFFECT"
    assert row["reason"] == "renderer_available"
    gateway_row = next(r for r in rows if r["control_ref"]["feature_id"] ==
                       "GT_GATEWAY")
    assert gateway_row["decision_site"] == "gateway.augment.candidate_admission"
    assert gateway_row["decision"] == "APPLIED"
    assert gateway_row["fact_class"] == "def_partition"
    assert gateway_row["candidate_id"] == _env().dedup_key
    assert gateway_row["candidate_chars"] == len(
        envelope_candidate_bytes(_env()).decode("utf-8"))


def test_gateway_edit_bridge_records_actual_admitted_candidate(monkeypatch) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    candidate = _env("signature_mismatch")
    monkeypatch.setattr(gateway, "_produce_patch_delta", lambda event, state: [candidate])
    monkeypatch.setattr(gateway, "_produce_caller_contract", lambda event, state: [])
    monkeypatch.setattr(gateway, "route_delivery", lambda env, event, state: gateway.ROUTE_DELIVER)
    rows: list[dict] = []
    event = gateway.ToolEvent(
        kind="edit",
        edit_before_after={"src/widget.py": ("old", "new")},
    )
    assert gateway.augment(event, _state(rows)) == [candidate]
    row = next(r for r in rows if r["control_ref"]["feature_id"] ==
               "GT_GATEWAY_EDIT_BRIDGES")
    assert row["decision"] == "APPLIED"
    assert row["fact_class"] == "signature_delta"
    assert row["candidate_id"] == candidate.dedup_key
    assert row["candidate_sha256_16"]


def test_xsession_owner_records_inert_suppression(monkeypatch) -> None:
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    rows: list[dict] = []
    state = _state(
        rows,
        episode=episode_state.EpisodeState(
            xsession_policy={"caller_contract": (5, 0)}),
    )
    assert gateway._apply_xsession_policy([_env("caller_break")], state) == []
    row = rows[0]
    assert row["decision_site"] == "gateway.xsession_policy.inert_suppression"
    assert row["decision"] == "APPLIED"


def test_xsession_rankup_records_post_control_candidate_and_fact(monkeypatch) -> None:
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    rows: list[dict] = []
    state = _state(
        rows,
        episode=episode_state.EpisodeState(
            xsession_policy={"caller_contract": (5, 2)}),
    )
    original = _env("caller_break")
    result = gateway._apply_xsession_rankup([original], state)
    assert result[0].native_args["_xsession_boost"] > 0
    row = rows[0]
    assert row["control_ref"]["feature_id"] == "GT_XSESSION_RANKUP"
    assert row["decision_site"] == "gateway.xsession_rankup.boost"
    assert row["decision"] == "APPLIED"
    assert row["fact_class"] == "caller_contract"
    assert row["candidate_id"] == original.dedup_key
    assert row["candidate_chars"] > 0


def test_brief_minimal_owner_uses_same_typed_schema_and_preserves_bytes(monkeypatch) -> None:
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    before = "<gt-task-brief>\n<gt-graph-map>\nheavy\n</gt-graph-map>\n</gt-task-brief>"
    after = v1r_brief._reduce_brief_to_minimal(before)
    rows = v1r_brief._brief_minimal_participation(before, after)
    assert after == v1r_brief._reduce_brief_to_minimal(before)
    assert rows[0]["control_ref"] == {
        "category": "CAP", "feature_id": "GT_BRIEF_MINIMAL",
        "role": "eligibility",
    }
    assert rows[0]["decision_site"] == "pretask.v1r_brief.minimal_reduction"
    assert rows[0]["decision"] == "APPLIED"
