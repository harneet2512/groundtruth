"""SEAM ROUND-TRIP GATE (2026-07-20) — the systemic cure for writer/reader drift.

Every defect class the paid runs exposed (A receipt-identity, B leaky-subject
receipt blackout, D lineage fallback) was a WRITER/READER pair disagreeing under
a live condition offline tests never exercised. This gate closes the practice
gap: for EVERY lane kind x live condition, generate a delivery through the REAL
seam writer path and replay it through the REAL readers, asserting zero
disagreement. A future writer or reader change that breaks any pairing fails
HERE, offline, before any paid dispatch.

Round-trip contract per (kind, condition):
  writer:  _lane_delivery_extra -> binding -> _seal_lane_delivery
  readers: receipt_sidecar._parse_record (STRICT) on every persisted line;
           receipt key joins the binding candidate identity;
           no silent receipt blackout (a sealed delivery must persist a receipt);
           no CLASS-6(a) ERROR row for a consistent identity.
Conditions: plain product turn · TEST-FILE turn (leaky observed subject, class B)
· post-pool byte mutation (provenance-style, class A) · registered steer lineage
(class D). Kinds cover the behavioural + evidence lane families generically —
parameterized, no task/repo keying.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "src"), str(_REPO / "scripts" / "swebench"),
          str(_REPO / "artifact_deepswe")):
    if p not in sys.path:
        sys.path.insert(0, p)

import gt_mini_patch as g  # noqa: E402
import receipt_sidecar as rs  # noqa: E402
from groundtruth.runtime.evidence_envelope import build_observation_binding  # noqa: E402

_PRODUCT_PATH = "src/pkg/mod.py"
_TEST_PATH = "tests/test_pkg/test_mod.py"
_TEXT = "the retry loop re-reads the config on every failure; cap it at three"
_MUTATED = "the retry loop re-reads the config on every failure"  # provenance-trimmed

_KINDS = ["detect.coherence", "detect.loop", "recovery", "l3b.evidence",
          "obligation.unexercised", "verify.horizon.executed"]
_CONDITIONS = ["plain", "test_turn", "mutation", "test_turn+mutation"]


def _deliver(monkeypatch, tmp_path, kind: str, condition: str):
    rows: list[dict] = []
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setattr(g, "_ledger_line_direct",
                        lambda row: rows.append(dict(row)) or True)
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    g._gt_gateway_chain_head = ""
    g._receipt_produced_keys.clear()
    if kind == "verify.horizon.executed":
        monkeypatch.setattr(g, "_last_verify_executed_identity",
                            ("covering_runner", "covering_red", "test_result"))
        lineage = g._lane_registered_lineage(kind, g.Event.REVIEW_TRANSITION)
        event = g.Event.REVIEW_TRANSITION
    else:
        lineage = None
        event = g.Event.TEST_RESULT

    subject = _TEST_PATH if "test_turn" in condition else _PRODUCT_PATH
    produced = _TEXT
    final = _MUTATED if "mutation" in condition else _TEXT

    producer, ev = g._lane_envelope_identity(kind, lineage)
    key = g._ga_unified_dedup_key(
        producer, ev, g._envelope_subject(subject), "", [produced])
    binding = build_observation_binding(
        batch_start_iteration=0, parent_policy_sha256="a" * 64,
        parent_policy_chars=len(produced), action_batch_sha256="b" * 64,
        candidate_ordinal=0, candidate_kind=kind, candidate_id=key,
    )
    extra = g._lane_delivery_extra(kind, final, subject, event, lineage=lineage)
    token = g._delivery_observation_context.set(binding)
    try:
        g._seal_lane_delivery(
            kind, final, subject, base_output="base observation",
            producer_text=produced, identity_text=final,
            delivery_extra=dict(extra or {}), lineage=lineage)
    finally:
        g._delivery_observation_context.reset(token)
    return rows, binding


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("condition", _CONDITIONS)
def test_seam_roundtrip(monkeypatch, tmp_path, kind, condition):
    rows, binding = _deliver(monkeypatch, tmp_path, kind, condition)

    # 1. No silent receipt blackout: the sealed delivery MUST persist a receipt.
    path = g._receipts_sidecar_path()
    assert path and os.path.isfile(path), (
        f"[{kind} @ {condition}] sealed delivery persisted NO receipt "
        "(class-B-style blackout)")

    # 2. Every persisted line must parse under the STRICT reader.
    records = [json.loads(line) for line in open(path, encoding="utf-8")
               if line.strip()]
    assert records
    for i, rec in enumerate(records):
        parsed = rs._parse_record(rec, i)  # raises on ANY identity disagreement
        # 3. The receipt join key must carry the binding's candidate identity.
        assert parsed.key.candidate_id == binding.candidate_id, (
            f"[{kind} @ {condition}] receipt join key diverged from binding")

    # 4. No CLASS-6(a) identity-mismatch ERROR row for a consistent delivery.
    joined = json.dumps(rows)
    assert "lane_envelope_candidate_identity_mismatch" not in joined, (
        f"[{kind} @ {condition}] consistent identity tripped the mismatch guard")
