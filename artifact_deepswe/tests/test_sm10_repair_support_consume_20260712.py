"""SM-10 (2026-07-12) — CONSUME ``repair_support`` at the seam flush.

``global_arbiter.arbitrate`` already computes ``repair_support = current_ordinal >
boundary_ordinal`` on the winner (a fact delivered AFTER its decision boundary is REPAIR
SUPPORT, not prevention). Pre-SM-10 the seam (``_global_pool_flush``) treated it as a
HOST-ONLY telemetry label — the winner was DELIVERED as if preventive regardless. This
wave gives it a real delivery consequence: a PREVENTIVE-class winner (localization /
caller_contract / causal_chain) that missed its decision boundary is DEMOTED to a
suppressed loser (0 bytes, re-armed for a later on-time turn); REACTIVE classes
(covering-RED / syntax / recovery / obligation) are UNTOUCHED — post-event is their nature.

Proven on the REAL seam (``gt_mini_patch._global_pool_flush``), hermetic (no graph / no ONNX):
  (1) a late PREVENTIVE winner ships 0 bytes (its thunk never runs);
  (2) a late REACTIVE winner STILL delivers (distinguished by CLASS, not the ordinal);
  (3) an ON-TIME preventive winner delivers (it is repair_support that gates, not the class);
  (4) the suppressed preventive winner is NOT dedup-stamped -> re-competes next turn
      (deferred, not destroyed) and is LOGGED as a loser (never a silent drop);
  (5) MUTATION — skip the repair_support consumption (force _ga_is_preventive -> False):
      the late preventive winner delivers again, reddening (1).

Windows: run with PYTHONIOENCODING=utf-8.
"""
from __future__ import annotations

import pytest

import gt_mini_patch as g
from groundtruth.runtime import global_arbiter as ga


@pytest.fixture(autouse=True)
def _clean_state():
    g._reset_oracle_state()
    yield
    g._reset_oracle_state()


def _pool():
    """Return (delivered_list, make(cand, tag)) — a thunk that records its tag when run."""
    delivered: list[str] = []

    def mk(cand, tag):
        def _t():
            delivered.append(tag)
        return (cand, _t)

    return delivered, mk


def _cand(kind, *, boundary, current, dedup_key, plane=ga.PLANE_GATEWAY):
    return ga.Candidate(plane=plane, kind=kind, dedup_key=dedup_key,
                        boundary_ordinal=boundary, current_ordinal=current, seq=0)


# =========================================================================== #
# the class classifier — preventive vs reactive by the LADDER, not a number
# =========================================================================== #
def test_preventive_classifier_by_class():
    # PREVENTIVE — guidance meant to arrive before the decision it steers
    assert g._ga_is_preventive("def_ref_partition") is True      # localization
    assert g._ga_is_preventive("caller_break") is True           # caller_contract
    assert g._ga_is_preventive("companion_surface") is True      # causal_chain
    # REACTIVE — post-event by design, never suppressed for lateness
    assert g._ga_is_preventive("covering_verdict") is False      # executed_world_fact
    assert g._ga_is_preventive("edit.syntax") is False           # edit_violation
    assert g._ga_is_preventive("l5.stuck") is False              # recovery
    assert g._ga_is_preventive("spec.obligation") is False       # obligation_violation
    # unmapped / internal -> False (correct-or-quiet, never suppressed)
    assert g._ga_is_preventive("internal_prior_xyz") is False


# =========================================================================== #
# (1) a late PREVENTIVE winner is SUPPRESSED (0 bytes) — the load-bearing pin
# =========================================================================== #
def test_late_preventive_winner_suppressed():
    delivered, mk = _pool()
    # localization boundary=1; delivered on an edit turn (current=3) -> repair_support (3>1)
    pool = [mk(_cand("def_ref_partition", boundary=1, current=3, dedup_key="k_prev"), "PREV")]
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    assert delivered == []                       # too-late preventive fact -> 0 bytes
    assert "k_prev" not in g._EPISODE.delivered_dedup   # not stamped -> re-competes next turn


# =========================================================================== #
# (2) a late REACTIVE winner STILL delivers — untouched (distinguish by CLASS)
# =========================================================================== #
def test_late_reactive_winner_still_delivers():
    delivered, mk = _pool()
    # executed_world_fact boundary=4; delivered on a submit turn (current=5) -> repair_support,
    # but a covering RED is SUPPOSED to be post-event, so it is NOT suppressed.
    pool = [mk(_cand("covering_verdict", boundary=4, current=5, dedup_key="k_react"), "REACT")]
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    assert delivered == ["REACT"]                # reactive class delivers even when late
    assert "k_react" in g._EPISODE.delivered_dedup


# =========================================================================== #
# (3) an ON-TIME preventive winner delivers — repair_support is the gate, not class
# =========================================================================== #
def test_on_time_preventive_winner_delivers():
    delivered, mk = _pool()
    # localization boundary=1; delivered on a SEARCH turn (current=1) -> repair_support False
    pool = [mk(_cand("def_ref_partition", boundary=1, current=1, dedup_key="k_ontime"), "ONTIME")]
    g._global_pool_flush(pool, kkind="post_search", kf="", krel="")
    assert delivered == ["ONTIME"]               # preventive + on-time -> delivered
    assert "k_ontime" in g._EPISODE.delivered_dedup


# =========================================================================== #
# (4) the suppressed winner is DEMOTED to a logged loser (deferred, not destroyed)
# =========================================================================== #
def test_suppressed_preventive_winner_logged_as_loser(monkeypatch):
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(g, "_record_hook_suppress",
                        lambda kind, reason="": logged.append((kind, reason)))
    delivered, mk = _pool()
    pool = [mk(_cand("def_ref_partition", boundary=1, current=3, dedup_key="k_prev"), "PREV")]
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    assert delivered == []
    # the demoted winner is logged with the repair-late reason (never a silent drop)
    assert ("def_ref_partition", "ga_" + g._GA_REASON_REPAIR_LATE) in logged


# =========================================================================== #
# (5) MUTATION — skip the repair_support consumption -> the pin reddens
# =========================================================================== #
def test_mutation_skip_repair_consumption_reddens(monkeypatch):
    """Force ``_ga_is_preventive`` -> False for every kind (== skipping the consumption): the
    late preventive winner is then delivered as if preventive, reddening (1)."""
    delivered, mk = _pool()
    pool = [mk(_cand("def_ref_partition", boundary=1, current=3, dedup_key="k_prev"), "PREV")]
    monkeypatch.setattr(g, "_ga_is_preventive", lambda _k: False)  # the mutation
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    assert delivered == ["PREV"]                 # without consumption the too-late fact ships (bites)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
