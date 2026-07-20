"""D-L (run6 29714439700, haystack-8997 iter25): a gateway winner whose
evidence_type is NOT one the gateway edit-fact factory can attest (a
GT_LOC_RESLOT reactive-localization `trace_frame`, which carries no gateway
ProducerInputs sidecar) hit build_gateway_attestation -> bare ValueError
("unsupported Gateway evidence type: 'trace_frame'") -> a SPURIOUS
attestation.persist measurement_failed row.

Absence of a gateway attestation for a localization trace_frame is
expected-by-design (its proof is the brief-level source_contribution
attestation, not this factory). It is NO-OPPORTUNITY, not a persistence FAILURE.
Fix: guard on the factory's own _SUPPORTED set BEFORE build. A SUPPORTED type
with genuinely broken inputs must STILL record measurement_failed (not swallowed).
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for p in (_REPO / "artifact_deepswe", _REPO / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gt_mini_patch as g  # noqa: E402


def _winner(producer: str, evidence_type: str):
    return SimpleNamespace(
        producer=producer, evidence_type=evidence_type,
        producer_inputs=None, dedup_key="k", lineage=None)


def test_trace_frame_gateway_is_no_opportunity_not_failure(monkeypatch):
    captured = []
    monkeypatch.setattr(g, "_attestation_persist_failure_row",
                        lambda *a, **k: captured.append((a, k)))
    sealed = SimpleNamespace(rendered_bytes_hash="0" * 32)
    out = g._persist_gateway_producer_attestation(
        _winner("trace", "trace_frame"), "shipped bytes", sealed)
    assert out is None
    assert captured == [], (
        "a non-attestable trace_frame must NOT record a measurement_failed row")


def test_supported_type_with_broken_inputs_still_fails(monkeypatch):
    # caller_break IS in _SUPPORTED but producer_inputs=None -> a GENUINE failure
    # that MUST still be recorded (the guard must not over-swallow).
    captured = []
    monkeypatch.setattr(g, "_attestation_persist_failure_row",
                        lambda *a, **k: captured.append((a, k)))
    sealed = SimpleNamespace(rendered_bytes_hash="0" * 32)
    out = g._persist_gateway_producer_attestation(
        _winner("caller_contract", "caller_break"), "shipped", sealed)
    assert out is None
    assert len(captured) == 1, (
        "a SUPPORTED type with broken inputs MUST still record measurement_failed")
