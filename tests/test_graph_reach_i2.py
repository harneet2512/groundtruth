"""I2 (depth↔rank isolation) regression tests for graph_reach.

CLUSTER 1 — RANK-SAFETY. Covers gaps:

  G01 — `_build_file_graph` SELECTed ALL edge types untyped, so promoted DEPTH
        edges (READS/WRITES/RAISES/CO_SERIALIZES/PRECEDES/DATA_FLOW + any
        `promote_%`-provenance edge) accrued reach_score (→ W_REACH in the
        localizer RANK) and were admitted to the graph_expand candidate set — a
        direct violation of invariant I2 ("depth never enters reach/RANK").

  G16 — explicit invariant: the reach BFS / graph_expand MUST exclude
        type='DATA_FLOW' and every other promote_* type, so a future reach
        consumer that unions edge types can never silently let intra-procedural
        data-flow depth enter ranking.

  G18 — the depth-type blacklist is the SINGLE SOURCE OF TRUTH imported from
        graph_localizer (`_PROMOTED_EDGE_TYPES`); reach and the localizer degree
        filter can never drift.

THE INVARIANT under test (the catalog's required regression):
    A graph WITH promoted READS/WRITES (and the rest of the depth set) yields
    BYTE-IDENTICAL reach_scores AND graph_expand to the same graph with those
    edges removed. Promoted depth is reach-invisible.

These are deterministic UNIT tests over an in-memory graph.db — no benchmark,
no agent, no task run.
"""

from __future__ import annotations

import sqlite3

import pytest

from groundtruth.pretask.graph_reach import (
    compute_reach,
    graph_expand_candidates,
)
from groundtruth.pretask.graph_localizer import _PROMOTED_EDGE_TYPES


# ---------------------------------------------------------------------------
# Fixture: a tiny multi-file graph.db with a known structural skeleton plus
# the full set of promoted depth edges layered on top.
# ---------------------------------------------------------------------------

# file_path per node id (anchor = a.py)
_NODES = {
    1: "a.py",  # anchor (source)
    2: "b.py",  # structural neighbor via CALLS
    3: "c.py",  # structural neighbor via IMPORTS
    4: "d.py",  # reachable ONLY via a depth edge (must stay invisible)
    5: "e.py",  # 2-hop structural neighbor from b.py
}

# (source_id, target_id, type, resolution_method, confidence)
# Structural skeleton — the ONLY edges reach is allowed to traverse.
_STRUCTURAL_EDGES = [
    (1, 2, "CALLS", "import", 1.0),
    (1, 3, "IMPORTS", "import", 1.0),
    (2, 5, "CALLS", "same_file", 1.0),
    (3, 5, "EXTENDS", "import", 0.9),
]

# Promoted DEPTH edges — every type in the blacklist + a `promote_%`-provenance
# CALLS edge (provenance-tagged depth that must ALSO be excluded). These wire up
# brand-new navigable hops (incl. to node 4 = d.py) that reach MUST ignore.
_DEPTH_EDGES = [
    (1, 4, "READS", "promote_reads", 1.0),
    (1, 4, "WRITES", "promote_writes", 1.0),
    (1, 4, "DATA_FLOW", "promote_dataflow", 1.0),
    (1, 4, "RAISES", "promote_raises", 1.0),
    (2, 4, "CO_SERIALIZES", "promote_coserializes", 1.0),
    (3, 4, "PRECEDES", "promote_precedes", 1.0),
    # provenance-tagged depth carried on an otherwise-structural type:
    (1, 4, "CALLS", "promote_callsfact", 1.0),
]


def _make_graph(path: str, *, with_depth: bool) -> None:
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, language TEXT)"
    )
    c.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, "
        "target_id INTEGER, type TEXT, resolution_method TEXT, confidence REAL)"
    )
    c.executemany(
        "INSERT INTO nodes (id, name, file_path, language) VALUES (?, ?, ?, 'python')",
        [(nid, fp.split(".")[0], fp) for nid, fp in _NODES.items()],
    )
    edges = list(_STRUCTURAL_EDGES)
    if with_depth:
        edges += _DEPTH_EDGES
    c.executemany(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        edges,
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def graph_with_depth(tmp_path):
    p = str(tmp_path / "with_depth.db")
    _make_graph(p, with_depth=True)
    return p


@pytest.fixture()
def graph_without_depth(tmp_path):
    p = str(tmp_path / "no_depth.db")
    _make_graph(p, with_depth=False)
    return p


_ANCHORS = ["a.py"]


# ---------------------------------------------------------------------------
# G01 + the catalog invariant: WITH-depth ≡ WITHOUT-depth (byte-identical).
# ---------------------------------------------------------------------------


def test_reach_scores_byte_identical_with_and_without_depth(graph_with_depth, graph_without_depth):
    """reach_score must be byte-identical whether or not promoted depth exists."""
    with_d = compute_reach(_ANCHORS, graph_with_depth, max_depth=3)
    no_d = compute_reach(_ANCHORS, graph_without_depth, max_depth=3)

    assert set(with_d.keys()) == set(no_d.keys()), (
        "depth edges changed the reachable file SET (candidate leak)"
    )
    for fp in no_d:
        # exact float equality — promoted depth must contribute ZERO reach
        assert with_d[fp].reach_score == no_d[fp].reach_score, (
            f"reach_score for {fp} differs with depth present: "
            f"{with_d[fp].reach_score!r} != {no_d[fp].reach_score!r}"
        )
        assert with_d[fp].min_path_length == no_d[fp].min_path_length
        assert with_d[fp].entered_via_graph == no_d[fp].entered_via_graph


def test_graph_expand_byte_identical_with_and_without_depth(graph_with_depth, graph_without_depth):
    """The graph_expand candidate set must be byte-identical with/without depth."""
    with_d = graph_expand_candidates(_ANCHORS, graph_with_depth, max_depth=3)
    no_d = graph_expand_candidates(_ANCHORS, graph_without_depth, max_depth=3)
    assert with_d == no_d, f"depth edges leaked into graph_expand candidates: extra={with_d - no_d}"


# ---------------------------------------------------------------------------
# G16: the depth-only target (d.py, reachable ONLY via promoted edges) must
# never appear in reach/expand. This is the navigable-hop leak the invariant
# pins shut as future RANK consumers evolve.
# ---------------------------------------------------------------------------


def test_depth_only_target_never_enters_reach(graph_with_depth):
    reach = compute_reach(_ANCHORS, graph_with_depth, max_depth=3)
    assert "d.py" not in reach, (
        "d.py is reachable ONLY via promoted depth edges; it MUST NOT accrue reach"
    )


def test_depth_only_target_never_enters_expand(graph_with_depth):
    cands = graph_expand_candidates(_ANCHORS, graph_with_depth, max_depth=3)
    assert "d.py" not in cands, (
        "d.py is reachable ONLY via promoted depth edges; it MUST NOT be a candidate"
    )


# ---------------------------------------------------------------------------
# G16 (explicit per-type exclusion) + G18 (SSOT): every blacklisted type AND
# the promote_% provenance are individually excluded from the reach adjacency.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("depth_type", _PROMOTED_EDGE_TYPES)
def test_each_promoted_type_excluded_from_reach(tmp_path, depth_type):
    """A graph whose ONLY edge to a file is `depth_type` must not reach it."""
    p = str(tmp_path / f"single_{depth_type}.db")
    conn = sqlite3.connect(p)
    c = conn.cursor()
    c.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, file_path TEXT)")
    c.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, "
        "target_id INTEGER, type TEXT, resolution_method TEXT, confidence REAL)"
    )
    c.executemany(
        "INSERT INTO nodes (id, file_path) VALUES (?, ?)",
        [(1, "a.py"), (2, "z.py")],
    )
    # the ONLY edge is the blacklisted depth type, at full confidence
    c.execute(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
        "VALUES (1, 2, ?, NULL, 1.0)",
        (depth_type,),
    )
    conn.commit()
    conn.close()

    reach = compute_reach(["a.py"], p, max_depth=3)
    assert "z.py" not in reach, (
        f"edge type {depth_type!r} (a promoted DEPTH type) leaked into reach"
    )
    # graph_expand always contains the anchor itself; the invariant is that the
    # depth-only target z.py is NOT admitted as a new candidate.
    assert "z.py" not in graph_expand_candidates(["a.py"], p, max_depth=3)


def test_promote_provenance_excluded_even_on_structural_type(tmp_path):
    """A CALLS edge tagged `promote_%` must be excluded (provenance, not type)."""
    p = str(tmp_path / "promote_calls.db")
    conn = sqlite3.connect(p)
    c = conn.cursor()
    c.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, file_path TEXT)")
    c.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, "
        "target_id INTEGER, type TEXT, resolution_method TEXT, confidence REAL)"
    )
    c.executemany(
        "INSERT INTO nodes (id, file_path) VALUES (?, ?)",
        [(1, "a.py"), (2, "z.py")],
    )
    c.execute(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
        "VALUES (1, 2, 'CALLS', 'promote_fact', 1.0)"
    )
    conn.commit()
    conn.close()
    assert "z.py" not in compute_reach(["a.py"], p, max_depth=3)
    assert "z.py" not in graph_expand_candidates(["a.py"], p, max_depth=3)


def test_structural_edges_still_traversed(graph_without_depth):
    """Sanity: the fix does NOT break legitimate structural reach."""
    reach = compute_reach(_ANCHORS, graph_without_depth, max_depth=3)
    # b.py, c.py (1-hop) and e.py (2-hop) are structurally reachable
    assert {"b.py", "c.py", "e.py"}.issubset(set(reach.keys()))
    for fp in ("b.py", "c.py", "e.py"):
        assert reach[fp].reach_score > 0.0
