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

    # RE-POINTED 2026-07-28 (Wave 1 Step 3).
    #
    # This companion used to install a binding keyed over the RAW win_text on the
    # ContextVar and assert the reader rejected the resulting receipt. That mutation is
    # now STRUCTURALLY UNOBSERVABLE, by design: `_ensure_observation_binding`
    # (gt_mini_patch.py:163-183) DISCARDS a cached binding whose candidate_id does not
    # match this candidate and derives a fresh one, and the lane seal keys it off the
    # envelope's own dedup_key (:14780-14782). So the injected binding never reached the
    # receipt. Measured, not inferred: a probe of the exact sequence showed the persisted
    # binding carrying the envelope's key and `_parse_record` returning OK.
    #
    # Worse, the old form was ORDER-DEPENDENT: alone it passed, and after any test that
    # called `_augment_output` it failed -- and when it passed it passed for the WRONG
    # reason (`receipt_observation_binding_missing`, not the
    # `receipt_candidate_identity_mismatch` it claimed to prove).
    #
    # The reachable surface is the READER, over the persisted RECORD. Mutate the record's
    # binding identity directly: that is a real corruption class (a receipt written by an
    # older/other writer) and it has two genuinely reachable outcomes.
    # The MUTATION COMPANION that used to live here was DELETED 2026-07-28 (Wave 1 Step 3).
    #
    # It installed a binding keyed over the RAW win_text on the ContextVar and asserted
    # the reader rejected the resulting receipt. That mutation is now structurally
    # unobservable, BY DESIGN: `_ensure_observation_binding` (gt_mini_patch.py:163-183)
    # discards a cached binding whose candidate_id is not this candidate's and derives a
    # fresh one, and the lane seal keys it off the envelope's own dedup_key (:14780-14782).
    # The injected binding never reached the receipt -- measured by probe, not inferred.
    #
    # It was also a FALSE GREEN. Run alone it passed; run after any test that called
    # `_augment_output` it failed. And when it passed, it passed for the WRONG reason --
    # `receipt_observation_binding_missing`, not the `receipt_candidate_identity_mismatch`
    # it claimed to prove. Both branches were dead as mutation detectors.
    #
    # THE COVERAGE IS NOT LOST, and is strictly better targeted: the pool-level detectors
    # in THIS file -- `test_steer_candidate_identity_is_over_transformed_bytes` and
    # `test_lane_a_candidate_identity_is_over_provenance_filtered_bytes` -- still bite the
    # exact mutation (keying over raw rather than transformed bytes) at the pool key,
    # which is where the property actually lives. The reader's fail-closed behaviour is
    # independently pinned by tests/swebench/test_receipt_observation_binding_all_seals_
    # 20260728.py:301-349.
    #
    # NOTE the positive half above is now weaker than it reads: because the seal derives
    # the binding from `env.dedup_key`, the join
    # `observation_candidate_id(envelope.dedup_key) == binding.candidate_id` is
    # TAUTOLOGICAL on any legacy lane seal. It still proves the pipeline persists a
    # parseable, seal-bearing receipt; it no longer proves the keying choice. A
    # reader-level mismatch test over a hand-built record is tracked separately -- it
    # needs a binding that is internally consistent (candidate_dedup_sha256 must agree,
    # evidence_envelope.py validate) while disagreeing with the envelope, which is more
    # than a one-field edit.
