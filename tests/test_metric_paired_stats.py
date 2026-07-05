"""Measurement-integrity pins for scripts/metrics/compute_paired_metrics.py (G9).

Two SOTA-gap fixes: the flip significance uses McNemar on discordant pairs (not a
binomial vs a p=0.5 null that can never fire), and the per-metric battery is
Holm-corrected before any significant_p05 is trusted true (raw p<0.05 across ~15
tests => FWER ~54%).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "scripts" / "metrics" / "compute_paired_metrics.py"
_spec = importlib.util.spec_from_file_location("compute_paired_metrics", _MOD)
assert _spec and _spec.loader
cpm = importlib.util.module_from_spec(_spec)
# Register before exec: the module defines a module-level @dataclass, whose
# KW_ONLY probe reads sys.modules[cls.__module__] at class-creation time.
sys.modules[_spec.name] = cpm
_spec.loader.exec_module(cpm)


# --------------------------------------------------------------------------- G9a
def test_flips_significance_uses_mcnemar_not_binomial() -> None:
    out = cpm._flips_mcnemar(flip_count=6, regression_count=1, n_eligible=200)
    assert out["test"] == "mcnemar_discordant_pairs"
    # McNemar operates on the discordant cells: b=regressions, c=flips
    assert out["b"] == 1 and out["c"] == 6
    assert out["flips_observed"] == 6 and out["regressions_observed"] == 1
    # the p-value reflects the 6-vs-1 asymmetry, NOT 6/200 vs 0.5
    assert 0.0 <= out["p_value"] <= 1.0
    assert out["p_value"] < 0.5  # asymmetric -> some evidence


def test_flips_no_discordant_pairs_is_not_significant() -> None:
    out = cpm._flips_mcnemar(flip_count=0, regression_count=0, n_eligible=50)
    assert out["significant_p05"] is False
    assert out.get("note") == "no_discordant_pairs"


# --------------------------------------------------------------------------- G9b
def test_holm_correction_kills_naive_battery_significance() -> None:
    """15 tests each raw-p just under 0.05 must NOT all remain significant after
    Holm — that is precisely the family-wise inflation the fix controls."""
    tests = {
        f"m{i:02d}": {"p_value": 0.049, "significant_p05": True}
        for i in range(15)
    }
    cpm._apply_holm(tests)
    n_sig = sum(1 for k, t in tests.items()
                if k != "_holm" and isinstance(t, dict) and t.get("significant_p05"))
    # raw: all 15 were "significant"; Holm at 0.049 vs 0.05/15..0.05/1 rejects none
    assert n_sig == 0
    # provenance preserved: the raw decision is retained
    assert all(t.get("significant_p05_raw") is True
               for k, t in tests.items() if k != "_holm")
    assert tests["_holm"]["method"] == "holm_bonferroni_step_down"


def test_holm_keeps_a_genuinely_tiny_p() -> None:
    """One dominant tiny p-value survives Holm; the marginal ones do not."""
    tests = {"strong": {"p_value": 1e-9, "significant_p05": True}}
    tests.update({f"weak{i}": {"p_value": 0.04, "significant_p05": True} for i in range(14)})
    cpm._apply_holm(tests)
    assert tests["strong"]["significant_p05"] is True   # 1e-9 <= 0.05/15
    assert all(tests[f"weak{i}"]["significant_p05"] is False for i in range(14))


def test_holm_ignores_entries_without_numeric_p() -> None:
    tests = {"nan_one": {"p_value": None, "significant_p05": False},
             "real": {"p_value": 0.001, "significant_p05": True}}
    cpm._apply_holm(tests)
    assert tests["nan_one"]["significant_p05"] is False  # untouched
    assert tests["real"]["significant_p05"] is True      # 0.001 <= 0.05/1
