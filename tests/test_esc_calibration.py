"""FIX 5 (2026-06-11 measurement-gate closeout) — GT_ESC_* band edges are
DATA-DERIVED from the frozen oracle corpus, not invented placeholders.

Derivation: scripts/calibrate_esc_bands.py replayed the 9 frozen trajectories
of run 27321848581 (offline Stage-0 sensor, deterministic) and measured the
behavioral signals at the live REVIEW-transition predicate. n=9, 0/9 resolved
-> the outcome-stratified quantiles of the design are impossible; per the
fallback rule the edges are corpus medians/quartiles ("n=9, calibration-
quality placeholder" — Stage 6, gt_gt §15.4, owns refinement). Receipt:
.claude/reports/metrics/esc_calibration_*.json.

Red->green: the in-code defaults were 0.4/0.5/0.3/0.7/2.0 (invented); they
are now the corpus-derived numbers below, monotone, and still env-overridable
(the calibration channel is unchanged).
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"

# The corpus-derived edges (8-dp; see scripts/calibrate_esc_bands.py + receipt).
_EXPECTED = {
    "_ESC_ADV_TCOV": 0.125,  # median tcov@first-review (0.0 on 9/9), floored 1/8
    "_ESC_ADV_B": 0.11166666,  # p25 budget@first-review
    "_ESC_URG_TCOV": 0.125,  # p25 tcov@first-review, same floor (monotone)
    "_ESC_URG_B": 0.35333333,  # median budget@LAST-review
    "_ESC_GATE_TCOV": 1.0,  # median tcov@last-review (7/9 fully covered)
    "_ESC_GATE_KV": 7.48,  # median remaining-at-SUBMISSION / V
}
_OLD_PLACEHOLDERS = {
    "_ESC_ADV_TCOV": 0.5,
    "_ESC_ADV_B": 0.4,
    "_ESC_URG_TCOV": 0.3,
    "_ESC_URG_B": 0.7,
    "_ESC_GATE_TCOV": 0.5,
    "_ESC_GATE_KV": 2.0,
}


def _load_patch(name: str, env: dict[str, str] | None = None):
    prev = {
        k: os.environ.get(k) for k in ["GT_BASELINE", "GT_STEP_LIMIT"] + list((env or {}).keys())
    }
    os.environ["GT_BASELINE"] = "1"
    os.environ["GT_STEP_LIMIT"] = "300"
    for k, v in (env or {}).items():
        os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location(name, _PATCH_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for var, p in prev.items():
            if p is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = p


@pytest.fixture(scope="module")
def gmp():
    return _load_patch("gmp_esc_cal")


def test_defaults_are_corpus_derived_not_placeholders(gmp):
    for attr, want in _EXPECTED.items():
        got = getattr(gmp, attr)
        assert abs(got - want) < 1e-9, f"{attr} = {got!r}, expected the corpus-derived {want!r}"
        assert abs(got - _OLD_PLACEHOLDERS[attr]) > 1e-9, (
            f"{attr} still equals the invented placeholder"
        )


def test_edges_monotone(gmp):
    # urgent must not be EASIER than advisory on either axis.
    assert gmp._ESC_URG_TCOV <= gmp._ESC_ADV_TCOV
    assert gmp._ESC_ADV_B <= gmp._ESC_URG_B
    # every tcov edge is functional (strict `tcov < edge` — 0.0 is a dead band)
    for attr in ("_ESC_ADV_TCOV", "_ESC_URG_TCOV", "_ESC_GATE_TCOV"):
        assert getattr(gmp, attr) > 0.0


def test_env_calibration_channel_still_wins():
    mod = _load_patch("gmp_esc_cal_env", {"GT_ESC_ADV_B": "0.4"})
    assert abs(mod._ESC_ADV_B - 0.4) < 1e-9


def test_defaults_match_committed_receipt():
    receipts = sorted(
        glob.glob(str(_ROOT / ".claude" / "reports" / "metrics" / "esc_calibration_*.json"))
    )
    if not receipts:
        pytest.skip("no calibration receipt on this checkout")
    data = json.loads(Path(receipts[-1]).read_text(encoding="utf-8"))
    gmp = _load_patch("gmp_esc_cal_receipt")
    for env_key, want in data["edges"].items():
        attr = "_" + env_key.removeprefix("GT_")
        assert abs(getattr(gmp, attr) - float(want)) < 1e-9, (
            f"{attr} diverges from the committed receipt {receipts[-1]}"
        )


def test_band_ladder_orders_with_new_edges(gmp):
    """Sanity of the band semantics under the derived edges: a fully
    uncovered editor moves DORMANT -> advisory -> urgent -> gate as the
    budget burns (S=300, V=25; gate zone starts at R < 7.48*25 = 187)."""

    def band(a):
        return gmp.verify_horizon_band(
            action_count=a,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=0.0,
            has_edits=True,
        )

    assert band(20) is None  # B=0.067 < ADV_B
    assert band(60) == "advisory"  # B=0.2 >= 0.1117, R=240 > 187
    assert band(140) == "gate"  # R=160 < 187 (inside typical submit zone)
    # urgent occupies the slice between advisory and the gate zone
    assert band(110) == "urgent"  # B=0.367 >= 0.3533, R=190 > 187

    # covered editor stays dormant until the gate's below-typical clause
    def band_cov(a, tc):
        return gmp.verify_horizon_band(
            action_count=a,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=tc,
            has_edits=True,
        )

    assert band_cov(110, 0.6) is None  # above both tcov edges, outside gate zone
    assert band_cov(290, 1.0) is None  # fully covered: NO band ever fires
