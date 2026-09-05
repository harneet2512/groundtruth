"""Shared SQL gates for graph evidence consumers."""

from __future__ import annotations

import sqlite3

from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS

MIN_EDGE_CONFIDENCE = 0.5
SUPPRESSED_TRUST_TIER = "SUPPRESSED"
STRONG_TRUST_TIERS = ("CERTIFIED", "CANDIDATE")


def edge_fact_clause(conn: sqlite3.Connection, alias: str = "e") -> str:
    """Return a SQL expression for fact-grade CALLS evidence.

    New graphs use categorical provenance. Older graphs degrade to the historical
    numeric confidence floor instead of failing open.
    """
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)").fetchall()}
    except sqlite3.Error:
        return f"COALESCE({alias}.confidence, 0.5) >= {MIN_EDGE_CONFIDENCE}"
    if {"resolution_method", "trust_tier", "candidate_count"}.issubset(cols):
        methods = ", ".join(f"'{m}'" for m in sorted(DETERMINISTIC_RESOLUTION_METHODS))
        tiers = ", ".join(f"'{t}'" for t in STRONG_TRUST_TIERS)
        return (
            f"(("
            f"LOWER(TRIM(COALESCE({alias}.resolution_method, ''))) IN ({methods}) "
            f"OR (UPPER(TRIM(COALESCE({alias}.trust_tier, 'SPECULATIVE'))) IN ({tiers}) "
            f"AND LOWER(TRIM(COALESCE({alias}.resolution_method, ''))) NOT LIKE 'name_match%')"
            f") AND UPPER(TRIM(COALESCE({alias}.trust_tier, 'SPECULATIVE'))) != '{SUPPRESSED_TRUST_TIER}')"
        )
    if "confidence" in cols:
        return f"COALESCE({alias}.confidence, 0.5) >= {MIN_EDGE_CONFIDENCE}"
    return "0=1"


def edge_fact_and(conn: sqlite3.Connection, alias: str = "e") -> str:
    """Return the fact clause with a leading AND for existing WHERE snippets."""
    return " AND " + edge_fact_clause(conn, alias=alias)
