from __future__ import annotations

import hashlib

import gt_mini_patch as g


def _exact_submit_extra(candidate_id: str) -> dict:
    extra = g._registered_delivery_extra(
        "submit_gate", "submit_refusal", "submit",
    )
    extra["candidate_id"] = candidate_id
    return extra


def _control_rows(rows: list[dict]) -> list[dict]:
    return [
        row for row in rows
        if row.get("control_ref", {}).get("feature_id") == "GT_SS_ACK_METRICS"
    ]


def test_ack_control_uses_final_boundary_joined_delivery_identity(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setenv("GT_SS_ACK_METRICS", "1")
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(row) or True)
    monkeypatch.setattr(g, "_action_count", 4)
    g._ss_pending_acks.clear()
    semantic = "src/widget.py:4: error: update call site"
    shipped = "\n" + semantic
    candidate_id = "submit-refusal:exact-final"

    g._ss_record_delivered(
        "submit_refusal",
        semantic,
        terminal_text=shipped,
        delivery_extra=_exact_submit_extra(candidate_id),
    )
    monkeypatch.setattr(g, "_action_count", 5)
    g._ss_scan_acks("I will update src/widget.py before submitting.")

    controls = _control_rows(rows)
    assert len(controls) == 1
    control = controls[0]
    assert control["participation_decision"] == "APPLIED"
    assert control["candidate_id"] == candidate_id
    assert control["fact_class"] == "submit_refusal"
    assert control["related_delivery_iteration"] == 4
    assert control["iteration"] == 5
    assert control["candidate_chars"] == len(shipped)
    assert control["candidate_sha256_16"] == hashlib.sha256(
        shipped.encode("utf-8", "surrogatepass")
    ).hexdigest()[:16]


def test_ack_expiry_records_no_effect_for_same_exact_delivery(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setenv("GT_SS_ACK_METRICS", "1")
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(row) or True)
    monkeypatch.setattr(g, "_action_count", 10)
    g._ss_pending_acks.clear()
    shipped = "\nsrc/widget.py:4: error: update call site"
    candidate_id = "submit-refusal:expires"
    g._ss_record_delivered(
        "submit_refusal",
        shipped.lstrip("\n"),
        terminal_text=shipped,
        delivery_extra=_exact_submit_extra(candidate_id),
    )

    monkeypatch.setattr(g, "_action_count", 11 + g._SS_ACK_WINDOW)
    g._ss_scan_acks("continuing without naming the delivered constraint")

    controls = _control_rows(rows)
    assert len(controls) == 1
    assert controls[0]["participation_decision"] == "NO_EFFECT"
    assert controls[0]["candidate_id"] == candidate_id
    assert controls[0]["related_delivery_iteration"] == 10


def test_untyped_delivery_keeps_legacy_ack_but_cannot_mint_typed_control(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setenv("GT_SS_ACK_METRICS", "1")
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(row) or True)
    monkeypatch.setattr(g, "_action_count", 20)
    g._ss_pending_acks.clear()
    text = "src/widget.py:4: warning: inspect this caller"
    g._ss_record_delivered(
        "legacy", text, terminal_text=text, delivery_extra=None,
    )
    monkeypatch.setattr(g, "_action_count", 21)
    g._ss_scan_acks("I will inspect src/widget.py now.")

    assert any(row.get("event_type") == "ack" for row in rows)
    assert _control_rows(rows) == []
