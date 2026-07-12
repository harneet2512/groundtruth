r"""SM-10 Item D (2026-07-12) — the <=1-dose arbiter, made honest about timing and acquisition.

Two sub-fixes on ``gt_mini_patch._global_pool_flush`` (the SM-5 global ranked competition):

D1 — ON-TIME RE-SELECTION. Item B demotes a PREVENTIVE top winner delivered AFTER its decision
boundary (``res.repair_support``) to a suppressed loser. Pre-D1 that SILENCED the whole turn even
when the pool still held an ON-TIME deliverable. D1 re-picks the highest-ranked ON-TIME eligible
fact as the SINGLE dose instead of shipping 0 bytes. It STAYS <=1 dose — the delivery loop still
fires exactly one thunk; D1 only changes WHICH candidate is the winner.

D2 — READ-SET ACQUIRED. The ``acquired`` set the arbiter uses to suppress a redundant
localization fact was edited-files-only. D2 seeds it from the per-attempt READ-SET (viewed /
greped / edited targets, PATHS only) so a localization fact pointing at a file the agent already
OPENED/greped is suppressed (non-re-acquisition — the charter's consumption definition).

Proven on the REAL seam (hermetic — no graph, no ONNX). Each pin is RED-first with a biting
mutation (each applied, observed to bite, restored). Both fixes ride only under GT_GLOBAL_ARBITER
(the flush runs only then) -> byte-identical when off. Windows: PYTHONIOENCODING=utf-8.
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


def _flush(specs, *, readset=None, kkind="post_edit"):
    """Build a pool from candidate specs, run the REAL flush, return the delivered tags.
    A spec: dict(kind, b=boundary, cur=current, key=dedup_key, tag, sym='', supp=False)."""
    if readset:
        for r in readset:
            g._EPISODE.read_targets.add(r)
    delivered: list[str] = []
    pool = []
    for s in specs:
        c = ga.Candidate(
            plane=ga.PLANE_GATEWAY, kind=s["kind"], dedup_key=s["key"],
            boundary_ordinal=s["b"], current_ordinal=s["cur"], seq=len(pool),
            symbol=s.get("sym", ""), suppressible_if_acquired=s.get("supp", False))
        pool.append((c, (lambda t=s["tag"]: delivered.append(t))))
    g._global_pool_flush(pool, kkind=kkind, kf="a/x.py", krel="a/x.py")
    return delivered


# =========================================================================== #
# D1 — on-time re-selection
# =========================================================================== #
def test_d1_reselects_best_on_time_fact_instead_of_silence():
    """A LATE preventive top winner (caller_contract, rank 40, current>boundary) is demoted;
    an ON-TIME lower-ranked localization fact (rank 20, current<=boundary) is re-selected and
    ships — the turn is NOT silenced. Pre-fix: 0 bytes."""
    delivered = _flush([
        {"kind": "caller_break", "b": 3, "cur": 5, "key": "k_late", "tag": "LATE"},
        {"kind": "def_ref_partition", "b": 1, "cur": 1, "key": "k_ontime", "tag": "ONTIME"},
    ])
    assert delivered == ["ONTIME"]                       # the on-time fact ships (was 0 bytes)
    # the on-time winner is dedup-stamped; the demoted late winner is NOT (re-competes later).
    assert "k_ontime" in g._EPISODE.delivered_dedup
    assert "k_late" not in g._EPISODE.delivered_dedup


def test_d1_at_most_one_dose_invariant():
    """<=1 DOSE INVARIANT under re-selection: a late top + TWO on-time facts -> EXACTLY ONE
    ships (the highest-ranked on-time). D1 re-picks the single dose, it never adds a second."""
    delivered = _flush([
        {"kind": "caller_break", "b": 3, "cur": 5, "key": "k_late", "tag": "LATE"},
        {"kind": "companion_surface", "b": 3, "cur": 3, "key": "k_hi", "tag": "HI"},   # causal_chain 30
        {"kind": "l5.stuck", "b": 5, "cur": 1, "key": "k_lo", "tag": "LO"},            # recovery 10
    ])
    assert len(delivered) == 1, "the <=1-dose invariant must hold through re-selection"
    assert delivered == ["HI"]                           # the highest-ranked on-time (30 > 10)


def test_d1_no_on_time_fact_stays_silent():
    """When the demoted preventive winner is the ONLY eligible fact (nothing on-time remains),
    the turn correctly stays silent — re-selection never fabricates a dose."""
    delivered = _flush([
        {"kind": "caller_break", "b": 3, "cur": 5, "key": "k_late", "tag": "LATE"},
    ])
    assert delivered == []
    assert "k_late" not in g._EPISODE.delivered_dedup     # deferred, not destroyed


def test_d1_mutation_skip_reselection_silences(monkeypatch):
    """MUTATION — force ``_ga_candidate_on_time`` -> False (no fact is ever on-time == skipping
    the re-selection): the demoted turn ships 0 bytes again, reddening the re-selection pin."""
    monkeypatch.setattr(g, "_ga_candidate_on_time", lambda _c: False)
    delivered = _flush([
        {"kind": "caller_break", "b": 3, "cur": 5, "key": "k_late", "tag": "LATE"},
        {"kind": "def_ref_partition", "b": 1, "cur": 1, "key": "k_ontime", "tag": "ONTIME"},
    ])
    assert delivered == []                               # without re-selection -> silenced


# =========================================================================== #
# D2 — read-set acquired
# =========================================================================== #
def test_d2_readset_suppresses_localization_fact_for_viewed_file():
    """A localization fact pointing at a file already in the READ-SET (viewed/greped, NOT
    edited) is suppressed as already-acquired. Pre-fix (edited-only acquired): it delivered."""
    delivered = _flush(
        [{"kind": "def_ref_partition", "b": 1, "cur": 1, "key": "k_loc", "tag": "LOC",
          "sym": "src/users.py", "supp": True}],
        readset={"src/users.py"}, kkind="post_search")
    assert delivered == []                               # non-re-acquisition suppression


def test_d2_fresh_unacquired_localization_fact_delivers():
    """A localization fact for a file the agent has NOT acquired (not in the read-set) is fresh
    -> delivers. Conservative: the read-set only suppresses what was actually acquired."""
    delivered = _flush(
        [{"kind": "def_ref_partition", "b": 1, "cur": 1, "key": "k_loc", "tag": "LOC",
          "sym": "src/other.py", "supp": True}],
        readset={"src/users.py"}, kkind="post_search")
    assert delivered == ["LOC"]


def test_d2_non_suppressible_fact_survives_readset():
    """Only ``suppressible_if_acquired`` (localization-class) facts are read-set gated; a
    non-suppressible fact whose target was viewed still delivers (conservative)."""
    delivered = _flush(
        [{"kind": "caller_break", "b": 3, "cur": 3, "key": "k_cc", "tag": "CC",
          "sym": "src/users.py", "supp": False}],
        readset={"src/users.py"}, kkind="post_edit")
    assert delivered == ["CC"]


def test_d2_mutation_no_readset_union_delivers():
    """MUTATION — the read-set is EMPTY (== the flush not unioning read_targets into acquired):
    the viewed-file localization fact is no longer suppressed and delivers, reddening the
    suppression pin."""
    delivered = _flush(
        [{"kind": "def_ref_partition", "b": 1, "cur": 1, "key": "k_loc", "tag": "LOC",
          "sym": "src/users.py", "supp": True}],
        readset=None, kkind="post_search")   # empty read-set (the mutation state)
    assert delivered == ["LOC"]


# =========================================================================== #
# D2 — the read-set seed (_command_path_targets): greped/opened FILE targets, leak-safe
# =========================================================================== #
def test_command_path_targets_extracts_explicit_file_args():
    assert g._command_path_targets("grep -n foo src/users.py") == {"src/users.py"}
    assert g._command_path_targets("cat pkg/mod.go && head lib/x.rs") == {"pkg/mod.go", "lib/x.rs"}
    # the ./ prefix normalizes to the graph frame
    assert g._command_path_targets("sed -n 1,5p ./a/b.py") == {"a/b.py"}


def test_command_path_targets_no_over_suppression_on_bare_grep():
    """A repo-wide grep (no explicit file) and a bare DIR arg name NO file -> empty read-set
    seed -> a localization fact answering the grep is NEVER over-suppressed."""
    assert g._command_path_targets("grep -rn 'def foo' .") == set()
    assert g._command_path_targets("grep -rn foo src/") == set()


def test_readset_resets_per_attempt():
    """The read-set is a per-attempt accumulator: reset_attempt / _reset_oracle_state clears it
    (a fresh attempt must re-acquire)."""
    g._EPISODE.read_targets.add("src/x.py")
    g._reset_oracle_state()
    assert g._EPISODE.read_targets == set()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
