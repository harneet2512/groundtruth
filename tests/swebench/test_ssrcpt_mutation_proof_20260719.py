"""SS-RCPT (2026-07-19, smoke 29711373486 defect class A) — evidence-backed
receipt tolerance for post-pool byte mutation.

Live kill reproduced: a behavioural lane delivery (detect.coherence) whose bytes
the provenance filter mutated AFTER pooling persists a receipt whose envelope
dedup_key (FINAL bytes) differs from ObservationBinding.candidate_id (PRODUCED
bytes). The strict reader killed the whole sidecar (sh-744/arviz-2413/hydra-3005).

Contract under test:
  RED  — an UNEXPLAINED mismatch (no produced_dedup_key) is still fatal;
  GREEN — a mismatch is tolerated ONLY when produced_dedup_key == binding.candidate_id;
  RED  — a WRONG produced_dedup_key stays fatal (no blanket tolerance);
  and the join key still uses binding.candidate_id (the ledger-coherent spine).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "src"), str(_REPO / "scripts" / "swebench")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

import receipt_sidecar as rs  # noqa: E402
from groundtruth.runtime.adapters.miniswe import seal_delivery  # noqa: E402
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    EvidenceEnvelope,
    build_observation_binding,
    to_dict as envelope_to_dict,
)

_PRODUCED = "coherence steer: stop editing tests/x.py in a loop"
_FINAL = "coherence steer: stop editing in a loop"  # provenance filter dropped a token


def _sealed_receipt_record(*, bind_over: str, transition: str = "delivered") -> dict:
    """Build one receipt record exactly as the seam persists it: envelope over the
    FINAL bytes, binding candidate_id over ``bind_over``, sealed for delivery."""
    env = EvidenceEnvelope.build(
        producer="detect.coherence", fact_id="", target="src/mod.py",
        evidence_type="detect.coherence", payload=(_FINAL,),
    )
    bind_env = EvidenceEnvelope.build(
        producer="detect.coherence", fact_id="", target="src/mod.py",
        evidence_type="detect.coherence", payload=(bind_over,),
    )
    binding = build_observation_binding(
        batch_start_iteration=0, parent_policy_sha256="a" * 64,
        parent_policy_chars=11, action_batch_sha256="b" * 64,
        candidate_ordinal=0, candidate_kind="detect.coherence",
        candidate_id=bind_env.dedup_key,
    )
    sealed, _head = seal_delivery(
        env, episode_id="ep", event_id="1", parent_hash="",
        rendered_bytes=("\n" + _FINAL).encode("utf-8"), renderer_id="lane",
        tool_output_bytes=b"base observation", boundary=b"16:detect.coherence",
        dedup_chain=set(), observation_binding=binding,
    )
    record = {
        "schema": "gt_receipt.v1",
        "kind": "lane",
        "transition": transition,
        "action_index": 3,
        "ts_ms": 1,
        "envelope": envelope_to_dict(sealed),
    }
    produced_env = EvidenceEnvelope.build(
        producer="detect.coherence", fact_id="", target="src/mod.py",
        evidence_type="detect.coherence", payload=(_PRODUCED,),
    )
    return record, produced_env.dedup_key, binding


def test_unexplained_mismatch_stays_fatal():
    record, _produced_key, _binding = _sealed_receipt_record(bind_over=_PRODUCED)
    with pytest.raises(ValueError):
        rs._parse_record(record, 0)


def test_proven_mutation_parses_and_keeps_ledger_join_key():
    record, produced_key, binding = _sealed_receipt_record(bind_over=_PRODUCED)
    record["produced_dedup_key"] = produced_key
    parsed = rs._parse_record(record, 0)
    # The join key must stay on the binding's (produced/ledger-coherent) identity.
    assert parsed.key.candidate_id == binding.candidate_id
    assert parsed.transition == "delivered"


def test_wrong_proof_stays_fatal():
    record, _produced_key, _binding = _sealed_receipt_record(bind_over=_PRODUCED)
    record["produced_dedup_key"] = "f" * 16  # hex-shaped but wrong identity
    with pytest.raises(ValueError):
        rs._parse_record(record, 0)


def test_consistent_receipt_still_parses_without_proof():
    record, _produced_key, _binding = _sealed_receipt_record(bind_over=_FINAL)
    parsed = rs._parse_record(record, 0)
    assert parsed.transition == "delivered"
