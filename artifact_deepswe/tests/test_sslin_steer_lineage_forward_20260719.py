"""SS-LIN (2026-07-19, smoke 29711373486 defect class D) — steer-plane seals must
derive the SAME registered identity their delivery_extra was minted with.

Live kill: `_deliver_gate_winner` / `_commit_prepared_steer` minted delivery_extra
with real lineage but sealed WITHOUT forwarding it; the seal fell back to the
(kind, kind) envelope identity, its dedup_key mechanically differed from
delivery_extra["candidate_id"] on byte-identical text, and the CLASS-6(a) guard
correctly fired ERROR + skipped the receipt (jupyter-ai detect.loop, dynaconf
verify.horizon.executed -> control_participation_integrity missing).

Contract: (GREEN) lineage forwarded -> registered identity, receipt persists, no
ERROR row; (RED, guard intact) identity disagreement still fires the ERROR row
and withholds the receipt.
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
from groundtruth.runtime.evidence_envelope import build_observation_binding  # noqa: E402
from groundtruth.runtime.feature_lineage import build_lineage  # noqa: E402

_STEER = "recovery steer: the build failure is in src/pkg/loader.py not the config"


def _setup(monkeypatch, tmp_path):
    rows: list[dict] = []
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)) or True)
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    g._gt_gateway_chain_head = ""
    return rows


def _executed_lineage(monkeypatch):
    """The SEAM's own registered lineage for the executed covering RED — the exact
    live shape of the dynaconf kill (verify.horizon.executed -> covering_runner/
    covering_red, registration match TRUE, identity differs from the (kind, kind)
    fallback)."""
    monkeypatch.setattr(
        g, "_last_verify_executed_identity",
        ("covering_runner", "covering_red", "test_result"))
    lin = g._lane_registered_lineage(
        "verify.horizon.executed", g.Event.REVIEW_TRANSITION)
    assert lin is not None and getattr(lin, "producer_registration_match", False)
    return lin


def test_lineage_forwarded_seals_registered_identity_and_persists(monkeypatch, tmp_path):
    rows = _setup(monkeypatch, tmp_path)
    lin = _executed_lineage(monkeypatch)
    extra = g._lane_delivery_extra(
        "verify.horizon.executed", _STEER, "src/pkg/loader.py",
        g.Event.REVIEW_TRANSITION, lineage=lin)
    assert extra
    assert extra.get("candidate_id"), "delivery_extra must mint a registered identity"
    binding = build_observation_binding(
        batch_start_iteration=0, parent_policy_sha256="a" * 64,
        parent_policy_chars=7, action_batch_sha256="b" * 64,
        candidate_ordinal=0, candidate_kind="verify.horizon.executed",
        candidate_id=extra["candidate_id"],
    )
    token = g._delivery_observation_context.set(binding)
    try:
        g._seal_lane_delivery(
            "verify.horizon.executed", _STEER, "src/pkg/loader.py",
            base_output="base observation", delivery_extra=dict(extra),
            lineage=lin)
    finally:
        g._delivery_observation_context.reset(token)
    errors = [r for r in rows
              if r.get("reason") == "lane_envelope_candidate_identity_mismatch"
              or (r.get("decision_reason") == "lane_envelope_candidate_identity_mismatch")]
    assert not errors, f"guard must NOT fire when lineage is forwarded: {errors[:1]}"
    path = g._receipts_sidecar_path()
    assert path and os.path.isfile(path), "receipt must persist"
    records = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    for i, rec in enumerate(records):
        parsed = rs._parse_record(rec, i)
        assert parsed.envelope.producer == "covering_runner"  # registered, not fallback


def test_identity_disagreement_still_fires_guard_and_withholds_receipt(monkeypatch, tmp_path):
    rows = _setup(monkeypatch, tmp_path)
    lin = _executed_lineage(monkeypatch)
    # OLD call shape: extra minted WITH lineage (the live callers use the FULL
    # _lane_delivery_extra), seal WITHOUT it -> fallback identity disagreement.
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    extra = g._lane_delivery_extra(
        "verify.horizon.executed", _STEER, "src/pkg/loader.py",
        g.Event.REVIEW_TRANSITION, lineage=lin)
    assert extra and extra.get("candidate_id")
    binding = build_observation_binding(
        batch_start_iteration=0, parent_policy_sha256="a" * 64,
        parent_policy_chars=7, action_batch_sha256="b" * 64,
        candidate_ordinal=0, candidate_kind="verify.horizon.executed",
        candidate_id=extra["candidate_id"],
    )
    token = g._delivery_observation_context.set(binding)
    try:
        g._seal_lane_delivery(
            "verify.horizon.executed", _STEER, "src/pkg/loader.py",
            base_output="base observation", delivery_extra=dict(extra))
    finally:
        g._delivery_observation_context.reset(token)
    joined = json.dumps(rows)
    assert "lane_envelope_candidate_identity_mismatch" in joined, (
        "CLASS-6(a) guard must STILL fire on a real identity disagreement")
    path = g._receipts_sidecar_path()
    assert not (path and os.path.isfile(path)), (
        "a mismatched delivery must never persist a receipt")
