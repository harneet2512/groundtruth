"""Fable L3/L7 (RED→GREEN, Stage-1 deterministic): the test/generated/tooling demote was
applied as ``score -= 0.4/0.5``, but under the BAKED-ON V2 RRF fusion the final localize()
sort keys on rank fusion — NEVER ``c.score`` — so the demote was DEAD: a ``.pb.go`` / ``_pb2.py``
/ vendored file re-entered top-k, and the negative demote also polluted the confidence gate's
flatness MAD (L7). The fix moves the demote into an ORDERING stratum (_nonsource_stratum) placed
AFTER the grep-recall floor (preserving the Phase-2 grep invariant) but ABOVE rank fusion, so a
high-RRF non-source file cannot outrank real source.

No task IDs, no gold labels, no benchmark symbols — keyed purely on path/witness STRUCTURE.
"""
from __future__ import annotations

import sqlite3

from groundtruth.pretask.graph_localizer import _is_generated, localize


def _make_stratum_db(tmp_path) -> str:
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
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
            # GENERATED file (real codegen suffix `_pb2.py`) — and it carries the STRONGER
            # signal: a verified CALLS edge points at its `process`. Its path also sorts
            # alphabetically FIRST, so without the stratum it wins BOTH the rrf3 rank and any
            # path tie-break.
            (1, "Method", "process", "aaa_pb2.py", 10, 40, "def process(self, payload):"),
            # SOURCE file — the same symbol, defines-only (weaker witness), path sorts LAST.
            (2, "Method", "process", "zzz_service.py", 10, 40, "def process(self, payload):"),
            # a caller that CALLS the GENERATED process (verified) → generated gets the edge.
            (3, "Function", "run", "main.py", 1, 8, "def run():"),
        ],
    )
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES "
        "(1,3,1,'CALLS',5,'main.py','import',1.0)"
    )
    conn.commit()
    conn.close()
    return db


def test_generated_suffix_is_recognized() -> None:
    # Guards the premise: `_pb2.py` must be a generated suffix, else the test is vacuous.
    assert _is_generated("aaa_pb2.py")
    assert not _is_generated("zzz_service.py")


def test_localize_generated_sinks_below_source_despite_stronger_witness(tmp_path) -> None:
    """The generated `aaa_pb2.py` has a verified caller edge (stronger rrf3) AND sorts first
    alphabetically — under the dead score-demote it ranked ABOVE the source `zzz_service.py`.
    The _nonsource_stratum must sink it BELOW real source in the final order.

    Mutation check: removing `_nonsource_stratum(c)` from the localize() sort key ranks
    `aaa_pb2.py` first again → RED.
    """
    issue = (
        "process handles the payload incorrectly. When run() calls process the "
        "payload validation is skipped."
    )
    db = _make_stratum_db(tmp_path)
    res = localize(issue, db)
    paths = [c.file_path for c in res.candidates]
    assert "zzz_service.py" in paths, f"source not a candidate: {paths}"
    assert "aaa_pb2.py" in paths, f"generated not a candidate: {paths}"
    src_rank = paths.index("zzz_service.py")
    gen_rank = paths.index("aaa_pb2.py")
    assert src_rank < gen_rank, (
        f"L3: generated _pb2.py (rank {gen_rank}) outranked real source (rank {src_rank}) — "
        f"the non-source stratum must sink generated/vendored/test below source: {paths}"
    )
