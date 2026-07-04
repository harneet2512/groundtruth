"""Pins for the deterministic issue-shape classifier (``stratum.py``).

Each test names the invariant it guards. The load-bearing ones are the #51
regression pins: stratum A must key on graph RESOLUTION, never token shape —
whether the shape leaks in through prose (test_B_*) or through the no-graph door
(test_no_graph_*). Reverting either guard flips a case and reddens this file.
"""

from __future__ import annotations

from groundtruth.pretask.anchors import extract_issue_anchors
from groundtruth.pretask.stratum import classify_stratum

# --- fixture inputs (mirror conftest.tiny_graph_db node set) ------------------
ISSUE_A = "SafeWatchdog does not release the file descriptor after a crash."
ISSUE_B = "The retry_backoff occasionally stalls when the connection drops under heavy load."
ISSUE_C = (
    "SafeWatchdog fails on start.\n\n"
    "Traceback (most recent call last):\n"
    '  File "patroni/watchdog.py", line 33, in _fd\n'
    "    raise OSError\n"
    "OSError: bad fd"
)
ISSUE_D = "Add support for `require_cache_info` and `reset_require_cache` helpers on SafeWatchdog."
ISSUE_CD = (
    "Add `require_cache_info`.\n\n"
    "Traceback (most recent call last):\n"
    '  File "patroni/watchdog.py", line 1, in f\n'
    "    raise"
)


def test_A_symbol_anchored(tiny_graph_db: str) -> None:
    r = classify_stratum(ISSUE_A, tiny_graph_db)
    assert r.label == "A"
    assert "SafeWatchdog" in r.evidence["resolved_anchors"]


def test_B_behavior_described_shape_is_not_resolution(tiny_graph_db: str) -> None:
    """#51 PIN. ISSUE_B carries a snake_case shape token (``retry_backoff``) that
    does NOT resolve to a node. If A is (re)gated on ``symbols_pre_stopword``
    shape instead of graph-resolved ``symbols``, this flips B->A and reddens."""
    a = extract_issue_anchors(ISSUE_B, tiny_graph_db)
    assert a.symbols == set()  # nothing resolves
    assert a.symbols_pre_stopword  # ...but shape tokens are present
    assert classify_stratum(ISSUE_B, tiny_graph_db).label == "B"


def test_C_traceback_beats_resolving_symbol(tiny_graph_db: str) -> None:
    """Precedence C>A: ISSUE_C resolves SafeWatchdog+_fd yet a traceback wins."""
    r = classify_stratum(ISSUE_C, tiny_graph_db)
    assert r.label == "C"
    assert r.evidence["n_resolved"] >= 1  # symbols DID resolve; C still wins


def test_D_feature_add_beats_resolving_symbol(tiny_graph_db: str) -> None:
    """Precedence D>A: one resolving symbol (SafeWatchdog) must not steal a
    feature-add whose unresolved tokens dominate."""
    r = classify_stratum(ISSUE_D, tiny_graph_db)
    assert r.label == "D"
    assert r.evidence["n_unresolved"] >= r.evidence["n_resolved"] >= 1


def test_precedence_C_over_D(tiny_graph_db: str) -> None:
    """A traceback outranks even a dominant feature-add (C>D)."""
    r = classify_stratum(ISSUE_CD, tiny_graph_db)
    assert r.evidence["has_traceback"] is True
    assert r.label == "C"


def test_no_graph_symbol_issue_degrades_to_B() -> None:
    """correct-or-quiet SIDE-DOOR PIN. With no graph, ``symbols`` is filled with
    raw un-cross-checked tokens; A must NOT fire on them (that is #51 again).
    Degrade to B."""
    assert classify_stratum(ISSUE_A, graph_db=None).label == "B"
    assert classify_stratum(ISSUE_D, graph_db=None).label == "B"


def test_no_graph_traceback_still_C() -> None:
    """C is textual (deepest-frame), so it fires without a graph."""
    assert classify_stratum(ISSUE_C, graph_db=None).label == "C"


def test_passed_anchors_are_trusted_resolved(tiny_graph_db: str) -> None:
    """A caller may hoist ``IssueAnchors`` once (the brief does) and pass them in;
    they are trusted graph-resolved, so A fires even with graph_db=None."""
    anchors = extract_issue_anchors(ISSUE_A, tiny_graph_db)
    r = classify_stratum(ISSUE_A, graph_db=None, issue_anchors=anchors)
    assert r.label == "A"
    assert classify_stratum(ISSUE_A, tiny_graph_db).label == r.label


def test_evidence_sorted_and_deterministic(tiny_graph_db: str) -> None:
    r1 = classify_stratum(ISSUE_D, tiny_graph_db)
    r2 = classify_stratum(ISSUE_D, tiny_graph_db)
    assert r1 == r2  # frozen dataclass, byte-identical
    assert r1.evidence["unresolved_symbols"] == sorted(r1.evidence["unresolved_symbols"])
    assert r1.evidence["resolved_anchors"] == sorted(r1.evidence["resolved_anchors"])
