r"""CLASS 6 — the lane seal must GUARD identity before it persists, and an intentional
identity-mismatch report must keep its NAMED reason + both identities.

(a) ``_seal_lane_delivery`` used to ``_persist_receipt`` BEFORE the identity-disagreement guard,
    so a detected candidate/dedup-key mismatch still poisoned the durable sidecar. The guard now
    runs FIRST: on mismatch it records the typed ERROR control row and writes NO receipt / NO
    producer attestation; a consistent (or absent) identity persists as before.
    RED (pre-fix): the receipt was persisted on the mismatch path too.
    MUTATION: move ``_persist_receipt`` back above the guard -> ``test_seal_mismatch_persists_no_receipt``
    sees the receipt written on a mismatch -> RED.

(b) The strict-default failure row recorded the BINDING's candidate_id, losing the disagreeing
    INPUT id. It now also carries ``mismatched_candidate_id``.
    MUTATION: drop the ``mismatched_candidate_id`` line in the except path -> RED.

(c) An intentional mismatch reporter (``allow_candidate_mismatch=True``) keeps its NAMED reason as
    the row reason instead of degrading to ``control_record_error:ValueError``.
    MUTATION: call without ``allow_candidate_mismatch`` -> the reason degrades -> RED.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    EvidenceEnvelope,
    build_observation_binding,
)

_PARENT = "a" * 64
_ACTION = "b" * 64


def _install_binding(candidate_id_hex: str):
    """Set a valid current ObservationBinding whose canonical candidate id is candidate_id_hex."""
    binding = build_observation_binding(
        batch_start_iteration=0,
        parent_policy_sha256=_PARENT,
        parent_policy_chars=10,
        action_batch_sha256=_ACTION,
        candidate_ordinal=0,
        candidate_kind="lane",
        candidate_id=candidate_id_hex,
    )
    return g._delivery_observation_context.set(binding), binding


def _seal_env(monkeypatch, calls: dict):
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    g._gt_gateway_chain_head = ""
    calls["receipt"] = 0
    calls["attest"] = 0
    calls["control"] = []
    monkeypatch.setattr(g, "_persist_receipt", lambda *a, **k: calls.__setitem__("receipt", calls["receipt"] + 1))
    monkeypatch.setattr(g, "_persist_lane_producer_attestation", lambda *a, **k: calls.__setitem__("attest", calls["attest"] + 1))
    monkeypatch.setattr(
        g, "_control_participation_record",
        lambda *a, **k: calls["control"].append((a, k)))


def _lane_dedup_key(text: str, target: str) -> str:
    producer, evidence_type = g._lane_envelope_identity("l5.no_test", None)
    env = EvidenceEnvelope.build(
        producer=producer, fact_id="", target=target,
        evidence_type=evidence_type, payload=(text,), lineage=None)
    return getattr(env, "dedup_key", "") or ""


# --------------------------------------------------------------------------- #
# CLASS 6(a) — guard runs BEFORE persist
# --------------------------------------------------------------------------- #
def test_seal_mismatch_persists_no_receipt(monkeypatch):
    calls: dict = {}
    _seal_env(monkeypatch, calls)
    # force a terminal identity whose candidate_id DISAGREES with the real env.dedup_key.
    monkeypatch.setattr(g, "_terminal_delivery_identity", lambda _x: ("caller_contract", "zz_wrong_id"))
    g._seal_lane_delivery("l5.no_test", "GT: run a covering test", "src/x.py",
                          base_output="obs", delivery_extra={"any": 1})
    assert calls["receipt"] == 0, "mismatch must persist NO receipt (guard runs before persist)"
    assert calls["attest"] == 0, "mismatch must persist NO producer attestation"
    # the typed ERROR control row is still recorded, with its NAMED reason + the mismatch flag.
    errs = [c for c in calls["control"]
            if c[0][2] == "ERROR" and c[1].get("reason") == "lane_envelope_candidate_identity_mismatch"]
    assert len(errs) == 1, f"expected one named ERROR control row; got {calls['control']}"
    assert errs[0][1].get("allow_candidate_mismatch") is True
    # the delivery bytes were still sealed (bookkeeping over already-shipped bytes).
    assert g._gt_gateway_deliveries, "seal record must exist even on an identity mismatch"


def test_seal_consistent_identity_persists_receipt(monkeypatch):
    calls: dict = {}
    _seal_env(monkeypatch, calls)
    text, target = "GT: run a covering test", "src/x.py"
    good = _lane_dedup_key(text, target)
    assert good, "precondition: env must derive a dedup key"
    monkeypatch.setattr(g, "_terminal_delivery_identity", lambda _x: ("caller_contract", good))
    g._seal_lane_delivery("l5.no_test", text, target, base_output="obs", delivery_extra={"any": 1})
    assert calls["receipt"] == 1, "a consistent identity must persist the receipt"
    assert calls["attest"] == 1, "a consistent identity must persist the producer attestation"
    applied = [c for c in calls["control"] if c[0][2] == "APPLIED"]
    assert applied, "a consistent identity records the APPLIED control row"


def test_seal_no_terminal_identity_still_persists_receipt(monkeypatch):
    calls: dict = {}
    _seal_env(monkeypatch, calls)
    monkeypatch.setattr(g, "_terminal_delivery_identity", lambda _x: None)
    g._seal_lane_delivery("l5.no_test", "GT: run a covering test", "src/x.py",
                          base_output="obs", delivery_extra=None)
    assert calls["receipt"] == 1, "no terminal identity to check -> normal persist preserved"


# --------------------------------------------------------------------------- #
# CLASS 6(b)+(c) — the real _control_participation_record identity handling
# --------------------------------------------------------------------------- #
def _capture_rows(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    return rows


def test_intentional_mismatch_keeps_named_reason_and_records_both_ids(monkeypatch):
    """CLASS 6(c): allow_candidate_mismatch keeps the NAMED reason (not control_record_error) and
    CLASS 6(b): preserves the disagreeing input id while canonicalizing candidate_id to the binding."""
    rows = _capture_rows(monkeypatch)
    token, binding = _install_binding("a" * 16)
    try:
        g._control_participation_record(
            "GT_LANE_ENVELOPE", "mini_seam.lane_envelope.candidate_conversion", "ERROR",
            candidate_bytes="x", fact_class="caller_contract",
            candidate_id="b" * 16, reason="lane_envelope_candidate_identity_mismatch",
            allow_candidate_mismatch=True)
    finally:
        g._delivery_observation_context.reset(token)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.get("decision_reason") == "lane_envelope_candidate_identity_mismatch", (
        "the NAMED mismatch reason must survive (not degrade to control_record_error)")
    assert row.get("reason") != "control_record_error:ValueError"
    assert row.get("participation_decision") == "ERROR"
    assert row.get("candidate_id") == "a" * 16, "record id canonicalized to the binding"
    assert row.get("mismatched_candidate_id") == "b" * 16, "the disagreeing INPUT id is preserved"


def test_strict_default_mismatch_failure_row_preserves_disagreeing_id(monkeypatch):
    """CLASS 6(b): the STRICT default raises -> the failure row records BOTH the binding id
    (canonical) and the disagreeing input id."""
    rows = _capture_rows(monkeypatch)
    token, binding = _install_binding("a" * 16)
    try:
        g._control_participation_record(
            "GT_LANE_ENVELOPE", "mini_seam.lane_envelope.candidate_conversion", "ERROR",
            candidate_bytes="x", fact_class="caller_contract",
            candidate_id="b" * 16, reason="some_named_reason")  # allow_candidate_mismatch defaults False
    finally:
        g._delivery_observation_context.reset(token)
    assert len(rows) == 1, rows
    row = rows[0]
    assert str(row.get("reason", "")).startswith("control_record_error"), (
        "strict default must fail closed to the generic error row")
    assert row.get("candidate_id") == "a" * 16, "failure row records the binding's canonical id"
    assert row.get("mismatched_candidate_id") == "b" * 16, "and preserves the disagreeing input id"
