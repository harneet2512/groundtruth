"""Metric-honesty fixes (Cluster-5 ITEM 1 D2 + ITEM 2 D3-D5, 2026-07-18).

D3-D5: three gold-ratio metrics fabricated a real-looking ``0.0`` when the gold denominator was
absent (``if n_gold else 0.0``). A reader could not distinguish "0% recall" from "UNMEASURED — no
gold". They now return ``None`` (UNMEASURED), matching every sibling gold-ratio metric.

D2: wasted_token_rate is a STEP proxy (no per-step token attribution exists in the trajectory), so
its former reachable ``else 0.0`` fabricated 0% waste on an empty timeline. It now returns ``None``
and carries a step-proxy basis; gt_feature_metrics stamps its formula_provenance as a step proxy so
it is never mislabeled as the MEASURED §9 token formula. D5 discloses gt_token_overhead's chars/4
token estimate; D4 gives task-scope rows a REAL denominator_provenance, not ``mandatory_contract:*``.

RED-first: on the pre-fix tree each asserted value below was ``0.0`` (not ``None``), and
denominator_provenance was ``mandatory_contract:distribution``.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gt_feature_metrics as fm  # noqa: E402
import gt_performance_metrics as pm  # noqa: E402


# ── D3/D4/D5: gold-ratio metrics UNMEASURED (None), never a fabricated 0.0 ────────────────────
def test_localization_recall_and_navigation_unmeasured_without_gold() -> None:
    loc = pm._compute_localization(timeline=[], gold_files=[], brief_txt="")
    assert loc["localization_recall"] is None  # D3 (was 0.0)
    assert loc["navigation_directness"] is None  # D4 (was 0.0)


def test_scope_coverage_unmeasured_without_gold() -> None:
    scope = pm._compute_scope_completeness(timeline=[], gold_files=[])
    assert scope["scope_coverage"] is None  # D5 (was 0.0)


# ── D2: wasted_token_rate step proxy — None on an empty timeline, basis disclosed ─────────────
def test_wasted_token_rate_unmeasured_on_empty_timeline() -> None:
    tok = pm._compute_token_efficiency(trajectory={}, timeline=[], gold_files=[])
    assert tok["wasted_token_rate"] is None  # was a fabricated 0.0
    assert tok["_wasted_token_rate_basis"].startswith("step_proxy")
    assert tok["_gt_token_overhead_basis"] == "chars_over_4_token_estimate"
    assert tok["_non_idle_step_count"] == 0


# ── D2/D5: gt_feature_metrics provenance names the proxy/estimate, not the token formula ──────
def test_wasted_token_rate_formula_provenance_names_the_step_proxy() -> None:
    formula, denom = fm._perf_provenance("token_efficiency", "wasted_token_rate")
    assert "STEP-PROXY" in formula and "NOT the" in formula
    assert "MANDATORY_METRICS.md#" not in formula  # must NOT masquerade as the measured formula
    assert "_non_idle_step_count" in denom


def test_gt_token_overhead_formula_provenance_discloses_chars_over_4_estimate() -> None:
    formula, denom = fm._perf_provenance("token_efficiency", "gt_token_overhead")
    assert "chars/4" in formula and "ESTIMATE" in formula
    assert denom == "gt_deep_metrics:performance.token_efficiency.total_tokens_in"


# ── D4: task-scope denominator_provenance is a REAL source, never mandatory_contract:* ────────
def test_task_scope_denominator_provenance_is_real_not_mandatory_contract() -> None:
    # A gold-ratio row names its real underscore/field denominator.
    _f, denom = fm._perf_provenance("localization", "localization_recall")
    assert denom == "gt_deep_metrics:performance.localization.n_gold_files"
    # And the DEFAULT (an un-overridden metric) still names a real deep-metrics source, never the
    # fabricated "mandatory_contract:<type>" pointer the audit (D4) called out.
    _f2, denom2 = fm._perf_provenance("localization", "localization_precision")
    assert denom2.startswith("gt_deep_metrics:performance.")
    assert not denom2.startswith("mandatory_contract")
