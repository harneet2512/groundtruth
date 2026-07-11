"""B-24 (curation_map edge_metadata consumer).

``edges.metadata`` is polymorphic (route JSON on API edges vs ``;``-separated key=value on
promoted CALLS). The Go indexer normalizes both into the derived ``edge_metadata`` sub-table
via ONE canonical parser; the Python consumer must (1) provide the byte-faithful
``parse_edge_metadata`` twin for the fallback and (2) detect the ``dataflow`` annotation via
the ``edge_metadata`` table when present, falling back to the raw-string ``LIKE`` on an old
graph.db.

RED (pre-fix): ``_focus_depth_rels`` used ``e.metadata LIKE '%dataflow=%'`` unconditionally —
a stray ``…dataflow=`` substring inside ANOTHER key's value false-matched, and there was no
``parse_edge_metadata``. GREEN: the table JOIN keys on ``em.key='dataflow'`` exactly; the
fallback parser is deterministic + total.
"""
from __future__ import annotations

import sqlite3

from groundtruth.pretask.curation_map import (
    _focus_depth_rels,
    _has_columns,
    parse_edge_metadata,
)


# ----------------------------------------------------- parse_edge_metadata twin
def test_parse_semicolon_kv():
    assert parse_edge_metadata("receiver_type=UserRepo;dataflow=fetch;usage=return") == {
        "receiver_type": "UserRepo", "dataflow": "fetch", "usage": "return"
    }


def test_parse_json_object_scalars_stringified():
    assert parse_edge_metadata('{"route":"/orders","method":"GET","cnt":3,"ok":true}') == {
        "route": "/orders", "method": "GET", "cnt": "3", "ok": "true"
    }


def test_parse_total_on_empty_and_malformed():
    assert parse_edge_metadata("") == {}
    assert parse_edge_metadata(None) == {}          # type: ignore[arg-type]
    assert parse_edge_metadata("=noKey;alsoBad") == {}   # empty key / no '=' -> skipped
    # malformed JSON falls through to key=value parsing (parity with Go)
    assert parse_edge_metadata('{oops') == {}


# ------------------------------------------------ _focus_depth_rels via the table
def _mk(conn):
    conn.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " is_test INTEGER, language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
    )
    # focus func (id 1) CALLS callee (id 2) over a deterministic edge annotated dataflow=save
    conn.execute("INSERT INTO nodes VALUES(1,'Function','handler','','svc/h.py',1,9,0,'python')")
    conn.execute("INSERT INTO nodes VALUES(2,'Function','save','','svc/db.py',1,9,0,'python')")
    conn.execute(
        "INSERT INTO edges(id,source_id,target_id,type,resolution_method,confidence,metadata)"
        " VALUES(10,1,2,'CALLS','import',0.95,'dataflow=save')"
    )
    conn.commit()


def _dataflow_targets(conn):
    has_conf, has_method = _has_columns(conn)
    rels = _focus_depth_rels(conn, [1], has_conf=has_conf, has_method=has_method)
    return sorted(d.target for d in rels if d.kind == "DATA_FLOW")


def test_dataflow_via_edge_metadata_table():
    conn = sqlite3.connect(":memory:")
    _mk(conn)
    conn.executescript(
        "CREATE TABLE edge_metadata(edge_id INTEGER, key TEXT, value TEXT,"
        " schema_version INTEGER, PRIMARY KEY(edge_id,key))"
    )
    conn.execute("INSERT INTO edge_metadata VALUES(10,'dataflow','save',1)")
    conn.commit()
    assert _dataflow_targets(conn) == ["save"]


def test_dataflow_fallback_like_when_table_absent():
    """Old graph.db (no edge_metadata table) -> fall back to the raw-string LIKE:
    byte-identical DATA_FLOW result."""
    conn = sqlite3.connect(":memory:")
    _mk(conn)  # no edge_metadata table
    assert _dataflow_targets(conn) == ["save"]


def test_empty_edge_metadata_table_falls_back():
    """The table EXISTS but is EMPTY (indexed before populate) -> fall back to the raw
    string so the dataflow annotation is not lost."""
    conn = sqlite3.connect(":memory:")
    _mk(conn)
    conn.execute(
        "CREATE TABLE edge_metadata(edge_id INTEGER, key TEXT, value TEXT,"
        " schema_version INTEGER, PRIMARY KEY(edge_id,key))"
    )  # left EMPTY
    conn.commit()
    assert _dataflow_targets(conn) == ["save"]
