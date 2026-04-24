"""Canonical mechanism-rate formulas for GT benchmark plumbing.

One source of truth for the reader/writer contract. Historical drift where
`gt_canary_report.arm_summary()` stopped emitting `delivery_rate` /
`engagement_rate` while `verify_report.compute()` still read them as
pre-computed keys caused smoke v5 to FAIL with both rates at 0.0 even though
raw chain counters were healthy. Both sides now import from here.

Formula source: historical PASS artifact
benchmarks/swebench/baseline_confirm_nolsp/gt_arm_summary.json (delivery=0.8
from 16/20, engagement=0.9375 from 15/16) + scripts/swebench/gt_finalization.py
lines 469-470. If any of these definitions changes, change them here only.
"""
from __future__ import annotations

SCHEMA_INVALID = "schema_invalid"

# name -> (numerator_key, denominator_key) in a summary dict.
# Gate thresholds live in scripts/swebench/verify_report.py; values here are
# purely definitional.
MECHANISM_RATES = {
    "delivery_rate": ("steer_delivered_total", "ack_armed_total"),
    "engagement_rate": ("ack_engagement_total", "steer_delivered_total"),
}


def _num_or_none(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_rate(summary: dict, rate_key: str) -> float | None:
    """Compute one mechanism rate from raw totals in a summary dict.

    Precedence:
      1. If summary already has a valid pre-computed `rate_key`, return it.
      2. Otherwise derive from (numerator / denominator) using the canonical
         mapping.
      3. If either side is missing or the denominator is zero, return None
         (caller should surface as schema_invalid, NOT silently zero).
    """
    pre = _num_or_none(summary.get(rate_key))
    if pre is not None:
        return pre
    if rate_key not in MECHANISM_RATES:
        return None
    num_key, den_key = MECHANISM_RATES[rate_key]
    num = _num_or_none(summary.get(num_key))
    den = _num_or_none(summary.get(den_key))
    if num is None or den is None or den == 0:
        return None
    return num / den


def mechanism_rates(summary: dict) -> dict:
    """Return all canonical mechanism rates from a summary dict.

    Values are floats when derivable, None when the schema is invalid for
    that rate. The writer should emit the float values; the reader should
    surface None as schema_invalid rather than coercing to 0.0.
    """
    return {k: compute_rate(summary, k) for k in MECHANISM_RATES}
