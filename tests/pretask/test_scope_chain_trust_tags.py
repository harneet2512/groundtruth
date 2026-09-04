"""TTD (red->green) for finding (A): scope-chain edge trust must be VISIBLE.

SEAM: graph_localizer._build_scope_chains admits CERTIFIED (deterministic),
CANDIDATE (promote_%), and SPECULATIVE (name_match >= floor) cross-file edges into
the rendered ``description`` (the agent-visible "Scope chain ... Chain: A -> B (sym)"
line). _scope_edge_trust's own docstring promises a SPECULATIVE edge is "rendered
with an explicit (unverified) tag, never as a fact" — but the producer originally
tagged only CANDIDATE and left SPECULATIVE BARE, so a name_match scope edge at
conf >= floor laundered as a verified graph fact (Pillar-3 correct-or-quiet defect).

This asserts the load-bearing contract:
  1. a SPECULATIVE (name_match >= floor) scope edge renders ' (unverified)'
  2. a CANDIDATE (promote_%) scope edge renders ' (CANDIDATE)'
  3. a CERTIFIED (deterministic) scope edge renders BARE (no tag)

Pure sqlite. No AI, no task symbols, no per-case weight tuning.
"""

from __future__ import annotations

import sqlite3

from groundtruth.pretask.graph_localizer import (
    _NAME_MATCH_FLOOR,
    _build_scope_chains,
)


class _Cand:
    """Minimal candidate stand-in — _build_scope_chains reads only .file_path."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path


_FILE_A = "pkg/a.py"
_FILE_B = "pkg/b.py"


def _one_edge_db(tmp_path, *, etype: str, method: str, conf: float) -> sqlite3.Connection:
    """Two files joined by exactly ONE cross-file edge of the given (type, method,
    confidence). Returns an open connection over the synthetic graph."""
    conn = sqlite3.connect(str(tmp_path / "graph.db"))
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,"
        "is_test,language) VALUES (?,?,?,?,?,?,?,0,'python')",
        [
            (1, "Function", "alpha", _FILE_A, 1, 10, "def alpha():"),
            (2, "Function", "beta", _FILE_B, 1, 10, "def beta():"),
        ],
    )
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,resolution_method,confidence)"
        " VALUES (1,1,2,?,?,?)",
        (etype, method, conf),
    )
    conn.commit()
    return conn


def _chain_desc(tmp_path, *, etype: str, method: str, conf: float) -> str:
    conn = _one_edge_db(tmp_path, etype=etype, method=method, conf=conf)
    try:
        chains = _build_scope_chains(
            [_Cand(_FILE_A), _Cand(_FILE_B)],
            conn,
            True,  # type: ignore[arg-type]
        )
    finally:
        conn.close()
    assert chains, "expected a connected 2-file scope chain"
    return chains[0].description


def test_speculative_name_match_edge_renders_unverified(tmp_path):
    # name_match at/above the floor is admitted (byte-identical reach) but is a NAME
    # GUESS -> it MUST carry '(unverified)', never bare (the laundering this closes).
    desc = _chain_desc(tmp_path, etype="CALLS", method="name_match", conf=_NAME_MATCH_FLOOR + 0.1)
    assert "(unverified)" in desc, desc
    assert "(CANDIDATE)" not in desc, desc


def test_candidate_promote_edge_renders_candidate(tmp_path):
    # a promote_% edge is derived-from-facts but NOT verified -> explicit (CANDIDATE).
    desc = _chain_desc(tmp_path, etype="READS", method="promote_reads", conf=0.9)
    assert "(CANDIDATE)" in desc, desc
    assert "(unverified)" not in desc, desc


def test_certified_deterministic_edge_renders_bare(tmp_path):
    # a deterministic resolution (import) is a FACT -> renders with NO trust tag.
    desc = _chain_desc(tmp_path, etype="CALLS", method="import", conf=1.0)
    assert "(unverified)" not in desc, desc
    assert "(CANDIDATE)" not in desc, desc


def _trust_tier_db(tmp_path, tier, name="tt.db"):
    db = str(tmp_path / name)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, "
        "target_id INTEGER, type TEXT, resolution_method TEXT, confidence REAL, "
        "trust_tier TEXT)"
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path) VALUES (?,?,?,?)",
        [(1, "Function", "alpha", _FILE_A), (2, "Function", "beta", _FILE_B)],
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,"
        "trust_tier) VALUES (1,2,'CALLS','import',0.95,?)",
        (tier,),
    )
    conn.commit()
    return conn


def test_suppressed_edge_is_never_a_scope_member(tmp_path):
    # A SUPPRESSED edge (even high confidence) must NOT build a scope chain -- parity
    # with the witness BFS _edge_admitted (Unit 5b, defense-in-depth).
    conn = _trust_tier_db(tmp_path, "SUPPRESSED")
    chains = _build_scope_chains([_Cand(_FILE_A), _Cand(_FILE_B)], conn, True)  # type: ignore[arg-type]
    conn.close()
    assert not chains


def test_certified_edge_with_trust_tier_column_builds_chain(tmp_path):
    # control: CERTIFIED -> chain built (proves the trust_tier column path works).
    conn = _trust_tier_db(tmp_path, "CERTIFIED", name="cc.db")
    chains = _build_scope_chains([_Cand(_FILE_A), _Cand(_FILE_B)], conn, True)  # type: ignore[arg-type]
    conn.close()
    assert chains
