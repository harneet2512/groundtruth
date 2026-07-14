from __future__ import annotations

from groundtruth.runtime import gateway


def _make(*, cap_feature_ids=()):
    return gateway._mk_add(
        gateway.GatewayState(),
        gateway.ToolEvent(kind="edit", action_index=4),
        fact_kind="signature_mismatch",
        target="pkg/api.py",
        body_lines=["signature mismatch"],
        evidence=[],
        tier=gateway.INFO,
        producer="patch_delta",
        symbol="parse",
        cap_feature_ids=cap_feature_ids,
    )


def test_gateway_mk_add_attaches_canonical_fact_lineage() -> None:
    env = _make()
    assert env.lineage is not None
    assert env.lineage.fact_class == "signature_delta"
    assert env.lineage.runtime_producer_id == "patch_delta"
    assert [(ref.category, ref.feature_id, ref.role) for ref in env.lineage.features] == [
        ("FACT", "signature_delta", "fact")
    ]


def test_gateway_cap_owner_is_explicit_not_inferred_from_producer() -> None:
    plain = _make()
    owned = _make(cap_feature_ids=("GT_PATCH_DELTA",))
    assert plain.lineage is not None
    assert owned.lineage is not None
    assert all(ref.category != "CAP" for ref in plain.lineage.features)
    assert ("CAP", "GT_PATCH_DELTA", "byte_owner") in {
        (ref.category, ref.feature_id, ref.role) for ref in owned.lineage.features
    }
    assert plain.dedup_key == owned.dedup_key
