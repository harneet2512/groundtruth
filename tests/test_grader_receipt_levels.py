"""B-7 (Wave 10) TTD — the consumption grader must not claim causality from a single arm.

Doctrine under test (verifyandobserve.md §0 receipt ladder + gate item 12):
    The honest receipt ladder has FIVE levels — 1 DELIVERED · 2 REFERENCED · 3 ACTED ·
    4 RESOLVED_STATE · 5 CAUSAL. A SINGLE arm (one run's trajectory) may substantiate at
    MOST level 4. Level 5 CAUSAL is a PAIRED / fixed-history counterfactual claim and MUST
    NEVER be emitted from one trajectory — a one-arm receipt level is CORRELATION, not the
    paired non-reacquisition / causal criterion. (Gate item 12: "CAUSAL only from a paired /
    fixed-history counterfactual.")

RED-first: the assertions below reference the B-7 refusal surface (``receipt_arms``,
``receipt_causal``, ``compute_causal``, ``assert_paired_for_causal``, ``SingleArmCausalError``,
``cap_single_arm_level``). Against the pre-B-7 grader these keys/functions do NOT exist, so the
tests ERROR/FAIL (RED); after the fix they pass (GREEN). A biting mutation (disable the
single-arm chokepoint => causal from one arm) is proven load-bearing.

Fixtures (mini-swe-agent trajectory shape, same convention as the sibling suite):
    subst_receipt_resolved            — single arm, foo reaches RESOLVED_STATE (reused).
    subst_b7_env_not_referenced       — a delivered symbol named ONLY in env output.
    subst_b7_pair_on / subst_b7_pair_off — a matched GT-on / GT-off pair.
"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_GRADER = os.path.join(os.path.dirname(_HERE), "scripts", "gt_substitution_grader.py")
_FIX = os.path.join(_HERE, "fixtures")


def _load_grader():
    spec = importlib.util.spec_from_file_location("gt_substitution_grader", _GRADER)
    assert spec and spec.loader, f"grader not found at {_GRADER}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def grader():
    return _load_grader()


def _per_fact(rep, token, cls=None):
    pf = rep.get("receipt_per_fact") or []
    hits = [f for f in pf if f["token"] == token and (cls is None or f["cls"] == cls)]
    assert hits, f"no receipt_per_fact for token={token!r} cls={cls!r}"
    return hits[0]


# ===========================================================================
# (a) SINGLE-ARM ⇒ levels 1-4 only; CAUSAL refused / UNAVAILABLE.
# ===========================================================================
def test_single_arm_report_labels_arms_and_refuses_causal(grader):
    """A single-arm grade MUST declare arms=single and mark causal UNAVAILABLE, so no
    consumer can read its RESOLVED_STATE rate as a causal/effectiveness claim."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_resolved"))
    assert rep["receipt_arms"] == grader.ARMS_SINGLE == "single", rep.get("receipt_arms")
    assert rep["receipt_causal"] == grader.CAUSAL_UNAVAILABLE_SINGLE_ARM, rep.get("receipt_causal")
    assert rep["receipt_causal"] == "UNAVAILABLE(single-arm)"


def test_single_arm_never_assigns_level_five(grader):
    """No per-fact receipt in a single arm may exceed RESOLVED_STATE (level 4)."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_resolved"))
    for f in rep["receipt_per_fact"]:
        assert 1 <= f["highest_level_num"] <= 4, f
        assert f["highest_level"] != grader.RECEIPT_CAUSAL, f
    # the per-class table is likewise arms-labeled single.
    for cls, blk in rep["receipt_by_class"].items():
        assert blk["arms"] == "single", (cls, blk.get("arms"))


def test_cap_single_arm_level_clamps_to_four(grader):
    """The level-capping primitive clamps any level to <=4 for single-arm grading."""
    assert grader.cap_single_arm_level(1) == 1
    assert grader.cap_single_arm_level(4) == 4
    assert grader.cap_single_arm_level(5) == 4   # CAUSAL can never come from one arm
    assert grader.cap_single_arm_level(99) == 4


def test_assert_paired_for_causal_is_the_chokepoint(grader):
    """The named chokepoint raises for any non-paired arms value, no override."""
    grader.assert_paired_for_causal(grader.ARMS_PAIRED)   # paired => OK (no raise)
    with pytest.raises(grader.SingleArmCausalError):
        grader.assert_paired_for_causal(grader.ARMS_SINGLE)
    with pytest.raises(grader.SingleArmCausalError):
        grader.assert_paired_for_causal("anything-else")


# ===========================================================================
# (b) ENV-OUTPUT-ONLY naming a delivered token does NOT reach REFERENCED (mirror B-13).
# ===========================================================================
def test_env_output_naming_does_not_reference(grader):
    """compute_widget is delivered, named ONLY by a tool observation ('def compute_widget():
    return 1'), never by an agent policy message => REFERENCED must stay False (policy
    source required; env output can never promote the receipt)."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_b7_env_not_referenced"))
    f = _per_fact(rep, "compute_widget", "symbol-definition")
    assert f["levels"]["referenced"] is False, f
    assert f["highest_level"] == "delivered", f


# ===========================================================================
# (c) MATCHED PAIR ⇒ CAUSAL computed (arms=paired); single arm ⇒ refused.
# ===========================================================================
def test_matched_pair_computes_causal(grader):
    """A GT-on / GT-off pair (edit@1 vs edit@3, 0 vs 2 searches) yields a paired causal
    table: arms=paired, causal_available, and off-on behavioral deltas at 8-dp."""
    on = grader.grade_run(os.path.join(_FIX, "subst_b7_pair_on"))
    off = grader.grade_run(os.path.join(_FIX, "subst_b7_pair_off"))
    assert on["turns_to_first_edit"] == 1, on
    assert off["turns_to_first_edit"] == 3, off
    assert off["search_count"] == 2, off

    causal = grader.compute_causal(on, off)
    assert causal["receipt_arms"] == grader.ARMS_PAIRED == "paired"
    assert causal["causal_available"] is True
    d = causal["causal_behavioral_delta"]
    assert d["turns_to_first_edit_delta"] == 2.0, d      # off(3) - on(1)
    assert d["search_count_delta"] == 2.0, d             # off(2) - on(0)
    # per-fact-class causal table is keyed by fact_class + arms-labeled paired.
    assert set(causal["causal_by_class"]) == set(grader.FACT_CLASSES)
    for cls, blk in causal["causal_by_class"].items():
        assert blk["arms"] == "paired", (cls, blk)


def test_compute_causal_refuses_single_arm(grader):
    """The SINGLE-ARM refusal: passing one graded report as BOTH arms (or the identical
    run dir twice) is a single arm masquerading as a pair => SingleArmCausalError.
    This is the coercion the current grader could not refuse (no arms notion at all)."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_resolved"))
    with pytest.raises(grader.SingleArmCausalError):
        grader.compute_causal(rep, rep)                  # same object => single arm
    # a re-grade of the SAME dir (distinct object, identical trajectory) is also single-arm.
    rep2 = grader.grade_run(os.path.join(_FIX, "subst_receipt_resolved"))
    with pytest.raises(grader.SingleArmCausalError):
        grader.compute_causal(rep, rep2)


def test_compute_causal_refuses_ungraded_arm(grader, tmp_path):
    """An empty/ungraded arm cannot ground a counterfactual => refuse."""
    on = grader.grade_run(os.path.join(_FIX, "subst_b7_pair_on"))
    empty = grader.grade_run(str(tmp_path))              # no trajectory => 'none'
    assert empty["trajectory_source"] == "none"
    with pytest.raises(grader.SingleArmCausalError):
        grader.compute_causal(on, empty)


# ===========================================================================
# (d) DETERMINISM — same input twice ⇒ identical output.
# ===========================================================================
def test_single_arm_determinism(grader):
    d = os.path.join(_FIX, "subst_receipt_resolved")
    a = json.dumps(grader.grade_run(d), sort_keys=True, default=str)
    b = json.dumps(grader.grade_run(d), sort_keys=True, default=str)
    assert a == b


def test_causal_determinism(grader):
    on = grader.grade_run(os.path.join(_FIX, "subst_b7_pair_on"))
    off = grader.grade_run(os.path.join(_FIX, "subst_b7_pair_off"))
    a = json.dumps(grader.compute_causal(on, off), sort_keys=True, default=str)
    b = json.dumps(grader.compute_causal(on, off), sort_keys=True, default=str)
    assert a == b
