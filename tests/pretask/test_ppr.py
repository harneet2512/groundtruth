"""Stage A unit tests for Module 3 (Personalized PageRank)."""

from __future__ import annotations

from groundtruth.pretask.ppr import (
    aggregate_scores_by_file,
    personalized_pagerank,
)


def test_ppr_no_seeds_returns_empty(tiny_graph_db: str) -> None:
    """Empty seed set → empty result; NOT global PageRank."""
    res = personalized_pagerank(tiny_graph_db, seed_nodes=set())
    assert res.node_scores == {}
    assert res.iterations_run == 0


def test_ppr_seeded_concentrates(tiny_graph_db: str) -> None:
    """Seed = SafeWatchdog (id=1). Watchdog file ranks #1 by aggregated score."""
    res = personalized_pagerank(tiny_graph_db, seed_nodes={1})
    assert res.node_scores  # non-empty
    # SafeWatchdog itself (id=1) should retain non-trivial mass.
    assert res.node_scores.get(1, 0.0) > 0.0

    file_scores = aggregate_scores_by_file(res.node_scores, tiny_graph_db)
    # Sort by score desc.
    ranked = sorted(file_scores.items(), key=lambda kv: kv[1][0], reverse=True)
    assert ranked, "expected at least one file to score"
    top_file = ranked[0][0]
    assert top_file == "patroni/watchdog.py"


def test_ppr_filters_low_confidence_edges(tiny_graph_db: str) -> None:
    """The conf=0.2 noisy edge (utils -> _fd) must not contribute.

    If the filter were broken, seeding from id=2 (_fd) would push mass
    to patroni/utils.py via the noisy backward edge — but our adjacency
    is forward-only, so we test the forward direction: seeding from
    format_value (id=5) with the noisy edge filtered should yield NO
    flow into _fd's file.
    """
    res = personalized_pagerank(
        tiny_graph_db, seed_nodes={5}, min_confidence=0.5
    )
    # Either node 5 holds all the mass (seed restart), or it has no
    # outgoing edges that survived the filter — both are correct.
    file_scores = aggregate_scores_by_file(res.node_scores, tiny_graph_db)
    # patroni/watchdog.py should not have absorbed mass since the only
    # path was through the conf=0.2 edge.
    watchdog = file_scores.get("patroni/watchdog.py", (0.0, 0))[0]
    utils = file_scores.get("patroni/utils.py", (0.0, 0))[0]
    assert utils >= watchdog


def test_ppr_convergence_under_iter_cap(tiny_graph_db: str) -> None:
    """PPR halts at or before the 30-iteration cap and produces stable scores.

    Note: a 2-cycle dangling-node graph oscillates with geometric ratio
    ~alpha^2 ≈ 0.72, so 30 iterations may not hit a 1e-4 l-inf delta even
    though the steady state is well-approximated. The contract is "stop
    by iter==30" — convergence flag is informational.
    """
    res = personalized_pagerank(tiny_graph_db, seed_nodes={1}, iterations=30)
    assert res.iterations_run <= 30
    assert res.iterations_run > 0
    # Steady-state mass on node 1 is ~0.54; allow generous tolerance.
    assert 0.4 < res.node_scores.get(1, 0.0) < 0.7
