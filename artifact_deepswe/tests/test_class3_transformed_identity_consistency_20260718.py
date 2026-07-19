r"""CLASS 3 — a transformed winner's identity is frozen over the FINAL delivered bytes.

Two producers transform their payload AFTER the pre-transform text would otherwise have keyed
the candidate dedup key + opportunity binding:

  (a) Lane-A: GT_SS_PROVENANCE drops scratch-path LINES at delivery (_prepare_batch_delivery ->
      _commit_prepared_lane seals over the filtered payload). The pool used to key the candidate
      over the RAW text.
  (b) Steer: GT_STEER_NATIVE strips the <gt-*> frame at delivery (_deliver_gate_winner ->
      _steer_native). The pool used to key the candidate over the RAW win_text.

Either way the sealed envelope.dedup_key (over the transformed bytes) disagreed with
binding.candidate_id (over the raw bytes), so receipt_sidecar._parse_record rejected the WHOLE
sidecar (receipt_candidate_identity_mismatch / observation_binding:candidate_id_mismatch). The
fixes key the candidate over the SAME transformed bytes the delivery seals.

RED-first / MUTATION: reverting either key back to the raw payload re-reddens the candidate-level
tests below AND makes the transformed steer's receipt fail _parse_record (the explicit CLASS 3(c)
acceptance: observation_candidate_id(envelope.dedup_key) == binding.candidate_id must PASS).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "swebench"))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.evidence_envelope import build_observation_binding  # noqa: E402
import receipt_sidecar as rs  # noqa: E402

_STEER = '\n<gt-nudge reason="recovery">\nGT: run a covering test now\n</gt-nudge>'
_BAD = "/tmp/scratch/generated_thing.py"
_LANE_TEXT = (
    "[CALLERS] 2 verified caller(s) -- preserve this interface\n"
    "  " + _BAD + ":10 bar()\n"
    "  src/real.py:20 baz()"
)


def _reset(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    g._gt_gateway_chain_head = ""
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")


# --------------------------------------------------------------------------- #
# (b) STEER — dedup key over the native-transformed bytes
# --------------------------------------------------------------------------- #
def test_steer_candidate_identity_is_over_transformed_bytes(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("GT_STEER_NATIVE", "1")
    g._last_gate_winner_kind = "recovery"
    g._last_gate_winner_hash = "h1"
    pool: list = []
    out = {"output": "base observation"}
    g._global_pool_add_steer(
        pool, out, "cmd", _STEER, kkind="post_test", kf="", krel="src/x.py",
        event=g.Event.TEST_RESULT, steer_base="base observation")
    assert pool, "steer candidate must be pooled"
    cand = pool[0][0]
    prepared = g._steer_native(_STEER, kind="recovery", stage_control=False)
    assert prepared != _STEER, "precondition: GT_STEER_NATIVE must transform this steer"
    lin = g._lane_registered_lineage("recovery", g.Event.TEST_RESULT)
    prod, ev = g._lane_envelope_identity("recovery", lin)
    over_final = g._ga_unified_dedup_key(prod, ev, "src/x.py", "", [prepared])
    over_raw = g._ga_unified_dedup_key(prod, ev, "src/x.py", "", [_STEER])
    assert cand.dedup_key == over_final, "steer identity must key over the transformed bytes"
    assert cand.dedup_key != over_raw
    # MUTATION[key over [win_text]] -> cand.dedup_key == over_raw -> RED.


# --------------------------------------------------------------------------- #
# (a) LANE-A — dedup key over the provenance-filtered bytes
# --------------------------------------------------------------------------- #
def test_lane_a_candidate_identity_is_over_provenance_filtered_bytes(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    root = g._root()
    filtered = g._ss_provenance_filter(_LANE_TEXT, root)
    assert filtered != _LANE_TEXT, "precondition: provenance must drop the scratch-path line"
    pool: list = []
    out = {"output": "base"}
    g._global_pool_add_lane_a(
        pool, out, "cmd", [("l3.contract", _LANE_TEXT, "src/real.py")],
        krel="src/real.py", event=g.Event.POST_VIEW, kkind="post_view")
    assert pool, "lane-A candidate must be pooled"
    cand = pool[0][0]
    lin = g._lane_registered_lineage("l3.contract", g.Event.POST_VIEW, text=_LANE_TEXT)
    prod, ev = g._lane_envelope_identity("l3.contract", lin)
    over_final = g._ga_unified_dedup_key(prod, ev, "src/real.py", "", [filtered])
    over_raw = g._ga_unified_dedup_key(prod, ev, "src/real.py", "", [_LANE_TEXT])
    assert cand.dedup_key == over_final, "lane-A identity must key over the filtered bytes"
    assert cand.dedup_key != over_raw
    # MUTATION[key over [text]] -> cand.dedup_key == over_raw -> RED.


# --------------------------------------------------------------------------- #
# (c) end-to-end: the transformed steer's RECEIPT parses consistently
# --------------------------------------------------------------------------- #
def test_native_steer_receipt_parses_consistently(monkeypatch, tmp_path):
    """Drive the REAL steer pool + its commit thunk and assert the persisted receipt passes
    receipt_sidecar._parse_record (envelope.dedup_key canonicalizes to binding.candidate_id)."""
    _reset(monkeypatch)
    monkeypatch.setenv("GT_STEER_NATIVE", "1")
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    g._last_gate_winner_kind = "recovery"
    g._last_gate_winner_hash = "h1"
    pool: list = []
    out = {"output": "base observation"}
    g._global_pool_add_steer(
        pool, out, "cmd", _STEER, kkind="post_test", kf="", krel="src/x.py",
        event=g.Event.TEST_RESULT, steer_base="base observation")
    assert pool, "steer candidate must be pooled"
    cand, thunk = pool[0]
    # The seam builds the opportunity binding from the candidate's own dedup key (my fix -> over
    # the transformed bytes). Install it, then run the ACTUAL commit thunk (-> _deliver_gate_winner
    # -> transform -> seal -> persist receipt).
    binding = build_observation_binding(
        batch_start_iteration=0, parent_policy_sha256="a" * 64, parent_policy_chars=5,
        action_batch_sha256="b" * 64, candidate_ordinal=0, candidate_kind="recovery",
        candidate_id=cand.dedup_key)
    token = g._delivery_observation_context.set(binding)
    try:
        thunk()
    finally:
        g._delivery_observation_context.reset(token)
    path = g._receipts_sidecar_path()
    assert path and os.path.isfile(path), "the commit must persist a receipt under GT_CERT_DIR"
    records = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    assert records, "at least one receipt must be written"
    for i, rec in enumerate(records):
        rs._parse_record(rec, i)  # raises on receipt_candidate_identity_mismatch -> the fix's proof

    # MUTATION / RED companion: a binding keyed over the RAW win_text (pre-fix behavior) is REJECTED
    # by the SAME reader — proving the transformed-bytes keying is load-bearing.
    lin = g._lane_registered_lineage("recovery", g.Event.TEST_RESULT)
    prod, ev = g._lane_envelope_identity("recovery", lin)
    raw_key = g._ga_unified_dedup_key(prod, ev, "src/x.py", "", [_STEER])
    raw_binding = build_observation_binding(
        batch_start_iteration=0, parent_policy_sha256="a" * 64, parent_policy_chars=5,
        action_batch_sha256="b" * 64, candidate_ordinal=1, candidate_kind="recovery",
        candidate_id=raw_key)
    import pytest
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    g._gt_gateway_chain_head = ""
    out2 = {"output": "base observation"}
    pool2: list = []
    g._last_gate_winner_kind = "recovery"
    g._last_gate_winner_hash = "h2"
    g._global_pool_add_steer(
        pool2, out2, "cmd", _STEER, kkind="post_test", kf="", krel="src/x.py",
        event=g.Event.TEST_RESULT, steer_base="base observation")
    token2 = g._delivery_observation_context.set(raw_binding)
    try:
        pool2[0][1]()
    finally:
        g._delivery_observation_context.reset(token2)
    records2 = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    raw_rec = records2[-1]
    with pytest.raises(ValueError):
        rs._parse_record(raw_rec, 99)
