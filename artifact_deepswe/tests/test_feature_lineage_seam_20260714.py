from __future__ import annotations

import json

import gt_mini_patch as g
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
from groundtruth.runtime.feature_lineage import build_lineage, lineage_to_dict


def _lineage():
    value = build_lineage(
        runtime_producer_id="patch_delta",
        evidence_type="signature_mismatch",
        actual_event="edit_result",
        cap_feature_ids=("GT_PATCH_DELTA",),
    )
    assert value is not None
    return value


def _envelope() -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="patch_delta", fact_id="symbol", target="src/example.py",
        evidence_type="signature_mismatch", payload=("exact payload",),
        provenance=(("src/example.py", 7),), confidence=0.9, tier="verified",
        graph_revision="rev", valid_until="rev", preferred_event="edit",
        blocking_eligibility="advisory", estimated_cost_tokens=4,
        measured=False, lineage=_lineage(),
    )


def test_global_candidate_transports_lineage_without_affecting_ranking(monkeypatch) -> None:
    monkeypatch.setattr(g, "_ss_enabled", lambda _name: False)
    lineage = _lineage()
    candidate = g._ga_make_candidate(
        "gateway", "signature_mismatch", dedup_key="d", lineage=lineage,
    )
    assert candidate is not None
    assert candidate.lineage is lineage


def test_receipt_persists_lineage_outside_stable_envelope_schema(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    env = _envelope()
    g._persist_receipt(env, kind="gateway", transition="delivered")

    row = json.loads((tmp_path / "gt_receipts.jsonl").read_text(encoding="utf-8"))
    assert "lineage" not in row["envelope"]
    assert env.lineage is not None
    assert row["lineage"] == lineage_to_dict(env.lineage)


def test_gateway_pool_candidate_preserves_envelope_lineage(monkeypatch) -> None:
    monkeypatch.setattr(g, "_ss_any_content_gate_on", lambda: False)
    pool = []
    winner = _envelope()
    g._global_pool_add_gateway(
        pool, winner, False, lambda: None, ev_kind="edit",
        rendered_text="exact payload",
    )
    assert len(pool) == 1
    assert pool[0][0].lineage is winner.lineage
