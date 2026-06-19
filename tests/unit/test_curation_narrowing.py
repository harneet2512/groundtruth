"""Consensus curation narrowing — proves the curation area SHRINKS across bands.

The monumental property: the candidate set GT keeps in play decreases as GT and
the agent build shared understanding (early full -> mid drop-visited -> late
edit-focus). These assertions FAIL on a no-op narrow(), so they prove real
narrowing, not a pass-through. The structured log line is the agent-loop proof.
"""
from groundtruth.router.curation import CurationTracker, band_for

CANDS = {f"src/f{i}.py" for i in range(10)}  # 10-file curation area


def _neighbors(f):
    # f0 is graph-connected to f1, f2 (its callers/callees)
    return {"src/f1.py", "src/f2.py"} if f == "src/f0.py" else set()


def test_band_boundaries():
    assert band_for(0) == "early"
    assert band_for(4) == "early"
    assert band_for(5) == "mid"
    assert band_for(9) == "mid"
    assert band_for(10) == "late"
    assert band_for(20) == "late"


def test_curation_shrinks_monotonically_across_bands():
    t = CurationTracker(candidates=set(CANDS), neighbors_of=_neighbors)
    # EARLY (action 2): agent viewed 2 files, no edit -> full prior kept.
    s_early = t.narrow(2, visited={"src/f5.py", "src/f6.py"}, edited=set())
    assert len(s_early) == 10, "early band keeps the full prior"
    # MID (action 7): agent visited-and-left 4 files (none edited) -> drop them.
    s_mid = t.narrow(7, visited={"src/f5.py", "src/f6.py", "src/f7.py", "src/f8.py"}, edited=set())
    assert len(s_mid) == 6, "mid band drops visited-and-left (relevance feedback)"
    # LATE (action 12): agent edited f0 (neighbors f1,f2) -> keep only edit-connected.
    s_late = t.narrow(12, visited={"src/f5.py", "src/f6.py", "src/f7.py", "src/f8.py"}, edited={"src/f0.py"})
    assert s_late == {"src/f0.py", "src/f1.py", "src/f2.py"}, "late band converges on edit + neighbors"
    # The monumental invariant: monotonic shrink, strictly smaller by late.
    assert len(s_early) >= len(s_mid) >= len(s_late)
    assert len(s_late) < len(s_early)


def test_late_with_no_edit_does_not_empty():
    """If the agent hasn't edited by the late band, keep the mid filter — never
    collapse the curation area to empty (that would blind the agent)."""
    t = CurationTracker(candidates=set(CANDS), neighbors_of=_neighbors)
    t.narrow(7, visited={"src/f1.py"}, edited=set())
    s = t.narrow(12, visited={"src/f1.py", "src/f2.py"}, edited=set())
    assert len(s) > 0


def test_late_with_edit_outside_candidates_converges_not_empties():
    """Review fix: the agent routinely edits a file the brief never proposed (it
    localized OUTSIDE the prior set — the normal path). The late band must
    converge on the edit-neighborhood, NOT intersect to empty (which would emit
    size=0 at the exact moment the agent found the fix)."""
    t = CurationTracker(candidates=set(CANDS), neighbors_of=_neighbors)
    t.narrow(7, visited=set(), edited=set())
    # src/gold.py is NOT in CANDS and has no neighbors in CANDS:
    s = t.narrow(12, visited=set(), edited={"src/gold.py"})
    assert s == {"src/gold.py"}, "converges on the edited file; never empties"
    assert len(s) > 0


def test_curation_never_grows():
    """Strict monotonic non-increase across a whole 0-14 trajectory."""
    t = CurationTracker(candidates=set(CANDS), neighbors_of=_neighbors)
    prev = len(CANDS)
    for a in range(0, 15):
        edited = {"src/f0.py"} if a >= 10 else set()
        s = t.narrow(a, visited={"src/f9.py"}, edited=edited)
        assert len(s) <= prev, f"curation area GREW at action {a} ({len(s)} > {prev})"
        prev = len(s)


def test_log_line_is_structured_and_parseable():
    t = CurationTracker(candidates=set(CANDS), neighbors_of=_neighbors)
    t.narrow(7, visited={"src/f5.py", "src/f6.py"}, edited=set())
    line = t.log_line(7)
    assert line.startswith("[GT_CURATION]")
    for tok in ("action=7", "band=mid", "size=8", "initial=10", "dropped=2", "reason=mid_drop_visited"):
        assert tok in line, f"missing {tok} in {line!r}"


def test_no_op_would_fail_this():
    """Documents the RED: a pass-through narrow() leaves size=10 and fails here."""
    t = CurationTracker(candidates=set(CANDS), neighbors_of=_neighbors)
    s = t.narrow(7, visited={"src/f5.py", "src/f6.py", "src/f7.py"}, edited=set())
    assert len(s) == 7, "real narrowing drops the 3 visited-and-left files"
