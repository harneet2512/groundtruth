"""B-GW: the GT_GATEWAY mediator join closes at the COMMITTED-delivery identity.

Defect (live: amoffat__sh-744, 3 GT_GATEWAY records / 0 joins): the CAP mediator records
its participation at ``gateway.augment.candidate_admission`` using the PRE-render admission
candidate identity (``evidence_envelope.render_bytes`` — the canonical envelope JSON). The
seam then RE-renders + RE-keys the winner to its tagged/native presentation before the
byte-preserving append, so the delivered ledger row's ``content_sha256_16`` is the seal of
those FINAL bytes and the admission seal can NEVER equal it — the grader's exact mediation
join (``gt_feature_metrics._control_participation_evidence``: chars + seal + candidate_id +
iteration) can never close.

Fix: ``gateway.record_committed_delivery`` writes a SUPPLEMENTARY GT_GATEWAY row at
``mini_seam.gateway.candidate_committed`` carrying the seam's COMMITTED bytes + the delivered
candidate_id (mirroring GT_GATEWAY_NATIVE), driven by the seam's own gateway delivery path.
The admission row is preserved (decision-precedes-delivery chronology anchor).

TTD: a synthetic gateway flow (admission -> render -> delivery) where the pre-fix rows yield
0 joins and the committed row yields the exact join; plus a chronology guard, a forged-seal /
wrong-id rejection, and the correct-or-quiet contract of the producer. The GREEN/chronology/
forged rows are built by driving the REAL producer, so a mutation of the producer's identity
re-breaks the join.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from groundtruth.runtime import gateway
from groundtruth.runtime.adapters.miniswe import render_envelope
from groundtruth.runtime.control_participation import (
    build_control_participation,
    control_contract,
    participation_to_dict,
)
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
from groundtruth.runtime.evidence_envelope import render_bytes as envelope_candidate_bytes

# gt_feature_metrics lives under scripts/swebench (mirrors the collector test's bootstrap).
ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import gt_feature_metrics as metrics  # noqa: E402

ITER = 5


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _env(evidence_type: str = "def_ref_partition") -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="def_ref_partition",
        fact_id="Widget",
        target="src/widget.py",
        evidence_type=evidence_type,
        payload=("src/widget.py:4:Widget",),
        provenance=(("src/widget.py", 4),),
        confidence=0.9,
        tier="VERIFIED",
        preferred_event="search",
    )


def _shipped(env: EvidenceEnvelope, *, native: bool = False) -> str:
    """The seam's committed bytes: the rendered winner + the F6 leading-newline boundary."""
    return "\n" + render_envelope(env, native=native)


def _ledger_row(participation: dict) -> dict:
    """Serialize a participation record into the on-disk control-participation ledger row,
    exactly as ``gt_mini_patch._control_participation_record`` writes it (rename decision ->
    participation_decision, add the control.participation envelope columns)."""
    payload = dict(participation)
    decision = payload.pop("decision")
    iteration = payload.pop("iteration")
    return {
        "layer": "control.participation",
        "event_type": "control_decision",
        "file_path": "",
        "outcome": "measurement_failed" if decision == "ERROR" else "evaluated",
        "chars_delivered": 0,
        "iteration": iteration,
        "participation_decision": decision,
        **payload,
    }


def _admission_row(env: EvidenceEnvelope) -> dict:
    """The production admission-time GT_GATEWAY row: the PRE-render candidate identity
    (``render_bytes`` — the same bytes ``_candidate_control_identity`` stamps at augment)."""
    pre = envelope_candidate_bytes(env).decode("utf-8")
    return _ledger_row(participation_to_dict(build_control_participation(
        feature_id="GT_GATEWAY",
        decision_site="gateway.augment.candidate_admission",
        decision="APPLIED",
        iteration=ITER,
        candidate_bytes=pre,
        fact_class="def_partition",
        candidate_id=env.dedup_key,
        reason="candidate_admitted",
    )))


def _committed_row_via_producer(env: EvidenceEnvelope, final_bytes: str) -> dict:
    """Drive the REAL producer (``gateway.record_committed_delivery``) and serialize its one
    emitted call to a ledger row. Mutating the producer's identity re-breaks this row."""
    captured: list[dict] = []

    def recorder(feature_id, decision_site, decision, **extra):
        captured.append(participation_to_dict(build_control_participation(
            feature_id=feature_id,
            decision_site=decision_site,
            decision=decision,
            iteration=ITER,
            candidate_bytes=extra.get("candidate_bytes", ""),
            fact_class=extra.get("fact_class"),
            candidate_id=extra.get("candidate_id", ""),
            reason=extra.get("reason", ""),
        )))

    gateway.record_committed_delivery(env, final_bytes, recorder)
    assert len(captured) == 1, "producer must emit exactly one committed row"
    return _ledger_row(captured[0])


def _delivered_row(env: EvidenceEnvelope, final_bytes: str, **changes) -> dict:
    row = {
        "layer": "l3.def",
        "event_type": "post_search",
        "outcome": "delivered",
        "iteration": ITER,
        "chars_delivered": len(final_bytes),
        "content_sha256_16": _sha16(final_bytes),
        "native_text": final_bytes,
        "candidate_id": env.dedup_key,
        "lineage_schema": "gt.feature_lineage.v1",
        "evidence_type": "def_ref_partition",
        "runtime_producer_id": "def_ref_partition",
        "registered_producer_id": "post_search",
        "producer_registration_match": True,
        "fact_class": "def_partition",
    }
    row.update(changes)
    return row


def _gateway_joins(rows: list[dict], final_bytes: str) -> tuple[dict, list]:
    messages = [{"role": "tool", "content": "observation output\n" + final_bytes}]
    evidence = metrics._control_participation_evidence(rows, messages, {"entries": []}, None)
    return evidence, evidence["joins"].get("GT_GATEWAY", [])


# --------------------------------------------------------------------------- #
# The defect precondition + the producer's committed identity.
# --------------------------------------------------------------------------- #
def test_admission_seal_can_never_equal_the_delivered_seal() -> None:
    env = _env()
    final = _shipped(env)
    pre = envelope_candidate_bytes(env).decode("utf-8")
    # The admission row stamps the PRE-render envelope bytes; the delivery seals the FINAL
    # rendered bytes. They are structurally different -> the admission join can never close.
    assert pre != final
    assert _sha16(pre) != _sha16(final)


def test_producer_records_the_committed_delivery_identity() -> None:
    env = _env()
    final = _shipped(env)
    committed = _committed_row_via_producer(env, final)
    assert committed["control_ref"]["feature_id"] == "GT_GATEWAY"
    assert committed["decision_site"] == "mini_seam.gateway.candidate_committed"
    # The seal + chars are the COMMITTED bytes, not the pre-render admission bytes.
    assert committed["candidate_sha256_16"] == _sha16(final)
    assert committed["candidate_chars"] == len(final)
    assert committed["candidate_id"] == env.dedup_key
    assert committed["fact_class"] == "def_partition"
    assert committed["participation_decision"] == "APPLIED"


# --------------------------------------------------------------------------- #
# RED -> GREEN: the mediation join closes only with the committed row.
# --------------------------------------------------------------------------- #
def test_admission_alone_never_joins_but_committed_row_closes_it() -> None:
    env = _env()
    final = _shipped(env)
    admission = _admission_row(env)
    delivered = _delivered_row(env, final)

    # RED (current production): only admission + delivery exist -> zero joins.
    ev_red, joins_red = _gateway_joins([admission, delivered], final)
    assert ev_red["valid"] is True
    assert joins_red == []

    # GREEN (fix): the committed row (from the real producer) closes the exact join, and
    # the admission row still does NOT join (its pre-render seal never matches).
    committed = _committed_row_via_producer(env, final)
    ev_green, joins_green = _gateway_joins([admission, committed, delivered], final)
    assert ev_green["valid"] is True
    assert len(joins_green) == 1
    join = joins_green[0]
    assert join["candidate_id"] == env.dedup_key
    assert join["decision_site"] == "mini_seam.gateway.candidate_committed"
    assert join["delivery_row_index"] == 2


# --------------------------------------------------------------------------- #
# Chronology: control iteration must be <= delivery iteration.
# --------------------------------------------------------------------------- #
def test_join_refuses_a_control_after_its_delivery() -> None:
    env = _env()
    final = _shipped(env)
    delivered = _delivered_row(env, final, iteration=ITER)

    committed_late = _committed_row_via_producer(env, final)
    committed_late["iteration"] = ITER + 1  # control AFTER delivery
    _ev, joins_late = _gateway_joins([committed_late, delivered], final)
    assert joins_late == []

    committed_ok = _committed_row_via_producer(env, final)  # iteration == ITER
    assert committed_ok["iteration"] == ITER
    _ev2, joins_ok = _gateway_joins([committed_ok, delivered], final)
    assert len(joins_ok) == 1


# --------------------------------------------------------------------------- #
# A forged seal / wrong candidate id cannot fake the join (contract stays strict).
# --------------------------------------------------------------------------- #
def test_forged_seal_or_wrong_candidate_id_cannot_fake_a_join() -> None:
    env = _env()
    final = _shipped(env)
    delivered = _delivered_row(env, final)

    forged_seal = _committed_row_via_producer(env, final)
    forged_seal["candidate_sha256_16"] = "0" * 16  # right chars, wrong seal
    _ev, joins_forged = _gateway_joins([forged_seal, delivered], final)
    assert joins_forged == []

    wrong_id = _committed_row_via_producer(env, final)
    wrong_id["candidate_id"] = "gateway:forged-id"
    _ev2, joins_wrong = _gateway_joins([wrong_id, delivered], final)
    assert joins_wrong == []


# --------------------------------------------------------------------------- #
# Producer + contract invariants.
# --------------------------------------------------------------------------- #
def test_gateway_contract_declares_both_admission_and_committed_sites() -> None:
    sites = control_contract("GT_GATEWAY").decision_sites
    assert "gateway.augment.candidate_admission" in sites
    assert "mini_seam.gateway.candidate_committed" in sites


def test_producer_is_correct_or_quiet_without_bytes_recorder_or_registration() -> None:
    env = _env()
    calls: list = []

    def recorder(*args, **kwargs):
        calls.append((args, kwargs))

    gateway.record_committed_delivery(env, "", recorder)          # nothing shipped
    gateway.record_committed_delivery(env, "x", None)             # no recorder
    gateway.record_committed_delivery(
        _env("totally_unregistered_kind"), "x", recorder)         # unregistered class
    assert calls == []
