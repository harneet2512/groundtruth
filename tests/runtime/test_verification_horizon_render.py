"""Render-level tests for the verification-horizon emission: the structural risk_note
is appended, and the benchmark-shaped 'submit/submission' wording is gone (legitimacy
fix — a product agent finalizes/commits, it does not 'submit a patch')."""

from __future__ import annotations

from groundtruth.runtime.verification_horizon import render_verify_emission


def test_risk_note_is_appended_to_the_body():
    out = render_verify_emission(
        "advisory",
        5,
        100,
        {"pkg/a.py"},
        None,
        risk_note="set_fields (7 verified caller(s) in the graph) — no test has exercised it",
    )
    assert "Highest-impact unverified change: set_fields (7 verified caller(s)" in out


def test_no_risk_note_leaves_body_unchanged():
    out = render_verify_emission("advisory", 5, 100, {"pkg/a.py"}, None)
    assert "Highest-impact unverified change" not in out
    assert out.startswith('\n<gt-verify level="advisory">')


def test_no_benchmark_submit_wording_in_any_band():
    for band in ("advisory", "urgent", "gate", "pivot"):
        low = render_verify_emission(band, 90, 100, {"pkg/a.py"}, None).lower()
        assert "submit unverified" not in low, band
        assert "unverified submission" not in low, band
        assert "submit the minimal" not in low, band


def test_product_advisory_is_budget_free_text():
    # The advisory band is the ONLY one that fires on a product surface; it must never
    # mention steps/budget.
    low = render_verify_emission("advisory", 5, 100, {"pkg/a.py"}, None).lower()
    assert "step" not in low and "budget" not in low


def test_unknown_band_is_empty():
    assert render_verify_emission("none", 5, 100, {"pkg/a.py"}, None) == ""
