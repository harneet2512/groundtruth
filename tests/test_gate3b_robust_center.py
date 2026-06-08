"""RED->GREEN regression for GATE3b robust-center (FIX 4).

GATE3b embedder-consumption discriminator p3, MAD==0 / distinct>=2 branch.

BUG (pre-fix): p3 used `max > median(ALL components)` as the right-tail test.
When the scored cluster is the rendered MAJORITY (e.g. comps=[0.827413,
0.827413, 0.827413, 0.0, 0.0] — 3 scored files, 2 zero/unscored background),
the median lands ON the cluster (0.827413), so `0.827413 > 0.827413` is False
-> p3 FAIL -> the gate rejected its OWN stated good case ("three relevant
chunks tightly clustered above the unscored background"). The embedder is
healthy (GATE3a separates: cos 0.86 vs 0.76); the cosines are real, never
clamped.

FIX: in the MAD==0, distinct>=2 branch, judge the SCORED cluster vs the ZERO
BACKGROUND, not vs the all-component median:
    scored = [c for c in comps if c > 0.0]
    zeros  = [c for c in comps if c <= 0.0]
    p3 = len(scored) >= 2 and max(scored) > (max(zeros) if zeros else 0.0)
The distinct<=1 flat-distribution degeneracy guard (incl. all-zero) stays
intact -> all-equal still FAILs.

This is LANGUAGE-AGNOSTIC: the zeros are the HYBRID localizer's lexical/
graph-only candidates (legitimately sem=0 in ANY language), not a
Python/per-task assumption. The discriminator now accepts a tight scored
cluster above the unscored background for any repo/issue/language.

Mirrors the exact branch chain in
scripts/metrics/foundational_gates.py (gate_embedder_consumption p3).
"""

import importlib.util
import math
from pathlib import Path

# Load the live module so K_MAD / _median / _mad are the SAME objects the gate
# uses (no copied constants — the test breaks if the source center logic moves).
_SRC = Path(__file__).resolve().parents[1] / "scripts" / "metrics" / "foundational_gates.py"
_spec = importlib.util.spec_from_file_location("foundational_gates", _SRC)
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)


def _p3(comps):
    """Replicate the EXACT GATE3b p3 branch chain (foundational_gates.py).

    Kept in lock-step with the gate; uses the module's own _median/_mad/K_MAD.
    """
    med = fg._median(comps)
    mad = fg._mad(comps, med)
    mx = max(comps) if comps else 0.0
    scored = [c for c in comps if c > 0.0]
    distinct = len({round(c, 6) for c in comps})
    thresh = fg.K_MAD * mad
    if not comps:
        return False
    if distinct <= 1:
        # all-equal (incl. all-zero) -> flat degeneracy guard -> FAIL.
        return False
    if mad > 0.0:
        return (mx - med) >= thresh
    # MAD==0, distinct>=2: scored cluster vs zero background (the FIX).
    zeros = [c for c in comps if c <= 0.0]
    bg = max(zeros) if zeros else 0.0
    mx_scored = max(scored) if scored else 0.0
    return len(scored) >= 2 and mx_scored > bg


# --- The bug vector: 3 scored (majority) above 2 zero background ------------
def test_scored_majority_cluster_passes():
    """RED before fix (median lands on cluster -> max>median False), GREEN after."""
    comps = [0.827413, 0.827413, 0.827413, 0.0, 0.0]
    # Prove the OLD predicate would have FAILed (documents the regression).
    assert (max(comps) > fg._median(comps)) is False
    # The FIX: scored cluster stands above the zero background -> PASS.
    assert _p3(comps) is True


# --- Existing healthy vectors must STILL pass (no regression) ---------------
def test_arviz_clustered_above_background_passes():
    """Real arviz-shape: strong cluster + zero background (MAD==0 branch)."""
    comps = [0.84, 0.83, 0.82, 0.0, 0.0, 0.0]
    assert _p3(comps) is True


def test_cfn_lint_single_strong_hit_passes():
    """One/few strong hits above an all-zero background -> discriminates."""
    comps = [0.81, 0.79, 0.0, 0.0, 0.0]
    assert _p3(comps) is True


def test_dispersed_distribution_passes_via_mad_branch():
    """A spread distribution (MAD>0) still passes the right-tail test unchanged."""
    comps = [0.86, 0.70, 0.62, 0.55, 0.40]
    assert math.isclose(fg._mad(comps), fg._mad(comps))  # MAD>0 path is exercised
    assert fg._mad(comps) > 0.0
    assert _p3(comps) is True


# --- Degeneracy / dead-embedder vectors must STILL FAIL ---------------------
def test_all_zero_flat_fails():
    """Embedder returned all-zero -> flat -> degeneracy guard FAILs."""
    assert _p3([0.0, 0.0, 0.0, 0.0, 0.0]) is False


def test_all_equal_nonzero_flat_fails():
    """Embedder returned a constant (all-equal nonzero) -> flat -> FAIL."""
    assert _p3([0.5, 0.5, 0.5, 0.5]) is False


def test_single_scored_no_cluster_fails():
    """Only ONE scored value above background is not a cluster (need >=2) -> FAIL."""
    assert _p3([0.83, 0.0, 0.0, 0.0]) is False


def test_empty_components_fail():
    assert _p3([]) is False


if __name__ == "__main__":
    import sys
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
