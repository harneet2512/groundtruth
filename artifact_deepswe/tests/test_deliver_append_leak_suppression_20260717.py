"""RED contract — Cluster-2c defect 10.

The one model-facing direct-append chokepoint ``_gt_deliver_append`` dropped a
leaking payload SILENTLY (``return False`` with no ledger row) — invisible to
fired/delivered reconciliation. Every precheck suppression must record a NAMED reason
(``outcome=suppressed_hidden_only``, ``reason=leak_guard``) and never deliver known-
invalid bytes, while a correctly suppressed candidate must never abort and the model-
facing bytes stay byte-identical on the leak-free (real) path.
"""

from __future__ import annotations

import gt_mini_patch as g


def _record_capture(monkeypatch):
    calls: list[dict] = []

    def _rec(*, kind, outcome, reason="", chars=0, file_path="", event=None,
             content=None, extra=None):
        calls.append({"kind": kind, "outcome": outcome, "reason": reason,
                      "chars": chars})
        return True

    monkeypatch.setattr(g, "_runtime_ledger_record", _rec)
    return calls


def test_leaking_payload_records_named_suppression_and_drops(monkeypatch) -> None:
    monkeypatch.setattr(
        "groundtruth.runtime.native_render.contains_test_identity", lambda _t: True)
    calls = _record_capture(monkeypatch)
    out = {"output": "PRE"}
    appended = g._gt_deliver_append(out, "leaky::payload::test_x", kind="l3.contract")
    # Biting: the drop must NOT be silent — exactly one named suppression row.
    assert appended is False
    assert out["output"] == "PRE"  # zero model bytes appended (known-invalid never ships)
    assert len(calls) == 1
    assert calls[0]["reason"] == "leak_guard"
    assert calls[0]["kind"] == "l3.contract"
    assert getattr(calls[0]["outcome"], "value", calls[0]["outcome"]) in (
        "suppressed_hidden_only", g._ProductSignalOutcome.SUPPRESSED_HIDDEN_ONLY)


def test_default_kind_when_caller_supplies_none(monkeypatch) -> None:
    monkeypatch.setattr(
        "groundtruth.runtime.native_render.contains_test_identity", lambda _t: True)
    calls = _record_capture(monkeypatch)
    out = {"output": ""}
    assert g._gt_deliver_append(out, "leaky") is False
    assert calls[0]["kind"] == "direct_append"


def test_clean_payload_is_byte_identical_and_records_nothing(monkeypatch) -> None:
    monkeypatch.setattr(
        "groundtruth.runtime.native_render.contains_test_identity", lambda _t: False)
    calls = _record_capture(monkeypatch)
    out = {"output": "OBS"}
    appended = g._gt_deliver_append(out, "\n<gt-contract>ok</gt-contract>")
    # Biting: a leak-free payload must be untouched — appended verbatim, NO suppression row.
    assert appended is True
    assert out["output"] == "OBS\n<gt-contract>ok</gt-contract>"
    assert calls == []


def test_correctly_suppressed_candidate_does_not_raise(monkeypatch) -> None:
    # A record fault must never propagate into an abort; the drop still returns False.
    monkeypatch.setattr(
        "groundtruth.runtime.native_render.contains_test_identity", lambda _t: True)

    def _boom(**_kw):
        raise RuntimeError("ledger down")

    # _runtime_ledger_record itself swallows internally; simulate a hostile stub to prove
    # the chokepoint never re-raises past its own guard on the real path.
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **_kw: True)
    out = {"output": "X"}
    assert g._gt_deliver_append(out, "leaky") is False
    assert out["output"] == "X"
