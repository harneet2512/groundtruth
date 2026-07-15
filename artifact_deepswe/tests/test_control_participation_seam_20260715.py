from __future__ import annotations

import hashlib

import gt_mini_patch as g


def _rows(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    return rows


def test_novelty_and_dedup_emit_only_when_their_decisions_execute(monkeypatch) -> None:
    rows = _rows(monkeypatch)
    monkeypatch.setattr(g, "_ss_provenance_on", lambda: False)
    monkeypatch.setattr(g, "_ss_late_drop_on", lambda: False)
    monkeypatch.setattr(g, "_ss_dedup2_on", lambda: True)
    monkeypatch.setattr(g, "_ss_dedup2_suppresses", lambda *a, **k: False)
    monkeypatch.setattr(g, "_ss_novelty_on", lambda: True)
    monkeypatch.setattr(g, "_ss_novelty_suppresses", lambda *a, **k: True)

    text = "[VERIFIED] src/pkg.py:7 keeps Thing.call()"
    assert g._ss_content_decision("l3.contract", text) == (True, "ss_step_behind")

    control_rows = [r for r in rows if r.get("layer") == "control.participation"]
    assert [r["control_ref"]["feature_id"] for r in control_rows] == [
        "GT_SS_DEDUP2", "GT_SS_NOVELTY",
    ]
    assert [r["participation_decision"] for r in control_rows] == [
        "NO_EFFECT", "APPLIED",
    ]
    assert {r["candidate_sha256_16"] for r in control_rows} == {
        hashlib.sha256(text.encode()).hexdigest()[:16]
    }
    assert all("feature_ids" not in r for r in control_rows)


def test_precedence_does_not_claim_controls_that_never_executed(monkeypatch) -> None:
    rows = _rows(monkeypatch)
    monkeypatch.setattr(g, "_ss_provenance_on", lambda: False)
    monkeypatch.setattr(g, "_ss_late_drop_on", lambda: True)
    monkeypatch.setattr(g, "_ss_late_drop_suppresses", lambda *a, **k: True)
    monkeypatch.setattr(g, "_ss_dedup2_on", lambda: True)
    monkeypatch.setattr(g, "_ss_novelty_on", lambda: True)

    assert g._ss_content_decision("spec.obligation", "subject_name") == (
        True, "ss_late")
    control_rows = [r for r in rows if r.get("layer") == "control.participation"]
    assert [r["control_ref"]["feature_id"] for r in control_rows] == [
        "GT_SS_LATE_DROP"
    ]


def test_control_metrics_are_byte_neutral_and_flag_off_is_silent(monkeypatch) -> None:
    rows = _rows(monkeypatch)
    monkeypatch.setattr(g, "_ss_provenance_on", lambda: False)
    monkeypatch.setattr(g, "_ss_late_drop_on", lambda: False)
    monkeypatch.setattr(g, "_ss_dedup2_on", lambda: False)
    monkeypatch.setattr(g, "_ss_novelty_on", lambda: False)
    text = "exact model-visible bytes"
    before = text.encode()
    assert g._ss_content_decision("l3.contract", text) == (False, "")
    assert text.encode() == before
    assert not [r for r in rows if r.get("layer") == "control.participation"]


def test_command_prefix_and_relatedness_bind_actual_decision_sites(monkeypatch) -> None:
    rows = _rows(monkeypatch)
    monkeypatch.setenv("GT_SS_ELIGIBILITY", "1")
    assert g._strip_leading_cd_prefix(
        "cd $(cat /tmp/root) && grep Widget src"
    ) == "grep Widget src"

    monkeypatch.setattr(g, "_d7_relatedness_on", lambda: True)
    monkeypatch.setattr(g, "_pending_delivery", [
        ("l3.contract", 1, "src/widget.py")
    ])
    g._ledger_judge_pending("sed -i s/a/b/ src/widget.py")

    decisions = {
        row["control_ref"]["feature_id"]: row["participation_decision"]
        for row in rows if row.get("layer") == "control.participation"
    }
    assert decisions["GT_SS_ELIGIBILITY"] == "APPLIED"
    assert decisions["GT_D7_RELATEDNESS"] == "APPLIED"


def test_shadow_assignment_binds_exact_would_be_render(monkeypatch) -> None:
    rows = _rows(monkeypatch)
    monkeypatch.setattr(g, "_ss_shadow_on", lambda: True)
    monkeypatch.setattr(g, "_ss_shadow_rate", lambda: "0")
    text = "[VERIFIED] exact candidate"
    assert g._ss_shadow_would_withhold("l3.contract", "key", text) is False
    row = next(r for r in rows if r.get("layer") == "control.participation")
    assert row["control_ref"] == {
        "category": "CAP", "feature_id": "GT_SS_SHADOW", "role": "eligibility",
    }
    assert row["participation_decision"] == "NO_EFFECT"
    assert row["candidate_sha256_16"] == hashlib.sha256(text.encode()).hexdigest()[:16]


def test_error_decision_is_not_graded_as_evaluated(monkeypatch) -> None:
    rows = _rows(monkeypatch)
    g._control_participation_record(
        "GT_SS_EXEC_TRUTH",
        "mini_seam.covering_selection.runner_eligibility",
        "ERROR",
        reason="runner_filter_error:OSError",
    )
    row = next(r for r in rows if r.get("layer") == "control.participation")
    assert row["participation_decision"] == "ERROR"
    assert row["outcome"] == "measurement_failed"


def test_shadow_applied_is_recorded_only_after_holdout_row(monkeypatch) -> None:
    rows = _rows(monkeypatch)
    monkeypatch.setattr(g, "_ss_shadow_on", lambda: True)
    monkeypatch.setattr(g, "_ss_shadow_rate", lambda: "1")
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kwargs: True)
    text = "[VERIFIED] exact holdout candidate"
    assert g._ss_shadow_withheld("l3.contract", "key", text) is True
    row = next(r for r in rows if r.get("layer") == "control.participation")
    assert row["participation_decision"] == "APPLIED"


def test_shadow_write_failure_fails_open_and_records_error(monkeypatch) -> None:
    rows = _rows(monkeypatch)
    monkeypatch.setattr(g, "_ss_shadow_on", lambda: True)
    monkeypatch.setattr(g, "_ss_shadow_rate", lambda: "1")
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kwargs: False)
    text = "[VERIFIED] exact holdout candidate"
    assert g._ss_shadow_withheld("l3.contract", "key", text) is False
    row = next(r for r in rows if r.get("layer") == "control.participation")
    assert row["participation_decision"] == "ERROR"
    assert row["outcome"] == "measurement_failed"
