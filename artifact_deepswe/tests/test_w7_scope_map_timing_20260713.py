r"""W7 (2026-07-13) — RETIRE ``consensus.scope_map`` under the global arbiter (OPTION B).

THE DEFECT (forensics-verified on run 29217805592, all 30 tasks): the Lane-A
``consensus.scope_map`` producer emitted **859 candidates / 0 delivered** — a permanent
production cost with zero deliveries. Cause: the producer is classed ``localization``
(``global_arbiter.class_of_kind`` — a PREVENTIVE class whose decision boundary is ordinal 1,
a SEARCH turn) but it only ever FIRES on a post_view (ordinal 2) or post_edit (ordinal 3) turn.
So under GT_GLOBAL_ARBITER every candidate is born PAST its boundary and the seam flush demotes
it ``repair_support_late`` (626 on that run) or it is ``outranked`` (233). It can NEVER be an
on-time winner — ``is_reactive('consensus.scope_map')`` is False (only ``trace_frame`` is
reactive), so the SM-10 lateness demotion always bites, and ``_ga_candidate_on_time`` is False
for it so the on-time re-selection never re-picks it.

THE FIX (OPTION B — retire, not re-home): its content — bare 1-hop graph-neighbour PATHS of a
file the agent ALREADY acquired + the killed "confirm the edit target with grep" hedge — adds
NOTHING over the on-time search-boundary localization already delivered there
(``gateway._produce_ranked_localization``: issue-keyed top-N ``path:line:sym``;
``_produce_def_ref_partition``: def/ref/callers). Re-homing it to the search boundary (Option A)
would have to anchor on the ranked top file, DUPLICATING that producer as a second localization
dose — the <=1-dose law forbids it. So under the arbiter we STOP producing it (zero
``_query_scope`` / zero ``_seen`` burn) and log ONE per-episode retirement note at
first-eligibility. Delivered observations are byte-identical (scope_map was already 0-delivered
under the arbiter); when the arbiter is OFF the exact legacy production path runs (byte-identical).

Coverage:
  RED-shape (the 859/0 defect) — a scope_map candidate born on a view/edit turn is
    ``repair_support_late`` (0 bytes) under the arbiter. MUTATION (a): revert the boundary
    (``localization`` -> ordinal 3) so the view-turn candidate is on-time -> it delivers ->
    the RED-shape assertion reddens.
  OPTION B — arbiter ON: zero production + ONE per-episode retirement note (idempotent);
    arbiter OFF: the exact legacy production (byte-identical-off).
  BYTE-IDENTITY of observations — removing scope from the pool never changes the delivered dose,
    in BOTH the outranked and the arbitrate-winner-demoted case.
  <=1-DOSE — under the arbiter scope contributes ZERO Lane-A candidates, so it can never be a
    second localization dose alongside a real one; the flush delivers <=1. MUTATION (b): un-retire
    (``_scope_map_retired`` -> False) so the producer runs and appends a SECOND localization
    Lane-A entry -> the <=1 assertion reddens.

Hermetic: no graph.db, no ONNX, no repo checkout (``_query_scope`` monkeypatched; candidates
hand-built). Windows: run with PYTHONIOENCODING=utf-8 (the legacy tag carries an em-dash U+2014).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime import global_arbiter as ga  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    g._reset_oracle_state()
    yield
    g._reset_oracle_state()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _flush_pool(specs, *, kkind="post_view"):
    """Build a hand-made pool of (Candidate, tag-recording thunk) from ``specs`` and flush it.
    Returns the ordered list of delivered tags (<=1 by the arbiter's single-dose guarantee).
    ``specs`` = iterable of (kind, boundary, current, tier, seq, tag). Resets the seam's
    per-episode state first so each flush is an INDEPENDENT scenario (a prior flush's
    unified-dedup stamp cannot suppress this one)."""
    g._reset_oracle_state()
    delivered: list[str] = []
    pool = []
    for kind, boundary, current, tier, seq, tag in specs:
        c = ga.Candidate(plane=ga.PLANE_GATEWAY, kind=kind, dedup_key="k_" + tag,
                         boundary_ordinal=boundary, current_ordinal=current, tier=tier, seq=seq)
        pool.append((c, (lambda t=tag: delivered.append(t))))
    g._global_pool_flush(pool, kkind=kkind, kf="a/x.py", krel="a/x.py")
    return delivered


def _retirement_rows(led_path):
    if not os.path.isfile(led_path):
        return []                                   # no rows written yet -> no note
    rows = [json.loads(l) for l in open(led_path, encoding="utf-8") if l.strip()]
    return [r for r in rows
            if r.get("outcome") == "retired" and r.get("event_type") == "consensus.scope_map"]


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """Route the durable runtime ledger to a temp file so retirement rows are readable."""
    p = tmp_path / "gt_runtime_ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(p))
    return str(p)


# =========================================================================== #
# 1. RED-shape — the 859/0 defect: born-late scope_map is repair_support_late
# =========================================================================== #
def test_scope_map_is_localization_born_late_on_view_and_edit():
    """The structural cause: ``consensus.scope_map`` projects onto the ``localization`` class
    (boundary ordinal 1 = a SEARCH turn), yet the producer fires only on post_view (2) /
    post_edit (3) — ALWAYS past the boundary — and it is PREVENTIVE (not registry-reactive)."""
    assert ga.class_of_kind("consensus.scope_map") == "localization"
    c_view = g._ga_make_candidate(g._GA_PLANE_LANE_A, "consensus.scope_map",
                                  dedup_key="k", target="a/x.py", kkind="post_view", seq=0)
    c_edit = g._ga_make_candidate(g._GA_PLANE_LANE_A, "consensus.scope_map",
                                  dedup_key="k", target="a/x.py", kkind="post_edit", seq=0)
    assert (c_view.boundary_ordinal, c_view.current_ordinal) == (1, 2)   # search boundary; view turn
    assert (c_edit.boundary_ordinal, c_edit.current_ordinal) == (1, 3)   # search boundary; edit turn
    assert g._ga_is_preventive("consensus.scope_map") is True            # never on-time when late


def test_red_shape_born_late_scope_map_is_repair_support_late(monkeypatch):
    """RED-shape (reproduces 859/0): a lone ``consensus.scope_map`` candidate born on a view turn
    ships 0 bytes and is logged ``repair_support_late`` — the permanent-suppression the defect is.
    It is NOT dedup-stamped (deferred, not destroyed), which is exactly why retiring its
    production is byte-identical for DELIVERED bytes (nothing was ever going to deliver)."""
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(g, "_record_hook_suppress",
                        lambda kind, reason="": logged.append((kind, reason)))
    delivered = _flush_pool(
        [("consensus.scope_map", 1, 2, "INFO", 0, "SCOPE")], kkind="post_view")
    assert delivered == []                                          # born late -> 0 bytes
    assert "k_SCOPE" not in g._EPISODE.delivered_dedup             # not stamped -> deferred
    assert ("consensus.scope_map", "ga_" + g._GA_REASON_REPAIR_LATE) in logged


def test_mutation_a_via_seam_maker_reddens(monkeypatch):
    """MUTATION (a), through the seam's own candidate maker so the mutated boundary is what bites:
    with ``localization`` re-homed to ordinal 3, the view-turn scope_map maker yields boundary 3,
    the flush finds it on-time (2 <= 3), and it DELIVERS — reddening the RED-shape."""
    monkeypatch.setattr(g, "_GA_CLASS_BOUNDARY", {**g._GA_CLASS_BOUNDARY, "localization": 3})
    c = g._ga_make_candidate(g._GA_PLANE_GATEWAY, "consensus.scope_map",
                             dedup_key="k_SCOPE", target="a/x.py", kkind="post_view", seq=0)
    assert c.boundary_ordinal == 3                                  # the reverted boundary
    delivered: list[str] = []
    g._global_pool_flush([(c, (lambda: delivered.append("SCOPE")))],
                         kkind="post_view", kf="a/x.py", krel="a/x.py")
    assert delivered == ["SCOPE"]                                   # MUTANT: on-time -> ships (bites)


# =========================================================================== #
# 2. OPTION B — arbiter ON: zero production + ONE per-episode retirement note
# =========================================================================== #
def test_retired_zero_production_and_one_note(monkeypatch, ledger):
    """Under the arbiter (ga_on=True): the producer (``_consensus_scope_block`` / ``_query_scope``
    / the ``_seen`` latch burn) is NEVER called, NOTHING is enqueued, and EXACTLY ONE retirement
    note is logged — even across multiple eligible turns (per-episode latch)."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    calls = {"n": 0}
    monkeypatch.setattr(g, "_consensus_scope_block",
                        lambda rel: calls.__setitem__("n", calls["n"] + 1) or "SHOULD_NOT_APPEAR")
    g._consensus_scope.add(g._norm_rel("src/work.py"))
    g._consensus_scope.add(g._norm_rel("src/other.py"))

    lane: list = []
    g._consensus_scope_lane_a_append(lane, "src/work.py", True)     # post_edit / post_view turn 1
    g._consensus_scope_lane_a_append(lane, "src/other.py", True)    # eligible turn 2
    g._consensus_scope_lane_a_append(lane, "src/work.py", True)     # turn 3 (same file)

    assert calls["n"] == 0                                          # ZERO production
    assert lane == []                                              # nothing enqueued
    assert "consensus.scope_map" not in g._seen and \
        ("consensus_scope", g._norm_rel("src/work.py")) not in g._seen  # latch never burned
    assert len(_retirement_rows(ledger)) == 1                      # ONE note per episode


def test_note_only_at_first_eligibility(monkeypatch, ledger):
    """First-eligibility: the note is logged the first turn the touched file is IN scope (the
    producer WOULD have emitted a real block). A not-yet-in-scope file -> no note, no production
    (correct-or-quiet: the producer would have returned "" there anyway)."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_consensus_scope_block", lambda rel: pytest.fail("producer ran"))
    lane: list = []
    g._consensus_scope_lane_a_append(lane, "src/not_in_scope.py", True)  # not in scope yet
    assert _retirement_rows(ledger) == []                          # not eligible -> no note
    g._consensus_scope.add(g._norm_rel("src/work.py"))             # now the agent has collected scope
    g._consensus_scope_lane_a_append(lane, "src/work.py", True)
    assert len(_retirement_rows(ledger)) == 1                      # eligible -> exactly one note


def test_note_resets_per_episode(monkeypatch, ledger):
    """The retirement-note latch resets per attempt (F3 reset law), so a fresh episode logs the
    note again — one per episode, never leaked across the ``_reset_oracle_state`` boundary."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_consensus_scope_block", lambda rel: "X")
    g._consensus_scope.add(g._norm_rel("src/work.py"))
    g._consensus_scope_lane_a_append([], "src/work.py", True)
    assert len(_retirement_rows(ledger)) == 1
    g._reset_oracle_state()                                        # new episode
    g._consensus_scope.add(g._norm_rel("src/work.py"))
    g._consensus_scope_lane_a_append([], "src/work.py", True)
    assert len(_retirement_rows(ledger)) == 2                      # re-fires once in the new episode


# =========================================================================== #
# 3. BYTE-IDENTITY-OFF — arbiter OFF runs the exact legacy production path
# =========================================================================== #
def test_byte_identical_off_produces_and_enqueues(monkeypatch, ledger):
    """ga_on=False -> the EXACT legacy path: ``_consensus_scope_block`` runs, a non-empty block is
    enqueued as ``consensus.scope_map``, and NO retirement note is logged (the note only fires
    under the arbiter)."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_query_scope", lambda rel: ["src/neighbor.py"])
    g._consensus_scope.add(g._norm_rel("src/work.py"))
    lane: list = []
    g._consensus_scope_lane_a_append(lane, "src/work.py", False)
    assert len(lane) == 1 and lane[0][0] == "consensus.scope_map"  # legacy production enqueued
    assert "<gt-scope" in lane[0][1]                              # the real block bytes
    assert _retirement_rows(ledger) == []                        # no retirement note off the arbiter


def test_byte_identical_off_matches_direct_producer(monkeypatch):
    """The bytes the OFF path enqueues are EXACTLY what ``_consensus_scope_block`` returns directly
    — the helper is a transparent wrapper of the legacy producer when the arbiter is off."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_query_scope", lambda rel: ["src/a.py", "src/b.py"])
    g._consensus_scope.add(g._norm_rel("src/work.py"))
    direct = g._consensus_scope_block("src/work.py")             # burns the _seen latch
    g._reset_oracle_state()                                       # clear the latch for the wrapper
    monkeypatch.setattr(g, "_query_scope", lambda rel: ["src/a.py", "src/b.py"])
    g._consensus_scope.add(g._norm_rel("src/work.py"))
    lane: list = []
    g._consensus_scope_lane_a_append(lane, "src/work.py", False)
    assert lane[0][1] == direct                                   # byte-identical block


def test_off_path_isolates_producer_crash(monkeypatch, ledger):
    """A producer crash on the OFF path is caught, ``_crash_emit`` records it, and nothing is
    enqueued (Lane-A producer isolation preserved by the wrapper)."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    def _boom(rel):
        raise RuntimeError("boom")
    monkeypatch.setattr(g, "_consensus_scope_block", _boom)
    emitted: list[str] = []
    monkeypatch.setattr(g, "_crash_emit", lambda kind, exc=None: emitted.append(kind))
    lane: list = []
    g._consensus_scope_lane_a_append(lane, "src/work.py", False)
    assert lane == [] and emitted == ["consensus.scope_map"]


# =========================================================================== #
# 4. BYTE-IDENTITY of DELIVERED OBSERVATIONS — removing scope never moves the dose
# =========================================================================== #
# companion = an on-time localization fact at the search boundary (what actually delivers).
_COMP = ("def_ref_partition", 1, 1, "VERIFIED", 1, "COMP")


def test_removing_scope_from_pool_is_byte_identical_outranked_case():
    """Scope OUTRANKED (INFO tier loses to the VERIFIED companion): the pool WITH the late scope
    and the pool WITHOUT it deliver the SAME single dose (the companion). Removing scope changes
    nothing the agent observes."""
    scope_lo = ("consensus.scope_map", 1, 2, "INFO", 0, "SCOPE")
    with_scope = _flush_pool([scope_lo, _COMP])
    without_scope = _flush_pool([_COMP])
    assert with_scope == without_scope == ["COMP"]


def test_removing_scope_from_pool_is_byte_identical_demoted_case():
    """Scope would be the ARBITRATE WINNER (VERIFIED, seq 0) but is demoted ``repair_support_late``
    -> on-time re-selection picks the companion. The pool WITHOUT scope directly picks the
    companion. Same delivered dose either way — the demotion path and the removal path converge."""
    scope_hi = ("consensus.scope_map", 1, 2, "VERIFIED", 0, "SCOPE")
    with_scope = _flush_pool([scope_hi, _COMP])
    without_scope = _flush_pool([_COMP])
    assert with_scope == without_scope == ["COMP"]


# =========================================================================== #
# 5. <=1-DOSE — scope adds no second localization dose; the arbiter delivers <=1
# =========================================================================== #
def test_le_one_dose_two_on_time_candidates_deliver_one():
    """The arbiter's structural single-dose guarantee: two ON-TIME localization candidates on a
    search turn -> EXACTLY ONE delivers (the higher-ranked; here the VERIFIED one)."""
    delivered = _flush_pool(
        [("def_ref_partition", 1, 1, "VERIFIED", 0, "D1"),
         ("name_fold", 1, 1, "INFO", 1, "D2")], kkind="post_search")
    assert delivered == ["D1"]                                     # <=1 dose


def test_le_one_dose_retired_scope_adds_no_second_lane_a_entry(monkeypatch):
    """Under the arbiter, ``_consensus_scope_lane_a_append`` contributes ZERO Lane-A candidates, so
    a real localization block already in ``lane_a`` stays the ONLY localization dose — scope can
    never be a redundant SECOND dose. (This is the OPTION-B <=1-dose property.)"""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_consensus_scope_block", lambda rel: "SHOULD_NOT_APPEAR")
    g._consensus_scope.add(g._norm_rel("src/work.py"))
    lane: list = [("post_search.localize", "def facts here")]      # a real on-time localization dose
    g._consensus_scope_lane_a_append(lane, "src/work.py", True)
    kinds = [k for k, _t in lane]
    assert kinds == ["post_search.localize"]                       # scope added NOTHING
    assert "consensus.scope_map" not in kinds                      # no second localization dose


def test_mutation_b_allow_double_dose_reddens_le_one(monkeypatch):
    """MUTATION (b) — ALLOW DOUBLE-DOSE: un-retire the producer (``_scope_map_retired`` -> False)
    so the arbiter-on path runs the producer and appends a SECOND localization Lane-A entry
    (``consensus.scope_map``) ALONGSIDE the real one -> the <=1 assertion above reddens. Proves the
    retirement predicate is the load-bearing seam that keeps scope from being a redundant dose."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_consensus_scope_block", lambda rel: "SCOPE BLOCK")
    monkeypatch.setattr(g, "_scope_map_retired", lambda ga_on: False)   # the mutation
    g._consensus_scope.add(g._norm_rel("src/work.py"))
    lane: list = [("post_search.localize", "def facts here")]
    g._consensus_scope_lane_a_append(lane, "src/work.py", True)
    kinds = [k for k, _t in lane]
    assert "consensus.scope_map" in kinds and len(kinds) == 2       # MUTANT: a SECOND dose appears


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
