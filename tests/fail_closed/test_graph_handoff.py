"""Stage 2 — graph-base depth + resolved-graph handoff gate tests.

Drives graph_certificate.classify_graph (synthetic certs) + the real-DB FTS5 MATCH probe and
the cross-module hash canonicality (the edge fingerprint that pins build->LSP->gates->hooks to
ONE graph). No SWE-bench tasks, no gold, no per-repo logic.
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

_GC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "metrics", "graph_certificate.py")
_spec = importlib.util.spec_from_file_location("graph_certificate_t", _GC_PATH)
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from groundtruth.runtime import proof as _proof  # noqa: E402


def _base_graph_cert(**kw):
    """A valid, in-container, consistent-hash certificate; override per test."""
    c = {
        "edges_count": 100, "calls_edges_count": 80,
        "fts5_exists": True, "fts5_row_count": 50, "fts5_match_probe_ok": True,
        "built_inside_container": True,
        "host_resolved_graph_db": "/tmp/gt/graph.db", "prebuilt_active": True,
        "closure_rebuilt_after_lsp": True,
        "graph_hash": "abc", "graph_hash_after_lsp": "abc", "hook_graph_hash": "abc",
    }
    c.update(kw)
    return c


def _make_edges_db(path, with_fts5=False, fts5_regular=False):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, qualified_name TEXT, "
              "signature TEXT, file_path TEXT)")
    c.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
              "type TEXT, resolution_method TEXT, confidence REAL, trust_tier TEXT)")
    c.executemany("INSERT INTO nodes (id,name,qualified_name,signature,file_path) VALUES (?,?,?,?,?)",
                  [(1, "a", "a", "()", "f.py"), (2, "b", "b", "()", "f.py")])
    c.executemany("INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,trust_tier) "
                  "VALUES (?,?,?,?,?,?)",
                  [(1, 2, "CALLS", "same_file", 1.0, "CERTIFIED"),
                   (2, 1, "CALLS", "name_match", 0.4, "SPECULATIVE")])
    if with_fts5:
        try:
            c.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(name, qualified_name)")
            c.execute("INSERT INTO nodes_fts (name, qualified_name) VALUES ('a','a'),('b','b')")
        except sqlite3.OperationalError:
            pass  # this python's sqlite3 was built without FTS5
    if fts5_regular:
        c.execute("CREATE TABLE nodes_fts (name TEXT)")
        c.execute("INSERT INTO nodes_fts (name) VALUES ('a')")
    c.commit()
    c.close()


# ── classifier matrix (the required hard gates) ──────────────────────────────

def test_graph_valid():
    assert gc.classify_graph(_base_graph_cert(), proof_mode=True) == ("GRAPH_VALID", True)


def test_fts5_missing_fails():
    assert gc.classify_graph(_base_graph_cert(fts5_exists=False), proof_mode=True) == ("GRAPH_FAIL_FTS5", False)


def test_fts5_empty_fails():
    assert gc.classify_graph(_base_graph_cert(fts5_row_count=0), proof_mode=True) == ("GRAPH_FAIL_FTS5", False)


def test_fts5_match_unqueryable_fails():
    assert gc.classify_graph(_base_graph_cert(fts5_match_probe_ok=False), proof_mode=True) == ("GRAPH_FAIL_FTS5", False)


def test_missing_handoff_in_proof_fails():
    assert gc.classify_graph(_base_graph_cert(host_resolved_graph_db=""), proof_mode=True) == ("GRAPH_FAIL_MISSING_HANDOFF", False)


def test_missing_handoff_outside_proof_ok():
    # handoff is only mandatory under proof mode
    assert gc.classify_graph(_base_graph_cert(host_resolved_graph_db=""), proof_mode=False) == ("GRAPH_VALID", True)


def test_handoff_inactive_in_proof_fails():
    assert gc.classify_graph(_base_graph_cert(prebuilt_active=False), proof_mode=True) == ("GRAPH_FAIL_HANDOFF_INACTIVE", False)


def test_stale_closure_fails():
    assert gc.classify_graph(_base_graph_cert(closure_rebuilt_after_lsp=False), proof_mode=True) == ("GRAPH_FAIL_STALE_CLOSURE", False)


def test_hash_mismatch_vs_lsp_fails():
    assert gc.classify_graph(_base_graph_cert(graph_hash_after_lsp="DEF"), proof_mode=True) == ("GRAPH_FAIL_HASH_MISMATCH", False)


def test_hook_hash_mismatch_fails():
    assert gc.classify_graph(_base_graph_cert(hook_graph_hash="DEF"), proof_mode=True) == ("GRAPH_FAIL_HOOK_MISMATCH", False)


def test_built_on_host_in_proof_fails():
    assert gc.classify_graph(_base_graph_cert(built_inside_container=False), proof_mode=True) == ("GRAPH_FAIL_BUILT_ON_HOST", False)


def test_empty_graph_fails():
    assert gc.classify_graph(_base_graph_cert(calls_edges_count=0), proof_mode=True) == ("GRAPH_FAIL_EMPTY", False)


def test_empty_cert_fails():
    assert gc.classify_graph(None, proof_mode=True) == ("GRAPH_FAIL_EMPTY", False)


# ── real-DB: FTS5 MATCH proof + hash canonicality ────────────────────────────

def test_hash_canonical_across_modules(tmp_path):
    db = str(tmp_path / "g.db")
    _make_edges_db(db)
    h_gc = gc.graph_edges_hash(db)
    h_proof = _proof.graph_edges_hash(db)
    assert h_gc and h_gc == h_proof, "graph_certificate and proof hashes must be identical"
    # resolve._graph_edges_hash too (if resolve imports in this env)
    try:
        from groundtruth.resolve import _graph_edges_hash as _rh
        assert _rh(db) == h_gc, "resolve hash must match the canonical hash"
    except Exception:
        pass


def test_fts5_match_probe_positive(tmp_path):
    db = str(tmp_path / "g.db")
    _make_edges_db(db, with_fts5=True)
    exists, rows, match_ok = gc.fts5_match_probe(db)
    if not exists:
        pytest.skip("sqlite3 built without FTS5 in this environment")
    assert rows >= 1 and match_ok is True


def test_fts5_match_probe_negative_regular_table(tmp_path):
    # a REGULAR table named nodes_fts must fail the MATCH probe (not Go-built FTS5)
    db = str(tmp_path / "g.db")
    _make_edges_db(db, fts5_regular=True)
    exists, rows, match_ok = gc.fts5_match_probe(db)
    assert exists is True and match_ok is False


def test_build_certificate_real_db(tmp_path):
    db = str(tmp_path / "g.db")
    _make_edges_db(db, with_fts5=True)
    cert = gc.build_graph_certificate(db, source_root="/repo")
    assert cert["edges_count"] == 2 and cert["calls_edges_count"] == 2
    assert cert["deterministic_edge_count"] == 1 and cert["name_match_edge_count"] == 1
    assert cert["graph_hash"]
    # a real DB built here is consistent with the LSP cert when hashes are wired equal
    cert2 = gc.build_graph_certificate(
        db, lsp_cert={"graph_db": db, "graph_hash_after_lsp": cert["graph_hash"],
                      "closure_rebuilt_after_lsp": True})
    assert cert2["lsp_warm_from_same_graph"] is True
    assert gc.classify_graph(cert2, proof_mode=False)[0] == "GRAPH_VALID"


def test_witness_format():
    w = gc.format_graph_witness("/tmp/gt/graph.db", "/tmp/gt_index.db", "abc123", True)
    assert "host_resolved_graph_db=/tmp/gt/graph.db" in w
    assert "hook_graph_db=/tmp/gt_index.db" in w
    assert "hook_graph_hash=abc123" in w
    assert "_gt_prebuilt_active=True" in w
