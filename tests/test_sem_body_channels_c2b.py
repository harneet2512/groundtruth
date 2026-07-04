"""C2b — GT_SEM_BODY: consume the index-time body channels from graph.db (offline).

Offline-provable surface ONLY: the passage-assembly template, byte-identical-off, the
channel being LOAD-BEARING (swap channels between two symbols => the ON passage changes),
leak=0 (is_test excluded), and the parameter-free DF stoplist. The stratum-B retrieval
LIFT needs measure_brief.py + the ONNX embed env => Phase-4, NOT asserted here (per
BRIEFING.md: no ranking claim without the guarded harness + semantic-ON container).

Each test drives the REAL _assemble_symbol_passages / _boilerplate_stoplist against a
hand-built graph.db — no embedder, no file I/O.
"""

import sqlite3

import pytest

from groundtruth.pretask.graph_localizer import (
    _assemble_symbol_passages,
    _boilerplate_stoplist,
    _normalize,
)
from groundtruth.memory.enrich.embed import symbol_passage


def _make_graph(tmp_path, rows_nodes, rows_props, name="graph.db"):
    """rows_nodes: (id, file_path, name, signature, is_test)
    rows_props: (node_id, kind, value)"""
    db = str(tmp_path / name)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, file_path TEXT, name TEXT, "
        "signature TEXT, start_line INTEGER, end_line INTEGER, is_test INTEGER)"
    )
    conn.execute(
        "CREATE TABLE properties (node_id INTEGER, kind TEXT, value TEXT, line INTEGER, confidence REAL)"
    )
    for nid, fp, nm, sig, is_test in rows_nodes:
        conn.execute(
            "INSERT INTO nodes (id, file_path, name, signature, start_line, end_line, is_test) "
            "VALUES (?,?,?,?,?,?,?)", (nid, fp, nm, sig, 1, 20, is_test),
        )
    for node_id, kind, value in rows_props:
        conn.execute(
            "INSERT INTO properties (node_id, kind, value, line, confidence) VALUES (?,?,?,?,?)",
            (node_id, kind, value, 1, 1.0),
        )
    conn.commit()
    conn.close()
    return db


# Two non-test symbols in one file, each with docstring + the 3 new channels.
NODES = [
    (1, "svc/redis.py", "connect", "(host)", 0),
    (2, "svc/redis.py", "shutdown", "(force)", 0),
]
PROPS = [
    (1, "docstring", "open the connection"),
    (1, "string_literals", "redis://localhost:6379"),
    (1, "calls", "verify_certificate open_socket"),
    (1, "body_terms", "host url verify_certificate handshake tls establish"),
    (2, "docstring", "close the connection"),
    (2, "string_literals", "shutting down"),
    (2, "calls", "flush_buffers close_socket"),
    (2, "body_terms", "force flush drain close teardown"),
]


def test_off_byte_identical_to_props_only(tmp_path):
    db = _make_graph(tmp_path, NODES, PROPS)
    want = {_normalize("svc/redis.py")}
    fp_off, _ = _assemble_symbol_passages(db, want, body_on=False)
    got = fp_off[_normalize("svc/redis.py")]
    # OFF == symbol_passage(name, sig, DOCSTRING-only props) — the pre-C2b behavior.
    exp1 = symbol_passage("connect", "(host)", "open the connection")
    exp2 = symbol_passage("shutdown", "(force)", "close the connection")
    assert got == [exp1, exp2]
    # OFF passages carry NONE of the new-channel vocabulary.
    for p in got:
        for leaked in ("redis://localhost", "handshake", "verify_certificate", "flush_buffers"):
            assert leaked not in p


def test_on_passage_contains_channel_vocabulary(tmp_path):
    db = _make_graph(tmp_path, NODES, PROPS)
    want = {_normalize("svc/redis.py")}
    fp_on, _ = _assemble_symbol_passages(db, want, body_on=True)
    p1 = fp_on[_normalize("svc/redis.py")][0]  # connect
    # string_literals + calls + body_terms vocabulary now present.
    for want_tok in ("redis://localhost:6379", "verify_certificate", "handshake", "tls"):
        assert want_tok in p1, f"ON passage missing {want_tok!r}: {p1!r}"


def test_on_channel_is_load_bearing_not_name_leakage(tmp_path):
    """MUTATION companion: swap string_literals + body_terms between the two symbols.
    The NAMES/signatures are unchanged, so if the passage were driven by name only, the
    ON passages would be identical. Because the body CHANNEL is load-bearing, connect's ON
    passage must change (now carries shutdown's vocabulary)."""
    db = _make_graph(tmp_path, NODES, PROPS)
    want = {_normalize("svc/redis.py")}
    base = _assemble_symbol_passages(db, want, body_on=True)[0][_normalize("svc/redis.py")][0]

    swapped_props = []
    for node_id, kind, value in PROPS:
        if kind in ("string_literals", "body_terms"):
            other = 2 if node_id == 1 else 1  # swap between the two symbols
            swapped_props.append((other, kind, value))
        else:
            swapped_props.append((node_id, kind, value))
    db2 = _make_graph(tmp_path, NODES, swapped_props, name="graph_swap.db")
    swapped = _assemble_symbol_passages(db2, want, body_on=True)[0][_normalize("svc/redis.py")][0]

    assert base != swapped, "ON passage did not change when body channels were swapped — channel is DEAD"
    # connect now carries shutdown's vocabulary (proves the swap actually took effect).
    assert "flush_buffers" in swapped or "teardown" in swapped


def test_leak_zero_test_symbol_excluded(tmp_path):
    nodes = NODES + [(3, "svc/redis.py", "test_gold", "()", 1)]  # is_test=1
    props = PROPS + [
        (3, "string_literals", "secret-gold-token"),
        (3, "body_terms", "FAIL_TO_PASS gold assert marker"),
    ]
    db = _make_graph(tmp_path, nodes, props)
    want = {_normalize("svc/redis.py")}
    fp_on, _ = _assemble_symbol_passages(db, want, body_on=True)
    for p in fp_on[_normalize("svc/redis.py")]:
        assert "secret-gold-token" not in p
        assert "FAIL_TO_PASS" not in p
    # only the 2 non-test symbols produced passages
    assert len(fp_on[_normalize("svc/redis.py")]) == 2


def test_stoplist_is_distribution_derived(tmp_path):
    # "self" appears in every symbol (ubiquitous boilerplate); domain tokens appear once.
    node_terms = {
        1: "self redis connect",
        2: "self tls handshake",
        3: "self socket verify",
        4: "self buffer flush",
    }
    stop = _boilerplate_stoplist(node_terms)
    assert "self" in stop  # df=4 (all symbols) — above mean+std
    for kept in ("redis", "tls", "handshake", "socket", "buffer"):
        assert kept not in stop  # df=1 rare domain tokens survive


def test_stoplist_empty_below_two_tokens():
    assert _boilerplate_stoplist({}) == set()
    assert _boilerplate_stoplist({1: "onlyone"}) == set()  # single distinct token -> no distribution


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
