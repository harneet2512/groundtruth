"""SS-SUBJ (2026-07-19, smoke 29711373486 defect class B) — the turn's observed
test file must not become the envelope identity target.

Live kill reproduced: Lane-A facts produced on a TEST-file turn (V2 obligations,
verify.horizon.executed) bound `krel` (the test path) as the envelope target; the
envelope LEAK LAW then correctly refused the envelope and `_seal_lane_delivery`
silently skipped ALL receipt bookkeeping — a sealed delivered ledger row with no
receipt (csvkit/hydra/haystack-8997: `receipt_identity_not_found`).

Contract under test:
  - the leak LAW itself is untouched (a leaky payload/provenance still refuses);
  - the identity SUBJECT is sanitized: leaky observed path -> "" (deliverable
    paths pass byte-identical);
  - a Lane-A delivery on a test-file turn now persists a receipt that parses
    under the STRICT sidecar reader (no tolerance involved);
  - pool key / delivery_extra candidate_id / sealed dedup_key stay in lockstep.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "src"), str(_REPO / "scripts" / "swebench"),
          str(_REPO / "artifact_deepswe")):
    if p not in sys.path:
        sys.path.insert(0, p)

import gt_mini_patch as g  # noqa: E402
import receipt_sidecar as rs  # noqa: E402
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    EvidenceEnvelope,
    build_observation_binding,
    validate,
)

_TEST_PATH = "tests/test_utilities/test_in2csv.py"
_PROD_PATH = "csvkit/utilities/in2csv.py"
_OBLIG = "obligations: exercise the new --format flag end to end before submit"


def test_envelope_subject_sanitizes_only_leaky_paths():
    assert g._envelope_subject(_TEST_PATH) == ""
    assert g._envelope_subject("demo/example.py") == ""
    assert g._envelope_subject(_PROD_PATH) == _PROD_PATH
    assert g._envelope_subject("") == ""


def test_leak_law_untouched_for_leaky_payload_provenance():
    env = EvidenceEnvelope.build(
        producer="spec", fact_id="", target="",
        evidence_type="obligation_unexercised", payload=(_OBLIG,),
        provenance=((_TEST_PATH, 3),),
    )
    issues = validate(env)
    assert any("leak" in issue for issue in issues), issues


def test_lane_delivery_on_test_turn_persists_strict_parseable_receipt(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    g._gt_gateway_chain_head = ""
    # identity in lockstep: pool-style key over the SANITIZED subject
    producer, ev = g._lane_envelope_identity("obligation.unexercised", None)
    key = g._ga_unified_dedup_key(
        producer, ev, g._envelope_subject(_TEST_PATH), "", [_OBLIG])
    binding = build_observation_binding(
        batch_start_iteration=0, parent_policy_sha256="a" * 64,
        parent_policy_chars=9, action_batch_sha256="b" * 64,
        candidate_ordinal=0, candidate_kind="obligation.unexercised",
        candidate_id=key,
    )
    token = g._delivery_observation_context.set(binding)
    try:
        g._seal_lane_delivery(
            "obligation.unexercised", _OBLIG, _TEST_PATH,
            base_output="base observation",
            delivery_extra=g._lane_final_extra(
                "obligation.unexercised", _OBLIG, _TEST_PATH),
        )
    finally:
        g._delivery_observation_context.reset(token)
    path = g._receipts_sidecar_path()
    assert path and os.path.isfile(path), (
        "class B: the test-turn delivery must persist a receipt (was silently "
        "skipped by the leak-law early-return before SS-SUBJ)")
    records = [json.loads(line) for line in open(path, encoding="utf-8")
               if line.strip()]
    assert records, "at least one receipt line"
    for i, rec in enumerate(records):
        parsed = rs._parse_record(rec, i)  # STRICT reader; raises on any defect
        assert parsed.key.candidate_id == binding.candidate_id


def test_deliverable_subject_key_unchanged():
    producer, ev = g._lane_envelope_identity("l3b.evidence", None)
    raw = g._ga_unified_dedup_key(producer, ev, _PROD_PATH, "", ["caller rows"])
    sanitized = g._ga_unified_dedup_key(
        producer, ev, g._envelope_subject(_PROD_PATH), "", ["caller rows"])
    assert raw == sanitized  # byte-identical for every currently-receipted delivery
