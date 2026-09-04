"""Regression lock (I3): post_edit._categorical_edge_filter_clause must NEVER admit a
name_match edge as a fact — regardless of candidate_count, confidence, or trust_tier.

Found by the per-layer LIPI (wx2eywcer): the docstring claimed a 'name_match cc<=1'
admit (the old os.walk -> account.walk launder) that the SQL correctly does NOT
implement. This test fails if a future 'make code match docstring' edit re-introduces it.
"""

from __future__ import annotations

import sqlite3

from groundtruth.hooks.post_edit import (
    _categorical_edge_filter_clause,
    _STRONG_RESOLUTION_METHODS,
)


def _admits(tmp_path, method, tier, cc=1, conf=0.95) -> bool:
    conn = sqlite3.connect(":memory:")  # fresh db per call (caller may invoke twice)
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, type TEXT, resolution_method TEXT, "
        "trust_tier TEXT, candidate_count INTEGER, confidence REAL)"
    )
    conn.execute(
        "INSERT INTO edges (type, resolution_method, trust_tier, candidate_count, "
        "confidence) VALUES ('CALLS',?,?,?,?)",
        (method, tier, cc, conf),
    )
    conn.commit()
    clause = _categorical_edge_filter_clause()
    n = conn.execute(f"SELECT COUNT(*) FROM edges e WHERE e.type='CALLS' AND {clause}").fetchone()[
        0
    ]
    conn.close()
    return n > 0


def test_namematch_cc1_never_admitted(tmp_path):
    # The os.walk launder: a unique-by-name name_match (cc=1, high conf, CANDIDATE) is a
    # GUESS, not a fact. The docstring once claimed this is admitted; it must not be.
    assert not _admits(tmp_path, "name_match", "CANDIDATE", cc=1, conf=0.95)


def test_namematch_certified_tier_never_admitted(tmp_path):
    assert not _admits(tmp_path, "name_match", "CERTIFIED", cc=1, conf=0.99)


def test_namematch_qualified_unresolved_variant_never_admitted(tmp_path):
    # NOT LIKE 'name_match%' covers the variants directly, not only via their tier.
    assert not _admits(tmp_path, "name_match_qualified_unresolved", "CANDIDATE")
    assert not _admits(tmp_path, "name_match_ambiguous", "CERTIFIED")


def test_deterministic_method_admitted(tmp_path):
    method = sorted(_STRONG_RESOLUTION_METHODS)[0]
    assert _admits(tmp_path, method, "CANDIDATE")


def test_suppressed_tier_hard_excluded_even_for_strong_method(tmp_path):
    method = sorted(_STRONG_RESOLUTION_METHODS)[0]
    assert not _admits(tmp_path, method, "SUPPRESSED")
