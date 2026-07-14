from __future__ import annotations

from groundtruth.runtime.adapters.miniswe import seal_delivery, update_receipts
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope, to_dict
from groundtruth.runtime.feature_lineage import build_lineage
from groundtruth.runtime.global_arbiter import Candidate, PLANE_GATEWAY, arbitrate


def _lineage():
    lineage = build_lineage(
        runtime_producer_id="patch_delta",
        evidence_type="signature_mismatch",
        actual_event="edit_result",
        cap_feature_ids=("GT_PATCH_DELTA",),
    )
    assert lineage is not None
    return lineage


def _envelope(*, lineage=None) -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="patch_delta",
        fact_id="parse",
        target="pkg/api.py",
        evidence_type="signature_mismatch",
        payload=("signature mismatch for parse",),
        lineage=lineage,
    )


def test_envelope_lineage_is_identity_and_serialization_neutral() -> None:
    plain = _envelope()
    typed = _envelope(lineage=_lineage())
    assert typed == plain
    assert hash(typed) == hash(plain)
    assert typed.dedup_key == plain.dedup_key
    assert to_dict(typed) == to_dict(plain)


def test_seal_and_receipt_promotion_preserve_lineage() -> None:
    lineage = _lineage()
    sealed, _ = seal_delivery(
        _envelope(lineage=lineage),
        episode_id="ep",
        event_id="7",
        parent_hash="",
        rendered_bytes=b"pkg/api.py parse",
        renderer_id="native",
    )
    assert sealed.lineage is lineage
    promoted = update_receipts([sealed], next_action_cmd="sed -i s/a/b/ pkg/api.py")
    assert promoted[0].lineage is lineage


def test_candidate_lineage_does_not_change_arbitration_or_equality() -> None:
    lineage = _lineage()
    plain = Candidate(
        plane=PLANE_GATEWAY,
        kind="signature_mismatch",
        dedup_key="k",
        tier="VERIFIED",
        confidence=0.9,
        boundary_ordinal=3,
        current_ordinal=3,
    )
    typed = Candidate(
        plane=PLANE_GATEWAY,
        kind="signature_mismatch",
        dedup_key="k",
        tier="VERIFIED",
        confidence=0.9,
        boundary_ordinal=3,
        current_ordinal=3,
        lineage=lineage,
    )
    assert typed == plain
    assert arbitrate([typed]).winner is typed
    assert arbitrate([plain]).repair_support == arbitrate([typed]).repair_support
