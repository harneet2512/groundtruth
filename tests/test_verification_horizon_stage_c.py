"""Stage C — Verification Horizon H0+H1+H2 (red->green).

Verifies:
1. H0: budget sensor reads GT_STEP_LIMIT, computes B/R, V estimation
2. H1: covering-test query (reuses Stage B _covering_tests_for_symbols)
3. H2: the 4-band decision function (DORMANT/ADVISORY/URGENT/GATE/PIVOT)
4. Dose caps: advisory once, gate cap-3
5. Scale-free: thresholds are fractions of step_limit
6. Correct-or-quiet: no step_limit -> disabled, no edits -> silent
7. Product rendering: the <gt-verify> tag with correct level attribute
8. Boa replay: at step 166/300, budget 55% consumed, should be ADVISORY
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"


# FIX 5 (2026-06-11): the IN-CODE defaults are now corpus-derived (see
# tests/test_esc_calibration.py + scripts/calibrate_esc_bands.py). This suite
# tests the band STRUCTURE (predicate shapes, ordering, latches, rendering),
# so it PINS the historical reference thresholds through the env calibration
# channel — exactly the channel's stated purpose ("a calibrated value ships
# without a code change") — keeping the scenarios threshold-independent.
_REF_ESC = {
    "GT_ESC_ADV_TCOV": "0.5",
    "GT_ESC_ADV_B": "0.4",
    "GT_ESC_URG_TCOV": "0.3",
    "GT_ESC_URG_B": "0.7",
    "GT_ESC_GATE_TCOV": "0.5",
    "GT_ESC_GATE_KV": "2.0",
}


def _load_patch(step_limit: str = "300", vcc: str = "25"):
    """Load gt_mini_patch with specific env vars for the horizon."""
    env = {
        "GT_BASELINE": "1",
        "GT_STEP_LIMIT": step_limit,
        "GT_VERIFICATION_CYCLE_COST": vcc,
        **_REF_ESC,
    }
    prev = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    name = f"gt_mini_patch_stage_c_test_{step_limit}_{vcc}"
    try:
        spec = importlib.util.spec_from_file_location(name, _PATCH_PATH)
        assert spec and spec.loader
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
    return _load_patch("300", "25")


@pytest.fixture(scope="module")
def gmp_nolimit():
    """Module loaded with no step limit -> horizon disabled."""
    return _load_patch("", "25")


# ---------------------------------------------------------------------------
# Test H0: Budget sensor
# ---------------------------------------------------------------------------
class TestBudgetSensor:
    def test_step_limit_parsed(self, gmp):
        assert gmp._GT_STEP_LIMIT == 300

    def test_step_limit_absent_disables(self, gmp_nolimit):
        assert gmp_nolimit._GT_STEP_LIMIT is None

    def test_vcc_parsed_and_railed(self, gmp):
        assert gmp._GT_VERIFICATION_CYCLE_COST == 25

    def test_v_estimation_default(self, gmp):
        """No observed test cycles -> returns V_DEFAULT, railed."""
        gmp._test_cycle_spans.clear()
        v = gmp._estimate_v()
        assert v == 25
        assert gmp._V_MIN <= v <= gmp._V_MAX

    def test_v_estimation_from_observations(self, gmp):
        """With observed test cycles, uses median."""
        gmp._test_cycle_spans.clear()
        gmp._test_cycle_spans.extend([10, 12, 14])
        v = gmp._estimate_v()
        assert v == 12  # median of [10, 12, 14]
        gmp._test_cycle_spans.clear()

    def test_v_railing_min(self, gmp):
        """Observed spans below V_MIN are railed up."""
        gmp._test_cycle_spans.clear()
        gmp._test_cycle_spans.extend([2, 3, 4])
        v = gmp._estimate_v()
        assert v == gmp._V_MIN  # 8
        gmp._test_cycle_spans.clear()

    def test_v_railing_max(self, gmp):
        """Observed spans above V_MAX are railed down."""
        gmp._test_cycle_spans.clear()
        gmp._test_cycle_spans.extend([100, 200, 300])
        v = gmp._estimate_v()
        assert v == gmp._V_MAX  # 40
        gmp._test_cycle_spans.clear()


# ---------------------------------------------------------------------------
# Test H2: Band decision function — STAGE-4 contract (behavioral signals):
#   advisory : edit_coverage>0 (or None) AND test_coverage<0.5 AND B>0.4
#   urgent   : test_coverage<0.3 AND B>0.7
#   gate     : R < 2*V AND test_coverage<0.5
#   pivot    : last_test_failed in the critical zone
# Thresholds are the env-overridable GT_ESC_* calibration channel.
# ---------------------------------------------------------------------------
class TestBandDecision:
    def test_no_step_limit_returns_none(self, gmp):
        """No step_limit -> disabled (None)."""
        result = gmp.verify_horizon_band(
            action_count=150,
            step_limit=None,
            v=25,
            edit_coverage=None,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result is None

    def test_dormant_early_budget(self, gmp):
        """B <= 0.4 -> dormant even with zero coverage (the event-driven
        family owns the early trajectory)."""
        result = gmp.verify_horizon_band(
            action_count=100,
            step_limit=300,
            v=25,
            edit_coverage=0.5,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result is None

    def test_advisory_mid_budget_low_coverage(self, gmp):
        """B>0.4 with test_coverage<0.5 and edit_coverage>0 -> advisory."""
        result = gmp.verify_horizon_band(
            action_count=160,
            step_limit=300,
            v=25,
            edit_coverage=0.5,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result == "advisory"

    def test_advisory_suppressed_when_covered(self, gmp):
        """test_coverage >= 0.5 -> no advisory (the agent IS verifying)."""
        result = gmp.verify_horizon_band(
            action_count=160,
            step_limit=300,
            v=25,
            edit_coverage=0.5,
            test_coverage=0.6,
            has_edits=True,
        )
        assert result is None

    def test_advisory_requires_edit_coverage_when_obligations(self, gmp):
        """With obligations present and NONE edited, the advisory clause
        stays quiet — the obligation-status class owns that story."""
        result = gmp.verify_horizon_band(
            action_count=160,
            step_limit=300,
            v=25,
            edit_coverage=0.0,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result is None

    def test_advisory_no_obligations_degrades_to_edit_presence(self, gmp):
        """edit_coverage=None (no obligations) -> edit presence stands in."""
        result = gmp.verify_horizon_band(
            action_count=160,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result == "advisory"

    def test_urgent_late_budget(self, gmp):
        """B>0.7 with test_coverage<0.3 -> urgent."""
        result = gmp.verify_horizon_band(
            action_count=220,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=0.2,
            has_edits=True,
        )
        assert result == "urgent"

    def test_urgent_suppressed_at_partial_coverage(self, gmp):
        """test_coverage in [0.3, 0.5) at B>0.7: advisory-grade, not urgent."""
        result = gmp.verify_horizon_band(
            action_count=220,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=0.4,
            has_edits=True,
        )
        assert result == "advisory"

    def test_gate_critical_remaining(self, gmp):
        """R < 2*V with test_coverage<0.5 -> gate (keyed to observed pace)."""
        result = gmp.verify_horizon_band(
            action_count=255,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result == "gate"  # R=45 < 2*25=50

    def test_gate_suppressed_when_covered(self, gmp):
        result = gmp.verify_horizon_band(
            action_count=255,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=0.6,
            has_edits=True,
        )
        assert result is None

    def test_no_edits_dormant(self, gmp):
        """No edits -> no verification debt -> None (correct-or-quiet)."""
        result = gmp.verify_horizon_band(
            action_count=250,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=None,
            has_edits=False,
        )
        assert result is None

    def test_pivot_tested_but_failing(self, gmp):
        """Last observed outcome FAILED in the critical zone -> pivot."""
        result = gmp.verify_horizon_band(
            action_count=250,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=1.0,
            has_edits=True,
            last_test_failed=True,
        )
        assert result == "pivot"


# ---------------------------------------------------------------------------
# Test: Scale-free behavior across step limits (fractions of S, never steps)
# ---------------------------------------------------------------------------
class TestScaleFree:
    def test_s300_advisory_past_b04(self, gmp):
        result = gmp.verify_horizon_band(
            action_count=150,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result == "advisory"  # B=0.5 > 0.4

    def test_s100_gate_precedes_urgent(self, gmp):
        """At S=100/V=25 the gate zone (R<50) covers the urgent zone — gate
        wins because bands evaluate gate-first."""
        result = gmp.verify_horizon_band(
            action_count=63,
            step_limit=100,
            v=25,
            edit_coverage=None,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result == "gate"  # R=37 < 2*25

    def test_s100_advisory_at_45(self, gmp):
        result = gmp.verify_horizon_band(
            action_count=45,
            step_limit=100,
            v=10,
            edit_coverage=None,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result == "advisory"  # B=0.45 > 0.4; R=55 > 20

    def test_env_calibration_channel(self, gmp, monkeypatch):
        """The thresholds are env-overridable (H3 channel) — a calibrated
        value ships without a code change."""
        monkeypatch.setattr(gmp, "_ESC_ADV_B", 0.6, raising=False)
        result = gmp.verify_horizon_band(
            action_count=150,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result is None  # B=0.5 < the recalibrated 0.6
        monkeypatch.setattr(gmp, "_ESC_ADV_B", 0.4, raising=False)


# ---------------------------------------------------------------------------
# Test: Rendering
# ---------------------------------------------------------------------------
class TestRendering:
    def test_advisory_tag(self, gmp):
        block = gmp._render_verify_emission("advisory", 160, 300, {"src/monitor.py"}, [])
        assert '<gt-verify level="advisory">' in block
        assert "monitor.py" in block
        assert "</gt-verify>" in block

    def test_urgent_with_covering_test(self, gmp):
        covering = [
            {
                "name": "test_foo",
                "file": "tests/test_x.py",
                "confidence": 0.95,
                "run_cmd": "pytest tests/test_x.py::test_foo",
            }
        ]
        block = gmp._render_verify_emission("urgent", 245, 300, {"src/x.py"}, covering)
        assert '<gt-verify level="urgent">' in block
        assert "55" in block  # R = 300 - 245
        assert "test_foo" not in block
        assert "tests/test_x.py" not in block
        assert "pytest tests/test_x.py::test_foo" not in block
        assert "Run the covering test now" not in block
        assert "narrowest relevant repo test target" in block

    def test_gate_rendering(self, gmp):
        block = gmp._render_verify_emission("gate", 262, 300, {"src/a.py", "src/b.py"}, [])
        assert '<gt-verify level="gate">' in block
        assert "38" in block  # R = 300 - 262
        assert "LAST window" in block

    def test_pivot_rendering(self, gmp):
        block = gmp._render_verify_emission("pivot", 250, 300, {"src/a.py"}, [])
        assert '<gt-verify level="pivot">' in block
        assert "50" in block  # R
        assert "revert" in block


# ---------------------------------------------------------------------------
# Test: Dose caps
# ---------------------------------------------------------------------------
class TestDoseCaps:
    def test_advisory_fires_once(self, gmp):
        """Advisory should fire at most once per task."""
        gmp._horizon_advisory_fired = False
        gmp._horizon_gate_fire_count = 0
        gmp._GT_BASELINE = False  # enable for this test
        gmp._action_count = 160
        gmp._oracle_edited_rels.clear()
        gmp._oracle_edited_rels.add("src/foo.py")
        gmp._oracle_edited_tokens.clear()
        gmp._oracle_edited_tokens.add("some_func")
        gmp._oracle_tested_tokens.clear()

        # First call should produce a candidate
        result1 = gmp._verification_horizon_candidate()
        assert result1 is not None
        assert "advisory" in result1[1]

        # Second call should return None (dose cap)
        result2 = gmp._verification_horizon_candidate()
        assert result2 is None

        # Reset
        gmp._GT_BASELINE = True
        gmp._horizon_advisory_fired = False

    def test_gate_cap_three(self, gmp):
        """Gate should fire at most 3 times."""
        gmp._horizon_advisory_fired = True  # skip advisory
        gmp._horizon_gate_fire_count = 0
        gmp._GT_BASELINE = False
        gmp._action_count = 263
        gmp._oracle_edited_rels.clear()
        gmp._oracle_edited_rels.add("src/foo.py")
        gmp._oracle_edited_tokens.clear()
        gmp._oracle_edited_tokens.add("bar_func")
        gmp._oracle_tested_tokens.clear()

        # Should fire 3 times
        for i in range(3):
            result = gmp._verification_horizon_candidate()
            assert result is not None, f"gate should fire at fire {i + 1}"
            assert "gate" in result[1]

        # 4th time should be capped
        result = gmp._verification_horizon_candidate()
        assert result is None

        # Reset
        gmp._GT_BASELINE = True
        gmp._horizon_gate_fire_count = 0


# ---------------------------------------------------------------------------
# Test: Boa replay scenario
# ---------------------------------------------------------------------------
class TestBoaReplay:
    def test_boa_step_166_of_300_advisory(self, gmp):
        """Boa submitted at step ~166/300 without ever observing a test
        result (test_coverage=0). Budget 55% consumed -> ADVISORY fires."""
        result = gmp.verify_horizon_band(
            action_count=166,
            step_limit=300,
            v=25,
            edit_coverage=None,  # no obligations artifact in that run
            test_coverage=0.0,  # boa never observed a test result
            has_edits=True,
        )
        assert result == "advisory"
        # Product framing: at step 166/300, GT would tell the developer:
        # "you have edited source.rs but no test output observed so far
        # references these changes — consider running: cargo test"

    def test_boa_step_280_of_300_gate(self, gmp):
        """At step 280/300 with observed V=25, R=20 < 2*25=50 -> GATE."""
        result = gmp.verify_horizon_band(
            action_count=280,
            step_limit=300,
            v=25,
            edit_coverage=None,
            test_coverage=0.0,
            has_edits=True,
        )
        assert result == "gate"
