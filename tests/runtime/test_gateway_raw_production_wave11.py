from __future__ import annotations

from groundtruth.runtime import gateway
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope


def _candidate() -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="def_ref_partition",
        fact_id="Widget",
        target="src/widget.py",
        evidence_type="def_ref_partition",
        payload=("src/widget.py:4:Widget",),
        provenance=(("src/widget.py", 4),),
        confidence=0.9,
        tier="VERIFIED",
        preferred_event="search",
    )


def test_raw_production_bypasses_all_legacy_delivery_selection(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    candidate = _candidate()
    state = gateway.GatewayState()
    state.delivered_keys.add(candidate.dedup_key)
    monkeypatch.setattr(
        gateway, "classify_outcome",
        lambda event, current_state: gateway.AMBIGUOUS_HIT,
    )
    monkeypatch.setattr(
        gateway, "_produce_def_ref_partition",
        lambda event, current_state: [candidate],
    )
    monkeypatch.setattr(
        gateway, "route_delivery",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("raw production invoked legacy route_delivery")
        ),
    )
    monkeypatch.setattr(
        gateway, "_apply_xsession_policy",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("raw production invoked legacy suppression")
        ),
    )
    monkeypatch.setattr(
        gateway, "_apply_xsession_rankup",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("raw production invoked legacy rank-up")
        ),
    )

    result = gateway.produce_raw(
        gateway.ToolEvent(
            kind=gateway.KIND_SEARCH,
            command="rg Widget",
            output="src/widget.py:4:Widget",
        ),
        state,
    )

    assert result == [candidate]
    assert state.delivered_keys == {candidate.dedup_key}


def test_raw_production_preserves_distinct_complementary_candidates(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    first = _candidate()
    second = EvidenceEnvelope.build(
        producer="caller_contract",
        fact_id="Widget",
        target="src/caller.py",
        evidence_type="caller_break",
        payload=("src/caller.py:8:build_widget",),
        provenance=(("src/caller.py", 8),),
        confidence=1.0,
        tier="VERIFIED",
        preferred_event="edit",
    )
    monkeypatch.setattr(
        gateway, "classify_outcome",
        lambda event, current_state: gateway.SATISFIED,
    )
    monkeypatch.setattr(
        gateway, "_produce_patch_delta",
        lambda event, current_state: [first],
    )
    monkeypatch.setattr(
        gateway, "_produce_caller_contract",
        lambda event, current_state: [second],
    )
    monkeypatch.setenv("GT_PATCH_DELTA", "1")

    result = gateway.produce_raw(
        gateway.ToolEvent(
            kind=gateway.KIND_EDIT,
            changed_files=("src/widget.py",),
            edit_before_after={"src/widget.py": ("before", "after")},
        ),
        gateway.GatewayState(),
    )

    assert result == [first, second]


def test_raw_production_master_flag_off_is_quiet_and_state_preserving(
    monkeypatch,
) -> None:
    monkeypatch.delenv("GT_GATEWAY", raising=False)
    state = gateway.GatewayState()
    event = gateway.ToolEvent(
        kind=gateway.KIND_EDIT,
        changed_files=("src/widget.py",),
        edit_before_after={"src/widget.py": ("before", "after")},
    )

    assert gateway.produce_raw(event, state) == []
    assert event.semantic_events == ()
    assert state.edit_events == []


def test_legacy_augment_still_applies_delivery_dedup_after_raw_production(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    candidate = _candidate()
    state = gateway.GatewayState()
    state.delivered_keys.add(candidate.dedup_key)
    monkeypatch.setattr(
        gateway, "classify_outcome",
        lambda event, current_state: gateway.AMBIGUOUS_HIT,
    )
    monkeypatch.setattr(
        gateway, "_produce_def_ref_partition",
        lambda event, current_state: [candidate],
    )

    assert gateway.augment(
        gateway.ToolEvent(
            kind=gateway.KIND_SEARCH,
            command="rg Widget",
            output="src/widget.py:4:Widget",
        ),
        state,
    ) == []
